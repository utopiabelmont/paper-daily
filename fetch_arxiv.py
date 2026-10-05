#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一版 arXiv 每日抓取引擎 —— 用 --profile 选择方向配置。

用法：
    python fetch_arxiv.py --profile main   # 主方向：傅里叶光学/精密测量/工业检测ML
    python fetch_arxiv.py --profile am     # 交叉方向：激光增材监测 × 光学测量/ML

流程：读取历史简报已报道与已评分的 arXiv ID → 查 arXiv API → 时间窗过滤 → 剔除历史重复
      → 分层关键词召回与排序 → 单次内去重 → 输出 JSON + Markdown 候选清单。
      之后由 select_papers.py 做预筛与两次独立评分，决定哪些论文写进简报。

通用规则（两个 profile 共享，改一处即全局生效）：
  - 日期标签按 JST(UTC+9) 计算；时间窗按 UTC 比对（arXiv 时间就是 UTC）
  - 跨天去重扫描 DEDUP_DIRS 中所有历史简报，两条推送互不重复
  - 时间窗的锚点是「数据源索引前沿」而非「当前时刻」（2026-08-30 修复）：
    arXiv 的 submittedDate 索引会滞后（实测曾达 51 小时），若按当前时刻往回推，
    窗口的新鲜端是空的，而真正待召回的论文补进索引时已滑出窗口，造成静默漏检。
    改为先探测索引前沿、再以它为基准往回推，索引滞后多久窗口就自动后移多久。
    放宽窗口不会带来重复推送——跨天去重是按 arXiv ID 做的。
  - 「滞后」小时数本身不说明索引是否异常（2026-10-05 补）：arXiv 只在周日至周四
    20:00 ET 公告，定时任务在公告前运行时，周日、周一（JST）必然没有新批次，
    滞后会涨到 51h、75h。脚本按公告时间表判断索引是否已收录最近一次公告，
    在输出里写明「无新批次」还是「索引落后」，避免把周末空窗误报成故障。
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    NY_TZ = ZoneInfo("America/New_York")   # arXiv 公告时间表按美东时间（含夏令时）
except Exception:
    NY_TZ = None

LOCAL_TZ = timezone(timedelta(hours=9))
DEDUP_DIRS = ["digests", "digests_am"]          # 两个方向共用，互相防重复
SELECTION_LOG_DIR = "selection_log"             # select_papers.py 的评分日志，同样参与去重
API = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
# 历史简报里的 ID 多带版本后缀（2607.24703v1），\b 会被 v 挡住，故改用数字边界断言
ARXIV_ID_RE = re.compile(r"(?<!\d)(\d{4}\.\d{4,5})(?![\d.])")

