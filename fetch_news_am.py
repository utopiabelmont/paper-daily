#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交叉方向行业/商业动态抓取 —— 基于 Google News RSS（无需 API key，仅标准库）。
覆盖：增材制造在线监测的商用系统、公司动态、产业化新闻（中英双语查询）。

流程：多条查询拉 RSS → 时间窗过滤 → 三层去重（链接/标题/跨语种签名）
      → 按日期排序 → 输出 news_am.md 交给模型翻译/概述。

去重的三层结构（2026-08-30 修复，见下）：
  L1 链接：与本次已收条目、以及历史简报中出现过的链接精确比对。
  L2 标题：归一化标题与历史简报中的标题做字符级相似度比对，抓「同一新闻被
     Google News 分配了不同跳转 URL」的情况。
  L3 跨语种签名：同来源、同发布日、且去掉通用词后的拉丁词块高度重合，抓
     「同一新闻的中英文两条」——这两条标题不同、链接不同，L1/L2 都抓不住。
     已知边界：当中文标题只保留了一个拉丁词块（如公司名）时，L3 无法区分
     「同一新闻的另一语种版本」与「同一公司同来源的另一条新闻」，此时只靠
     「同发布日」这一个约束兜底。这正是 L3 必须可复核、不可静默丢弃的原因。
L2/L3 属于启发式判断，**不静默丢弃**：被它们抑制的条目会连同判据一起写进
news_am.md 末尾，便于人工复核与推翻。

