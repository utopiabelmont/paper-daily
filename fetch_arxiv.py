#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一版 arXiv 每日抓取引擎 —— 用 --profile 选择方向配置。

用法：
    python fetch_arxiv.py --profile main   # 主方向：傅里叶光学/精密测量/工业检测ML
    python fetch_arxiv.py --profile am     # 交叉方向：激光增材监测 × 光学测量/ML

流程：读取历史简报已报道的 arXiv ID → 查 arXiv API → 时间窗过滤 → 剔除历史重复
      → 相关度打分 → 单次内去重 → 输出 JSON + Markdown 候选清单交给模型总结。

通用规则（两个 profile 共享，改一处即全局生效）：
  - 日期标签按 JST(UTC+9) 计算；时间窗按 UTC 比对（arXiv 时间就是 UTC）
  - 跨天去重扫描 DEDUP_DIRS 中所有历史简报，两条推送互不重复
  - 时间窗的锚点是「数据源索引前沿」而非「当前时刻」（2026-08-30 修复）：
    arXiv 的 submittedDate 索引会滞后（实测曾达 51 小时），若按当前时刻往回推，
    窗口的新鲜端是空的，而真正待召回的论文补进索引时已滑出窗口，造成静默漏检。
    改为先探测索引前沿、再以它为基准往回推，索引滞后多久窗口就自动后移多久。
    放宽窗口不会带来重复推送——跨天去重是按 arXiv ID 做的。
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

LOCAL_TZ = timezone(timedelta(hours=9))
DEDUP_DIRS = ["digests", "digests_am"]          # 两个方向共用，互相防重复
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
        ],
        "keywords": [
            "edge localization", "edge detection", "defect detection", "surface defect",
            "anomaly detection", "surface inspection", "industrial inspection",
            "visual inspection", "optical inspection", "semiconductor", "wafer",
            "fourier optics", "wave optics", "spatial frequency", "frequency filtering",
            "phase retrieval", "computational imaging", "super-resolution",
            "point spread function", "wavefront", "diffraction", "interferometry",
            "metrology", "profilometry", "subpixel", "sub-pixel", "sub-micron", "submicron",
            "precision measurement", "dimensional measurement",
            "cnn", "convolutional", "deep learning", "machine learning",
        ],
        "window_hours": 72,
        "top_n": 12,
        "max_pages": 3,
    },
    # ---- 交叉方向：激光增材制造在线监测 × 光学精密测量/ML ----
    "am": {
        "label": "交叉方向候选论文",
        "out_json": "papers_am.json",
        "out_md": "candidates_am.md",
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
        ],
        "keywords": [
            "additive manufacturing", "directed energy deposition", "powder bed fusion",
            "selective laser melting", "laser metal deposition", "laser cladding",
            "melt pool", "meltpool", "laser ultrasonic", "laser welding", "keyhole",
            "in-situ monitoring", "in situ monitoring", "process monitoring",
            "online monitoring", "thermal imaging", "pyrometry", "spatter",
            "porosity", "lack of fusion", "layer height", "surface roughness",
            "closed-loop", "feedback control", "physics-informed", "digital twin",
            "nondestructive", "non-destructive", "ultrasonic",
            "defect detection", "anomaly detection", "quality control",
            "optical coherence tomography", "fringe projection", "structured light",
            "profilometry", "interferometry", "edge detection", "subpixel", "metrology",
            "cnn", "convolutional", "deep learning", "machine learning",
            "u-net", "segmentation",
        ],
        "window_hours": 72,
        "top_n": 12,
        "max_pages": 3,
    },
}
# ====================================================

MAX_RESULTS_PER_PAGE = 100
MAX_RETRIES = 6
RETRY_BACKOFF = 5   # 秒，第 n 次失败后等待 n * RETRY_BACKOFF
# 索引前沿最多允许把窗口往回推这么多小时。正常滞后是几小时；设上限是为了防止
# 数据源长时间异常时窗口无限扩大，把几百篇陈年论文重新拉进打分流程。
MAX_LAG_HOURS = 240


def load_past_reported_ids():
    ids = set()
    for d in DEDUP_DIRS:
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if fn.endswith(".md"):
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