# ==================== 方向配置区 ====================
PROFILES = {
    # ---- 主方向：傅里叶光学 / 精密测量 / 工业检测机器学习 ----
    "main": {
        "label": "主方向候选论文",
        "out_json": "papers.json",
        "out_md": "candidates.md",
        "digest_dir": "digests",
        "categories": ["physics.optics", "eess.IV", "cs.CV", "eess.SP"],
        "server_terms": [
            'abs:"edge localization"', 'abs:"edge detection"',
            'abs:"defect detection"', 'abs:"surface defect"', 'abs:"anomaly detection"',
            'abs:"surface inspection"', 'abs:"industrial inspection"',
            'abs:"visual inspection"', 'abs:"optical inspection"',
            'abs:"semiconductor"', 'abs:"wafer"',
            'abs:"Fourier optics"', 'abs:"spatial frequency"', 'abs:"phase retrieval"',
            'abs:"computational imaging"', 'abs:"super-resolution"',
            'abs:"point spread function"', 'abs:"wavefront"',
            'abs:"metrology"', 'abs:"profilometry"', 'abs:"interferometry"',
            'abs:"subpixel"', 'abs:"sub-pixel"', 'abs:"dimensional measurement"',
            # 2026-10-03 补：此前漏召回或只靠分类进来的方向
            'abs:"deflectometry"', 'abs:"fringe projection"', 'abs:"structured light"',
            'abs:"metasurface"', 'abs:"optical computing"', 'abs:"aberration"',
        ],
        # 分层关键词（2026-10-03）：只用于召回与排序，不决定是否推送。
        # 是否推送由 select_papers.py 的预筛与两次评分决定。
        "keywords_core": [
            "edge localization", "edge detection", "edge enhancement", "subpixel", "sub-pixel",
            "sub-micron", "submicron", "fourier optics", "spatial frequency", "frequency filtering",
            "phase retrieval", "point spread function", "aberration", "wavefront", "diffract",
            "interferometry", "interferometric", "profilometry", "metrology",
            "precision measurement", "dimensional measurement", "fringe projection",
            "structured light", "deflectometry", "defect detection", "surface defect",
            "surface inspection", "industrial inspection", "optical inspection",
            "visual inspection", "industrial anomaly", "wafer", "metasurface",
            "optical computing", "optical neural network", "computational imaging",
        ],
        "keywords_generic": [
            "anomaly detection", "super-resolution", "semiconductor", "wave optics",
            "cnn", "convolutional", "deep learning", "machine learning", "segmentation",
        ],
        "keywords_negative": [
            "mri", "medical", "clinical", "remote sensing", "satellite", "quantum",
            "wireless", "communication", "autonomous driving", "video anomaly",
        ],
        "window_hours": 72,
        "top_n": 30,   # 召回上限，交给模型预筛与评分；最终推送数见 selection/config.json
        "max_pages": 3,
    },
    # ---- 交叉方向：激光增材制造在线监测 × 光学精密测量/ML ----
    "am": {
        "label": "交叉方向候选论文",
        "out_json": "papers_am.json",
        "out_md": "candidates_am.md",
        "digest_dir": "digests_am",
        "categories": ["physics.app-ph", "cond-mat.mtrl-sci", "eess.IV",
                       "cs.CV", "eess.SY", "physics.optics"],
        "server_terms": [
            'abs:"additive manufacturing"', 'abs:"directed energy deposition"',
            'abs:"powder bed fusion"', 'abs:"selective laser melting"',
            'abs:"laser metal deposition"', 'abs:"laser cladding"',
            'abs:"melt pool"', 'abs:"laser ultrasonic"', 'abs:"laser welding"',
            'abs:"process monitoring"', 'abs:"in-situ monitoring"',
            'abs:"in situ monitoring"', 'abs:"physics-informed neural"',
            'abs:"nondestructive"', 'abs:"non-destructive"',
            # 2026-10-03 补：词形变体与激光加工
            'abs:"additively manufactured"', 'abs:"waterjet"', 'abs:"laser machining"',
        ],
        "keywords_core": [
            "additive manufacturing", "additively manufactured", "directed energy deposition",
            "powder bed fusion", "selective laser melting", "laser metal deposition",
            "laser cladding", "melt pool", "meltpool", "melt-pool", "laser ultrasonic",
            "laser welding", "keyhole", "spatter", "in-situ monitoring", "in situ monitoring",
            "process monitoring", "online monitoring", "layer height", "waterjet",
            "water jet guided", "water-jet guided", "laser machining",
        ],
        "keywords_generic": [
            "thermal imaging", "pyrometry", "porosity", "lack of fusion", "surface roughness",
            "closed-loop", "feedback control", "physics-informed", "digital twin",
            "nondestructive", "non-destructive", "ultrasonic", "defect detection",
            "anomaly detection", "quality control", "optical coherence tomography",
            "fringe projection", "structured light", "profilometry", "interferometry",
            "edge detection", "subpixel", "metrology", "cnn", "convolutional",
            "deep learning", "machine learning", "u-net", "segmentation",
        ],
        "keywords_negative": [
            "lidar", "remote sensing", "medical", "battery", "power electronics",
            "converter", "wireless",
        ],
        "window_hours": 72,
        "top_n": 30,   # 召回上限，交给模型预筛与评分；最终推送数见 selection/config.json
        "max_pages": 3,
    },
}
# ====================================================

MAX_RESULTS_PER_PAGE = 100
LEGACY_MD_TOP = 12  # candidates*.md 的篇数上限（旧版定时任务按它写简报）
MAX_RETRIES = 6
RETRY_BACKOFF = 15  # 秒，第 n 次失败后等待 n * RETRY_BACKOFF
# arXiv 对突发请求限流，且 406 与 429 混用（2026-09-22 实测同一 URL 同一请求头，
# 密集连发必 406/429、间隔 6s 后连发 4 次全部 200），故所有请求统一走节流闸门。
MIN_REQUEST_INTERVAL = 6  # 秒，任意两次 API 请求之间的最小间隔
# 探测失败时窗口额外回溯的小时数。锚点退回「当前时刻」是危险的默认：索引一旦
# 滞后超过窗口长度（2026-09-22 实测滞后 75.3h > 窗口 72h），窗口整段落在索引
# 前沿之后，明明有新论文也会全部滑出，把一次瞬时 406 放大成静默的零命中。
# 宁可放宽——跨天去重按 arXiv ID 做，窗口放宽不会造成重复推送。
PROBE_FAIL_EXTRA_HOURS = 96
_last_request_ts = [0.0]