前提：routine 环境需能访问 news.google.com（Network access 为 Full 即可）。
"""

import difflib
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import email.utils
from datetime import datetime, timedelta, timezone

# ---------------- 配置区 ----------------
LOCAL_TZ = timezone(timedelta(hours=9))
NEWS_WINDOW_HOURS = 168        # 行业新闻稀疏，取 7 天窗口；靠去重保证每条只出现一次
NEWS_TOP_N = 10
DEDUP_DIRS = ["digests_am"]    # 扫描历史简报里的链接做去重

# (查询, hl, gl, ceid)；英文抓国际产业动态，中文抓国内动态
QUERIES = [
    ('"additive manufacturing" monitoring OR inspection', "en-US", "US", "US:en"),
    ('"directed energy deposition"',                      "en-US", "US", "US:en"),
    ('"metal 3D printing" quality OR defect',             "en-US", "US", "US:en"),
    ('"laser ultrasonic" inspection',                     "en-US", "US", "US:en"),
    ('"melt pool" monitoring',                            "en-US", "US", "US:en"),
    ('增材制造 检测',                                       "zh-CN", "CN", "CN:zh-Hans"),
    ('金属3D打印 质量控制',                                 "zh-CN", "CN", "CN:zh-Hans"),
    ('激光超声 检测',                                       "zh-CN", "CN", "CN:zh-Hans"),
]
# ----------------------------------------

TAG_RE = re.compile(r"<[^>]+>")
# 历史简报里的链接几乎都写在 markdown 行内链接 `[文字](URL)` 里，后面还常紧跟
# 全角分隔符。旧写法 `https?://\S+` 会把右括号连同其后的内容一起吞掉，导致历史
# 链接集合中没有一条干净 URL、L1 去重完全失效（2026-08-30 定位并修复）。
URL_RE = re.compile(r"https?://[^\s)\]<>\"'“”‘’｜|，、）]+")
# 历史简报中的标题写法：**加粗标题**、`### 3. 标题`、以及重复清单表格的第 2 列
PAST_TITLE_RES = [
    re.compile(r"\*\*(.+?)\*\*"),
    re.compile(r"^#{2,4}\s*\d*[.、]?\s*(.+?)\s*$", re.M),
    re.compile(r"^\|\s*\d+\s*\|\s*(.+?)\s*\|", re.M),
]
# 通用词去掉后剩下的才是有区分力的词块（公司名、产品名、机构名等）
GENERIC_TOKENS = {
    "the", "and", "for", "with", "from", "into", "its", "new", "how", "why",
    "additive", "manufacturing", "printing", "printed", "print", "metal",
    "metals", "laser", "powder", "bed", "fusion", "material", "materials",
    "quality", "control", "monitoring", "inspection", "detection", "system",
    "systems", "technology", "technologies", "process", "industrial",
    "company", "companies", "market", "report", "reports", "research",
    "researchers", "study", "brings", "aims", "adapts", "announces",
}
LATIN_TOKEN_RE = re.compile(r"[a-z][a-z0-9]{2,}")
# L2/L3 判据阈值：真重复实测 0.89、真新条目实测 ≤0.35，0.72 有很宽的安全边界
TITLE_SIM_THRESHOLD = 0.72
SIGNATURE_CONTAINMENT = 0.6


def load_past():
    """扫历史简报，返回 (已报道链接集合, 已报道标题集合)。"""
    links, titles = set(), set()
    for d in DEDUP_DIRS:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".md"):
                continue
            try:
                with open(os.path.join(d, fn), encoding="utf-8") as f:
                    text = f.read()
            except Exception:
                continue
            links |= set(URL_RE.findall(text))
            for pat in PAST_TITLE_RES:
                for raw in pat.findall(text):
                    n = norm_title(raw)
                    # 太短的多为「本期」「如实写明」这类行文加粗，不是标题
                    if 6 < len(n) < 80:
                        titles.add(n)
    return links, titles


def fetch_rss(query, hl, gl, ceid):
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote_plus(query)
           + f"&hl={hl}&gl={gl}&ceid={ceid}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 daily-digest-am/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def parse_items(xml_bytes):
    out = []
    root = ET.fromstring(xml_bytes)
    channel = root.find("channel")
    if channel is None:
        return out
    for it in channel.findall("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate") or ""
        src = (it.findtext("source") or "").strip()
        desc = TAG_RE.sub(" ", it.findtext("description") or "")
        desc = " ".join(desc.split())[:200]
        try:
            dt = email.utils.parsedate_to_datetime(pub)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            dt = None
        out.append({"title": title, "link": link, "source": src,
                    "dt": dt, "snippet": desc})
    return out


def norm_title(t):
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", t.lower())


def signature(title):
    """标题里有区分力的拉丁词块。同一新闻的中英文两条报道标题不同、链接不同，
    但公司名/产品名（Additive Assurance、AMiRIS、Phase3D）通常都以拉丁字母原样保留。"""
    return {w for w in LATIN_TOKEN_RE.findall(title.lower()) if w not in GENERIC_TOKENS}


def containment(a, b):
    """重合词数 / 较小集合的大小。用包含度而非 Jaccard：中文标题保留的拉丁词块
    通常远少于英文标题，Jaccard 会被长度差异稀释掉。"""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def best_past_match(nt, past_titles):
    """返回 (最高相似度, 对应的历史标题)。"""
    best, best_t = 0.0, ""
    for p in past_titles:
        # 长度相差一倍以上不可能达到阈值，先跳过，省掉绝大部分比对开销
        if not (0.5 <= len(nt) / len(p) <= 2.0):
            continue
        r = difflib.SequenceMatcher(None, nt, p).ratio()
        if r > best:
            best, best_t = r, p
    return best, best_t


def day_key(it):
    return it["dt"].astimezone(LOCAL_TZ).strftime("%Y-%m-%d") if it["dt"] else ""


def main():
    past_links, past_titles = load_past()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=NEWS_WINDOW_HOURS)
    seen_links, seen_titles, items, errors = set(), set(), [], 0
    suppressed = []      # L2/L3 判定的疑似重复：连同判据一并输出，不静默丢弃
    dropped_l1 = 0       # L1 精确命中：确定重复，静默丢弃

    for q, hl, gl, ceid in QUERIES:
        try:
            for it in parse_items(fetch_rss(q, hl, gl, ceid)):
                if not it["title"] or not it["link"]:
                    continue
                if it["dt"] is None or it["dt"] < cutoff:
                    continue
                nt = norm_title(it["title"])

                # L1 精确重复（本次已收 / 历史简报已报道）
                if it["link"] in seen_links or nt in seen_titles:
                    continue
                if it["link"] in past_links:
                    dropped_l1 += 1
                    continue

                # L2 与历史简报标题的字符级相似度：抓「同一新闻换了跳转 URL」
                sim, matched = best_past_match(nt, past_titles)
                if sim >= TITLE_SIM_THRESHOLD:
                    it["reason"] = (f"L2 与历史简报标题相似度 {sim:.2f} "
                                    f"≥ {TITLE_SIM_THRESHOLD}")
                    it["matched"] = matched
                    suppressed.append(it)
                    continue

                # L3 同来源同日 + 拉丁词块高度重合：抓「同一新闻的中英文两条」
                sig = signature(it["title"])
                hit = None
                for kept in items:
                    if kept["source"] != it["source"] or day_key(kept) != day_key(it):
                        continue
                    ksig = signature(kept["title"])
                    c = containment(sig, ksig)
                    if c >= SIGNATURE_CONTAINMENT and (sig & ksig):
                        hit = (kept, c, sig & ksig)
                        break
                if hit:
                    kept, c, shared = hit
                    it["reason"] = (f"L3 同来源同日、关键词块重合度 {c:.2f}"
                                    f"（{'、'.join(sorted(shared))}）")
                    it["matched"] = kept["title"]
                    suppressed.append(it)
                    continue

                seen_links.add(it["link"])
                seen_titles.add(nt)
                items.append(it)
        except Exception as ex:
            errors += 1
            sys.stderr.write(f"[warn] 查询失败 {q!r}: {ex}\n")

    items.sort(key=lambda x: x["dt"], reverse=True)
    top = items[:NEWS_TOP_N]

    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    lines = [f"# 行业与商业动态候选 {today}（共 {len(top)} 条，窗口 {NEWS_WINDOW_HOURS}h）\n"]
    lines.append(f"> 去重记录：L1 精确命中历史链接 {dropped_l1} 条（确定重复，已丢弃）；"
                 f"L2/L3 判定疑似重复 {len(suppressed)} 条（判据见文末，可人工推翻）。\n")
    if errors == len(QUERIES):
        lines.append("全部查询失败：网络不可达或 news.google.com 未放行，请如实报告。")
    elif not top:
        lines.append("窗口期内无新的相关行业动态。")
    for i, it in enumerate(top, 1):
        d = it["dt"].astimezone(LOCAL_TZ).strftime("%Y-%m-%d") if it["dt"] else "未知"
        lines.append(f"## {i}. {it['title']}")
        lines.append(f"- 来源: {it['source'] or '未知'}  |  日期: {d}")
        lines.append(f"- 链接: {it['link']}")
        lines.append(f"- RSS摘要片段: {it['snippet']}\n")

    if suppressed:
        lines.append("\n---\n")
        lines.append("## 附：被 L2/L3 判为疑似重复而抑制的条目\n")
        lines.append("以下条目**未计入上方候选**。判据是启发式的，若发现误判请直接采用。\n")
        for i, it in enumerate(suppressed, 1):
            d = it["dt"].astimezone(LOCAL_TZ).strftime("%Y-%m-%d") if it["dt"] else "未知"
            lines.append(f"### S{i}. {it['title']}")
            lines.append(f"- 来源: {it['source'] or '未知'}  |  日期: {d}")
            lines.append(f"- 链接: {it['link']}")
            lines.append(f"- 抑制判据: {it['reason']}")
            lines.append(f"- 命中的既有条目: {it['matched']}\n")

    with open("news_am.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"完成：{len(top)} 条动态（失败查询 {errors}/{len(QUERIES)}；"
          f"L1 丢弃 {dropped_l1} 条，L2/L3 抑制 {len(suppressed)} 条），已写入 news_am.md")


if __name__ == "__main__":
    main()