def fetch_page(cfg, start):
    params = {
        "search_query": build_search_query(cfg),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "start": start,
        "max_results": MAX_RESULTS_PER_PAGE,
    }
    url = API + "?" + urllib.parse.urlencode(params)
    # arXiv 对这类长查询经常返回 503 / 超时，需重试
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "daily-digest/2.0"})
            with urllib.request.urlopen(req, timeout=90) as resp:
                return resp.read()
        except Exception as ex:
            last = ex
            sys.stderr.write(f"[retry] start={start} 第 {attempt + 1} 次失败: {ex}\n")
            time.sleep(RETRY_BACKOFF * (attempt + 1))
    raise last


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
        req = urllib.request.Request(url, headers={"User-Agent": "daily-digest/2.0"})
        with urllib.request.urlopen(req, timeout=90) as resp:
            entries = parse_entries(resp.read())
    except Exception as ex:
        sys.stderr.write(f"[warn] 索引前沿探测失败，退回按当前时刻计算窗口: {ex}\n")
        return None
    stamps = [d for d in (parse_published(e["published"]) for e in entries) if d]
    return max(stamps) if stamps else None


def compute_cutoff(cfg):
    """返回 (窗口起点, 索引前沿, 滞后小时数)。"""
    now = datetime.now(timezone.utc)
    frontier = probe_index_frontier(cfg)
    if frontier is None:
        return now - timedelta(hours=cfg["window_hours"]), None, None
    lag = (now - frontier).total_seconds() / 3600
    anchor = frontier if lag > 0 else now
    if lag > MAX_LAG_HOURS:
        sys.stderr.write(f"[warn] 索引滞后 {lag:.1f}h 超过上限 {MAX_LAG_HOURS}h，"
                         f"窗口锚点按上限截断\n")
        anchor = now - timedelta(hours=MAX_LAG_HOURS)
    return anchor - timedelta(hours=cfg["window_hours"]), frontier, lag


def relevance_score(paper, keywords):
    title = paper["title"].lower()
    summary = paper["summary"].lower()
    score = 0
    for kw in keywords:
        if kw in title:
            score += 3
        if kw in summary:
            score += 1
    return score


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True, choices=sorted(PROFILES),
                    help="选择方向配置: " + ", ".join(sorted(PROFILES)))
    args = ap.parse_args()
    cfg = PROFILES[args.profile]

    past = load_past_reported_ids()
    cutoff, frontier, lag = compute_cutoff(cfg)
    seen, collected, skipped_past = set(), [], 0
    for page in range(cfg["max_pages"]):
        try:
            entries = parse_entries(fetch_page(cfg, page * MAX_RESULTS_PER_PAGE))
        except Exception as ex:
            sys.stderr.write(f"[warn] 第 {page} 页抓取失败: {ex}\n")
            continue
        if not entries:
            break
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
            p["score"] = relevance_score(p, cfg["keywords"])
            if p["score"] > 0:
                collected.append(p)

    collected.sort(key=lambda x: (x["score"], x["published"]), reverse=True)
    top = collected[:cfg["top_n"]]

    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    with open(cfg["out_json"], "w", encoding="utf-8") as f:
        json.dump({"date": today, "profile": args.profile, "count": len(top),
                   "papers": top}, f, ensure_ascii=False, indent=2)

    if frontier is None:
        anchor_note = "索引前沿探测失败，窗口按当前时刻计算"
    else:
        anchor_note = (f"索引前沿 {frontier.strftime('%Y-%m-%dT%H:%M:%SZ')}"
                       f"（滞后 {lag:.1f}h），窗口起点 "
                       f"{cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')}")

    lines = [f"# {cfg['label']} {today}（共 {len(top)} 篇，已跳过 {skipped_past} 篇历史重复）\n"]
    lines.append(f"> 窗口 {cfg['window_hours']}h ｜ {anchor_note}\n")
    if not top:
        lines.append("今日无匹配新论文。")
    for i, p in enumerate(top, 1):
        lines.append(f"## {i}. {p['title']}")
        lines.append(f"- arXiv: {p['arxiv_id']}  |  分类: {p['primary_category']}  |  相关度: {p['score']}")
        lines.append(f"- 作者: {', '.join(p['authors'])}")
        lines.append(f"- 链接: {p['link']}")
        lines.append(f"- 摘要原文: {p['summary']}\n")
    with open(cfg["out_md"], "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"[{args.profile}] 完成：{len(top)} 篇候选（{today}，窗口 {cfg['window_hours']}h，"
          f"跳过历史重复 {skipped_past}），已写入 {cfg['out_json']} 与 {cfg['out_md']}\n"
          f"[{args.profile}] {anchor_note}")


if __name__ == "__main__":
    main()