# 传输层客户端，选用理由见 api_get() 的说明。
try:
    import requests as _requests
    _SESSION = _requests.Session()
    _SESSION.headers.update({"User-Agent": "daily-digest/2.0"})
except ImportError:
    _requests = None
    _SESSION = None
# 索引前沿最多允许把窗口往回推这么多小时。正常滞后是几小时；设上限是为了防止
# 数据源长时间异常时窗口无限扩大，把几百篇陈年论文重新拉进打分流程。
MAX_LAG_HOURS = 240


def load_past_reported_ids(own_digest_dir):
    """历史简报里出现过的 ID，加上此前各天已评过分的 ID（selection_log/）。

    评过分但没入选的论文也算「已处理」：72h 窗口每天交叠，否则同一篇会被连续几天重复评分。
    当天的评分日志和本方向当天的简报都不计入，这样同一天重跑时能拿回同一批候选；
    只算评分日志而不算简报的话，重跑会把首次运行已写进简报的论文当成历史重复剔掉，
    简报被覆盖后这几篇就丢了。另一方向当天的简报照常计入，两条推送互不重复。
    """
    ids = set()
    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    for d in DEDUP_DIRS + [SELECTION_LOG_DIR]:
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if d in (SELECTION_LOG_DIR, own_digest_dir) and fn.startswith(today):
                continue
            if fn.endswith((".md", ".jsonl")):
                try:
                    with open(os.path.join(d, fn), encoding="utf-8") as f:
                        ids |= set(ARXIV_ID_RE.findall(f.read()))
                except Exception:
                    pass
    return ids


def build_search_query(cfg):
    cat_q = " OR ".join(f"cat:{c}" for c in cfg["categories"])
    term_q = " OR ".join(cfg["server_terms"])
    return f"({cat_q}) AND ({term_q})"


