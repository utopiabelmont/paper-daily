#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日 arXiv 论文抓取脚本 —— 傅里叶光学 / 精密测量 / 工业检测机器学习
仅用标准库，无需 pip 安装，适合放进云端 routine 的仓库直接运行。

流程：按分类查 arXiv API → 时间窗过滤 → 相关度打分 → 按 arXiv ID 去重
      → 输出 Top-N 的 JSON + Markdown，交给模型做总结。

前提：routine 的云端环境需放行 export.arxiv.org（Network access 设为 Custom/Full）。
"""

import json
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# ---------------- 配置区（按需修改）----------------
CATEGORIES = ["physics.optics", "eess.IV", "cs.CV", "eess.SP"]

# 服务器端窄化：只有摘要/标题命中这些词的才返回，大幅减少无关结果
SERVER_TERMS = [
    'abs:"edge localization"', 'abs:"defect detection"',
    'abs:"surface inspection"', 'abs:"industrial inspection"',
    'abs:"optical inspection"', 'abs:"semiconductor inspection"',
    'abs:"Fourier optics"', 'abs:"spatial frequency"',
    'abs:metrology', 'abs:subpixel', 'abs:"sub-pixel"',
    'ti:"edge detection"',
]

# 客户端相关度打分词典（小写）；标题命中权重更高
KEYWORDS = [
    "edge localization", "edge detection", "defect detection",
    "surface inspection", "industrial inspection", "optical inspection",
    "metrology", "fourier optics", "wave optics", "spatial frequency",
    "frequency filtering", "subpixel", "sub-pixel", "sub-micron", "submicron",
    "semiconductor", "wafer", "precision measurement", "dimensional measurement",
    "cnn", "convolutional", "deep learning", "machine learning",
]

WINDOW_HOURS = 48          # 时间窗：只保留最近 N 小时提交的论文
MAX_RESULTS_PER_PAGE = 100 # 每页拉取数
MAX_PAGES = 2              # 最多翻几页（配合服务器端窄化，2 页足够）
TOP_N = 10                 # 最终保留篇数
API = "http://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
# ----------------------------------------------------


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
    req = urllib.request.Request(url, headers={"User-Agent": "daily-digest/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def parse_entries(xml_bytes):
    root = ET.fromstring(xml_bytes)
    out = []
    for e in root.findall(f"{ATOM}entry"):
        raw_id = e.findtext(f"{ATOM}id", "")            # http://arxiv.org/abs/2607.01234v2
        base_id = raw_id.split("/abs/")[-1].split("v")[0]
        title = " ".join((e.findtext(f"{ATOM}title") or "").split())
        summary = " ".join((e.findtext(f"{ATOM}summary") or "").split())
        published = e.findtext(f"{ATOM}published", "")
        authors = [a.findtext(f"{ATOM}name", "") for a in e.findall(f"{ATOM}author")]
        prim = e.find(f"{ARXIV}primary_category")
        category = prim.get("term") if prim is not None else ""
        link = raw_id  # abs 页链接
        out.append({
            "arxiv_id": base_id, "title": title, "summary": summary,
            "published": published, "authors": authors[:6],
            "primary_category": category, "link": link,
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
    seen, collected = set(), []
    for page in range(MAX_PAGES):
        try:
            entries = parse_entries(fetch_page(page * MAX_RESULTS_PER_PAGE))
        except Exception as ex:
            sys.stderr.write(f"[warn] 第 {page} 页抓取失败: {ex}\n")
            continue
        if not entries:
            break
        for p in entries:
            if p["arxiv_id"] in seen:          # 按 arXiv ID 去重（含跨分类交叉列出）
                continue
            if not within_window(p["published"]):
                continue
            seen.add(p["arxiv_id"])
            p["score"] = relevance_score(p)
            if p["score"] > 0:
                collected.append(p)

    collected.sort(key=lambda x: (x["score"], x["published"]), reverse=True)
    top = collected[:TOP_N]

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with open("papers.json", "w", encoding="utf-8") as f:
        json.dump({"date": today, "count": len(top), "papers": top},
                  f, ensure_ascii=False, indent=2)

    # 给模型看的紧凑清单（不含总结，总结由 routine 生成）
    lines = [f"# arXiv 候选论文 {today}（共 {len(top)} 篇）\n"]
    if not top:
        lines.append("今日无匹配新论文。")
    for i, p in enumerate(top, 1):
        lines.append(f"## {i}. {p['title']}")
        lines.append(f"- arXiv: {p['arxiv_id']}  |  分类: {p['primary_category']}  |  相关度: {p['score']}")
        lines.append(f"- 作者: {', '.join(p['authors'])}")
        lines.append(f"- 链接: {p['link']}")
        lines.append(f"- 摘要原文: {p['summary']}\n")
    with open("candidates.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"完成：{len(top)} 篇候选，已写入 papers.json 与 candidates.md")


if __name__ == "__main__":
    main()
