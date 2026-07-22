#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交叉方向每日 arXiv 抓取 —— 激光增材制造在线监测 × 光学精密测量/机器学习
（对应邢飞团队方向交叉：熔池监测/DED/激光超声NDT/PIML/闭环控制等 7 个子方向）

流程：读取历史简报已报道的 arXiv ID → 查 arXiv API → 时间窗过滤 → 剔除历史重复
      → 相关度打分 → 输出 papers_am.json + candidates_am.md 交给模型总结。

跨天去重：默认同时扫描 digests_am/ 与 digests/ 两个文件夹，
          避免同一篇论文在两条推送里重复出现。
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# ---------------- 配置区 ----------------
LOCAL_TZ = timezone(timedelta(hours=9))   # JST，仅用于日期标签

DEDUP_AGAINST_PAST = True
DEDUP_DIRS = ["digests_am", "digests"]    # 扫描这些文件夹里的历史简报做跨天去重

# 覆盖增材/材料/视觉/信号/控制/光学，配合下方词表做服务器端窄化
CATEGORIES = ["physics.app-ph", "cond-mat.mtrl-sci", "eess.IV",
              "cs.CV", "eess.SY", "physics.optics"]

SERVER_TERMS = [
    'abs:"additive manufacturing"', 'abs:"directed energy deposition"',
    'abs:"powder bed fusion"', 'abs:"selective laser melting"',
    'abs:"laser metal deposition"', 'abs:"laser cladding"',
    'abs:"melt pool"', 'abs:"laser ultrasonic"', 'abs:"laser welding"',
    'abs:"process monitoring"', 'abs:"in-situ monitoring"',
    'abs:"in situ monitoring"', 'abs:"physics-informed neural"',
    'abs:"nondestructive"', 'abs:"non-destructive"',
]

# 客户端打分：标题命中 +3，摘要命中 +1
KEYWORDS = [
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
]

WINDOW_HOURS = 72
MAX_RESULTS_PER_PAGE = 100
MAX_PAGES = 3
TOP_N = 12
API = "http://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
ARXIV_ID_RE = re.compile(r"\b(\d{4}\.\d{4,5})\b")
# ----------------------------------------


def load_past_reported_ids():
    ids = set()
    if not DEDUP_AGAINST_PAST:
        return ids
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


def build_search_query():
    cat_q = " OR ".join(f"cat:{c}" for c in CATEGORIES)
    term_q = " OR ".join(SERVER_TERMS)
    return f"({cat_q}) AND ({term_q})"


def fetch_page(start):
    params = {
        "search_query": build_search_query(),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "start": start,
        "max_results": MAX_RESULTS_PER_PAGE,
    }
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "daily-digest-am/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


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


def within_window(published):
    try:
        dt = datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return dt >= datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)


def relevance_score(paper):
    title = paper["title"].lower()
    summary = paper["summary"].lower()
    score = 0
    for kw in KEYWORDS:
        if kw in title:
            score += 3
        if kw in summary:
            score += 1
    return score


def main():
    past = load_past_reported_ids()
    seen, collected, skipped_past = set(), [], 0
    for page in range(MAX_PAGES):
        try:
            entries = parse_entries(fetch_page(page * MAX_RESULTS_PER_PAGE))
        except Exception as ex:
            sys.stderr.write(f"[warn] 第 {page} 页抓取失败: {ex}\n")
            continue
        if not entries:
            break
        for p in entries:
            if p["arxiv_id"] in seen:
                continue
            if not within_window(p["published"]):
                continue
            seen.add(p["arxiv_id"])
            if p["arxiv_id"] in past:
                skipped_past += 1
                continue
            p["score"] = relevance_score(p)
            if p["score"] > 0:
                collected.append(p)

    collected.sort(key=lambda x: (x["score"], x["published"]), reverse=True)
    top = collected[:TOP_N]

    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    with open("papers_am.json", "w", encoding="utf-8") as f:
        json.dump({"date": today, "count": len(top), "papers": top},
                  f, ensure_ascii=False, indent=2)

    lines = [f"# 交叉方向候选论文 {today}（共 {len(top)} 篇，已跳过 {skipped_past} 篇历史重复）\n"]
    if not top:
        lines.append("今日无匹配新论文。")
    for i, p in enumerate(top, 1):
        lines.append(f"## {i}. {p['title']}")
        lines.append(f"- arXiv: {p['arxiv_id']}  |  分类: {p['primary_category']}  |  相关度: {p['score']}")
        lines.append(f"- 作者: {', '.join(p['authors'])}")
        lines.append(f"- 链接: {p['link']}")
        lines.append(f"- 摘要原文: {p['summary']}\n")
    with open("candidates_am.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"完成：{len(top)} 篇候选（{today}，窗口 {WINDOW_HOURS}h，跳过历史重复 {skipped_past}）")


if __name__ == "__main__":
    main()
