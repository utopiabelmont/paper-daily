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
"""

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

LOCAL_TZ = timezone(timedelta(hours=9))
DEDUP_DIRS = ["digests", "digests_am"]          # 两个方向共用，互相防重复
API = "http://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
ARXIV_ID_RE = re.compile(r"\b(\d{4}\.\d{4,5})\b")

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
    req = urllib.request.Request(url, headers={"User-Agent": "daily-digest/2.0"})
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


def within_window(published, window_hours):
    try:
        dt = datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return dt >= datetime.now(timezone.utc) - timedelta(hours=window_hours)


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
            if not within_window(p["published"], cfg["window_hours"]):
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

    lines = [f"# {cfg['label']} {today}（共 {len(top)} 篇，已跳过 {skipped_past} 篇历史重复）\n"]
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
          f"跳过历史重复 {skipped_past}），已写入 {cfg['out_json']} 与 {cfg['out_md']}")


if __name__ == "__main__":
    main()