def api_get(url, timeout=90):
    """所有 arXiv API 请求的唯一出口：先等够节流间隔，再发请求。

    传输层优先 requests、缺失时才退回 urllib，原因是实测差异极其稳定：
    2026-09-22 用随机 nonce 构造「必定缓存 MISS」的全新 URL、请求间隔 8s
    （远超上面的节流阈值，故与限流无关）、四种客户端随机轮转交替各 3 轮——
        urllib（原请求头）        406 / 406 / 406
        urllib（带 Accept 头）    406 / 406 / 406
        requests                 200 / 200 / 200
        curl                     200 / 200 / 200
    同一时刻、同一检索式，仅换客户端即恢复，故排除限流、出口 IP、检索式复杂度。
    但进一步拆解头部时服务端返回过 503/429，且 429 说明限流确实存在（只是不是
    全部原因），各头部组合的结论在不同日期的复核中互相矛盾。因此这里不对 406 的
    确切触发条件下断言，只按可复现的强事实选客户端：requests 稳定可用。
    urllib 退回路径保留 Accept 与 Accept-Encoding 头，聊胜于无。
    """
    wait = MIN_REQUEST_INTERVAL - (time.time() - _last_request_ts[0])
    if wait > 0:
        time.sleep(wait)
    try:
        if _SESSION is not None:
            resp = _SESSION.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.content
        req = urllib.request.Request(url, headers={
            "User-Agent": "daily-digest/2.0",
            "Accept": "application/atom+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    finally:
        _last_request_ts[0] = time.time()


def api_get_retry(url, label):
    """带节流与重试的取数。索引前沿探测同样要重试——它只发一次就放弃的话，
    一次限流 406 就会让窗口锚点退回「当前时刻」，在索引滞后超过窗口长度时
    （2026-09-22 实测滞后 75.3h > 窗口 72h）直接产出全零的假空窗。"""
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            return api_get(url)
        except Exception as ex:
            last = ex
            sys.stderr.write(f"[retry] {label} 第 {attempt + 1} 次失败: {ex}\n")
            time.sleep(RETRY_BACKOFF * (attempt + 1))
    raise last


def fetch_page(cfg, start):
    params = {
        "search_query": build_search_query(cfg),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "start": start,
        "max_results": MAX_RESULTS_PER_PAGE,
    }
    url = API + "?" + urllib.parse.urlencode(params)
    # arXiv 对这类长查询经常返回 503 / 超时 / 限流(406、429)，需重试
    return api_get_retry(url, f"start={start}")


def parse_entries(xml_bytes):
    root = ET.fromstring(xml_bytes)
    out = []
    for e in root.findall(f"{ATOM}entry"):
        raw_id = e.findtext(f"{ATOM}id", "")
        base_id = raw_id.split("/abs/")[-1].split("v")[0]
        title = " ".join((e.findtext(f"{ATOM}title") or "").split())
        summary = " ".join((e.findtext(f"{ATOM}summary") or "").split())
        published = e.findtext(f"{ATOM}published", "")
        authors = [a.findtext(f"{ATOM}name", "") for a in e.findall(f"{ATOM}author")]
        prim = e.find(f"{ARXIV}primary_category")
        category = prim.get("term") if prim is not None else ""
        out.append({
            "arxiv_id": base_id, "title": title, "summary": summary,
            "published": published, "authors": authors[:6],
            "primary_category": category, "link": raw_id,
        })
    return out


def parse_published(published):
    try:
        return datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def probe_index_frontier(cfg):
    """探测数据源索引前沿：只按 category 查、不加任何关键词，取最新一条的投稿时间。

    不能拿主查询的最新命中当前沿——主查询带关键词，没有命中只说明今天没有相关
    论文，不代表索引滞后，两者必须分开测。探测失败返回 None，退回按当前时刻计算。
    """
    cat_q = " OR ".join(f"cat:{c}" for c in cfg["categories"])
    params = {"search_query": cat_q, "sortBy": "submittedDate",
              "sortOrder": "descending", "start": 0, "max_results": 5}
    url = API + "?" + urllib.parse.urlencode(params)
    try:
        entries = parse_entries(api_get_retry(url, "索引前沿探测"))
    except Exception as ex:
        sys.stderr.write(f"[warn] 索引前沿探测失败，窗口改走放宽兜底: {ex}\n")
        return None
    stamps = [d for d in (parse_published(e["published"]) for e in entries) if d]
    return max(stamps) if stamps else None


def _announcement(day):
    """day（美东日期）若是公告日，返回 (公告时刻, 本批收稿截止)，否则 None。

    arXiv 周日至周四 20:00 ET 公告，周五、周六不公告。每批收录截至当日 14:00 ET
    的投稿；周日那批截至周五 14:00 ET（周五下午至周一 14:00 ET 的投稿进周一那批）。
    不含 arXiv 节假日停更，节假日时会显示为「索引落后」，需要人工判断。
    """
    wd = day.weekday()  # 周一 = 0 … 周日 = 6
    if wd in (4, 5):
        return None
    cut_day = day - timedelta(days=2) if wd == 6 else day
    ann = datetime(day.year, day.month, day.day, 20, tzinfo=NY_TZ)
    cut = datetime(cut_day.year, cut_day.month, cut_day.day, 14, tzinfo=NY_TZ)
    return ann, cut


def arxiv_schedule_status(frontier, now):
    """对照公告时间表判断索引前沿是否正常，返回 (状态, 说明)。

    状态：in_sync＝索引已收录最近一次公告；behind＝最近一次公告已发出但索引还停在
    上一批；unknown＝无法判断（缺时区数据或探测失败）。
    """
    if NY_TZ is None or frontier is None:
        return "unknown", ""
    local = now.astimezone(NY_TZ)
    past = []          # 已发生的公告，由近到远
    for back in range(10):
        a = _announcement((local - timedelta(days=back)).date())
        if a and a[0] <= local:
            past.append(a)
        if len(past) == 2:
            break
    nxt = None
    for ahead in range(8):
        a = _announcement((local + timedelta(days=ahead)).date())
        if a and a[0] > local:
            nxt = a[0]
            break
    if len(past) < 2 or nxt is None:
        return "unknown", ""
    (last_ann, _), (_, prev_cut) = past
    jst = lambda d: d.astimezone(LOCAL_TZ).strftime("%m-%d %H:%M")
    next_note = f"下一次公告 JST {jst(nxt)}"
    if frontier > prev_cut:
        return "in_sync", (f"索引已收录最近一次 arXiv 公告（JST {jst(last_ann)}），"
                           f"此后尚无新批次，{next_note}")
    return "behind", (f"最近一次 arXiv 公告（JST {jst(last_ann)}）已发出，"
                      f"但索引仍停在上一批，属于索引延迟；{next_note}")


def compute_cutoff(cfg):
    """返回 (窗口起点, 索引前沿, 滞后小时数)。"""
    now = datetime.now(timezone.utc)
    frontier = probe_index_frontier(cfg)
    if frontier is None:
        # 探测不到前沿就不知道索引滞后多少，只能按最坏情况把窗口整体放宽，
        # 绝不能假设滞后为零（那等于断言「现在就是前沿」，正是假零的来源）。
        span = cfg["window_hours"] + PROBE_FAIL_EXTRA_HOURS
        sys.stderr.write(f"[warn] 索引前沿未知，窗口放宽到 {span}h 兜底\n")
        return now - timedelta(hours=span), None, None
    lag = (now - frontier).total_seconds() / 3600
    anchor = frontier if lag > 0 else now
    if lag > MAX_LAG_HOURS:
        sys.stderr.write(f"[warn] 索引滞后 {lag:.1f}h 超过上限 {MAX_LAG_HOURS}h，"
                         f"窗口锚点按上限截断\n")
        anchor = now - timedelta(hours=MAX_LAG_HOURS)
    return anchor - timedelta(hours=cfg["window_hours"]), frontier, lag


def keyword_score(paper, cfg):
    """分层关键词分，只用于召回与排序（2026-10-03 起不再决定是否推送）。

    - 核心词：标题 +3、摘要 +1（与旧版打分口径一致）
    - 泛化词：标题 +1、摘要 +1；没有任何核心词命中时，泛化词合计最多 1 分
    - 负面词：每命中一个 −2，只压低排序，不剔除——是否相关由模型预筛判断
    返回 (分数, 是否有任何正向命中)。没有正向命中的论文不进入候选。
    """
    title = paper["title"].lower()
    summary = paper["summary"].lower()
    core = sum((3 if kw in title else 0) + (1 if kw in summary else 0)
               for kw in cfg["keywords_core"])
    generic = sum((1 if kw in title else 0) + (1 if kw in summary else 0)
                  for kw in cfg["keywords_generic"])
    if core == 0:
        generic = min(generic, 1)
    negative = sum(2 for kw in cfg["keywords_negative"]
                   if re.search(r"\b" + re.escape(kw), title + " " + summary))
    return core + generic - negative, core + generic > 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True, choices=sorted(PROFILES),
                    help="选择方向配置: " + ", ".join(sorted(PROFILES)))
    args = ap.parse_args()
    cfg = PROFILES[args.profile]

    past = load_past_reported_ids(cfg["digest_dir"])
    cutoff, frontier, lag = compute_cutoff(cfg)
    index_status, index_note = arxiv_schedule_status(frontier, datetime.now(timezone.utc))
    seen, collected, skipped_past = set(), [], 0
    pages_ok, pages_failed, entries_seen = 0, 0, 0
    for page in range(cfg["max_pages"]):
        try:
            entries = parse_entries(fetch_page(cfg, page * MAX_RESULTS_PER_PAGE))
            pages_ok += 1
            entries_seen += len(entries)
        except Exception as ex:
            pages_failed += 1
            sys.stderr.write(f"[warn] 第 {page} 页抓取失败: {ex}\n")
            continue
        if not entries:
            break
        # 结果按投稿时间倒序，本页最旧一条若已早于窗口起点，后面的页只会更旧，
        # 不必再请求（每页省下的是一次受节流约束的往返）。
        page_stamps = [d for d in (parse_published(e["published"]) for e in entries) if d]
        oldest = min(page_stamps) if page_stamps else None
        for p in entries:
            if p["arxiv_id"] in seen:
                continue
            pub = parse_published(p["published"])
            if pub is None or pub < cutoff:
                continue
            seen.add(p["arxiv_id"])
            if p["arxiv_id"] in past:
                skipped_past += 1
                continue
            p["score"], hit = keyword_score(p, cfg)
            if hit:
                collected.append(p)
        if oldest is not None and oldest < cutoff:
            break

    collected.sort(key=lambda x: (x["score"], x["published"]), reverse=True)
    top = collected[:cfg["top_n"]]

    # ---- 假零护栏 ----
    # 2026-09-17 起连续六期出现过同一种事故：全部请求失败，脚本却照常写出
    # 「今日无匹配新论文」，与真的没有新论文在输出上完全无法区分，于是漏检被
    # 静默累积了六天。这里把「一条 entry 都没取到」与「取到了但没有命中」区分开：
    # 前者是故障，必须在文件里写明、并以非零退出码让调度方看见，绝不允许它再
    # 伪装成一次正常的空结果。
    fetch_failed = entries_seen == 0
    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    with open(cfg["out_json"], "w", encoding="utf-8") as f:
        json.dump({"date": today, "profile": args.profile, "count": len(top),
                   "fetch_failed": fetch_failed, "pages_ok": pages_ok,
                   "pages_failed": pages_failed, "entries_seen": entries_seen,
                   "skipped_past": skipped_past,
                   "index_frontier": frontier.strftime("%Y-%m-%dT%H:%M:%SZ") if frontier else None,
                   "index_lag_hours": round(lag, 1) if lag is not None else None,
                   "index_status": index_status, "index_note": index_note,
                   "papers": top},
                  f, ensure_ascii=False, indent=2)

    if frontier is None:
        anchor_note = (f"索引前沿探测失败，窗口放宽至 "
                       f"{cfg['window_hours'] + PROBE_FAIL_EXTRA_HOURS}h 兜底")
    else:
        anchor_note = (f"索引前沿 {frontier.strftime('%Y-%m-%dT%H:%M:%SZ')}"
                       f"（滞后 {lag:.1f}h），窗口起点 "
                       f"{cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    if index_note:
        anchor_note += f"；{index_note}"

    # candidates.md 是旧版定时任务直接据以写简报的清单，保持旧版的 12 篇上限，
    # 这样定时任务说明未切换时行为不变；新流程读取 papers.json 里的全部候选。
    legacy = top[:LEGACY_MD_TOP]
    lines = [f"# {cfg['label']} {today}（共 {len(legacy)} 篇，已跳过 {skipped_past} 篇历史重复）\n"]
    lines.append(f"> 窗口 {cfg['window_hours']}h ｜ {anchor_note}\n")
    if len(top) > len(legacy):
        lines.append(f"> 本文件只列关键词分最高的 {len(legacy)} 篇；精选流程读取 "
                     f"{cfg['out_json']} 中的全部 {len(top)} 篇。\n")
    if fetch_failed:
        lines.append(f"**⚠ 抓取失败：{pages_failed} 页请求全部失败，一条 entry 都没取到。**\n")
        lines.append("本文件不代表「今日无新论文」——这是故障，不是空结果。"
                     "请勿据此撰写「今日无匹配新论文」，须在简报中如实记录抓取失败。\n")
    elif not top:
        lines.append("今日无匹配新论文。")
        lines.append(f"\n> 抓取健康：成功 {pages_ok} 页、取到 {entries_seen} 条 entry、"
                     f"其中 {skipped_past} 条为历史已报道。此为真实空结果。")
    for i, p in enumerate(legacy, 1):
        lines.append(f"## {i}. {p['title']}")
        lines.append(f"- arXiv: {p['arxiv_id']}  |  分类: {p['primary_category']}  |  相关度: {p['score']}")
        lines.append(f"- 作者: {', '.join(p['authors'])}")
        lines.append(f"- 链接: {p['link']}")
        lines.append(f"- 摘要原文: {p['summary']}\n")
    with open(cfg["out_md"], "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"[{args.profile}] 完成：{len(top)} 篇候选（{today}，窗口 {cfg['window_hours']}h，"
          f"跳过历史重复 {skipped_past}），已写入 {cfg['out_json']} 与 {cfg['out_md']}\n"
          f"[{args.profile}] 抓取健康：成功 {pages_ok} 页 / 失败 {pages_failed} 页 / "
          f"entry {entries_seen} 条\n"
          f"[{args.profile}] {anchor_note}")
    if fetch_failed:
        sys.stderr.write(f"[error] {args.profile}: 抓取全败（{pages_failed} 页），"
                         f"输出的 0 篇是故障而非空结果，退出码 1\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
