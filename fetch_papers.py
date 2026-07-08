#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日 arXiv 论文抓取脚本 —— 傅里叶光学 / 精密测量 / 工业检测机器学习
仅用标准库，无需 pip 安装，适合放进云端 routine 的仓库直接运行。

流程：读取历史简报里已报道过的 arXiv ID → 按分类查 arXiv API → 时间窗过滤
      → 剔除历史已报道 → 相关度打分 → 单次内去重 → 输出 Top-N 的 JSON + Markdown。

跨天去重：扫描 digests/*.md 里出现过的 arXiv ID，今天不再重复推送（DEDUP_AGAINST_PAST）。
调数量：WINDOW_HOURS（时间窗）、SERVER_TERMS（服务器端窄化，越少越宽）、TOP_N（上限）。

前提：routine 云端环境需放行 export.arxiv.org。
日期标签按本地时区（JST, UTC+9）计算，时间窗按 UTC 比对（arXiv 时间就是 UTC）。
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# ---------------- 配置区（按需修改）----------------
LOCAL_TZ = timezone(timedelta(hours=9))   # 日本时区 JST（UTC+9），仅用于给简报打日期标签

DEDUP_AGAINST_PAST = True   # True=剔除历史简报已报道过的论文；想恢复"允许重看"就设为 False
DIGEST_DIR = "digests"

CATEGORIES = ["physics.optics", "eess.IV", "cs.CV", "eess.SP"]

# 服务器端窄化：摘要/标题命中任一即返回。词表越长越宽、召回越多。
SERVER_TERMS = [
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
]

# 客户端相关度打分词典（小写）；标题命中权重更高。用于给召回结果排序。
KEYWORDS = [
    "edge localization", "edge detection", "defect detection", "surface defect",
    "anomaly detection", "surface inspection", "industrial inspection",
    "visual inspection", "optical inspection", "semiconductor", "wafer",
    "fourier optics", "wave optics", "spatial frequency", "frequency filtering",
    "phase retrieval", "computational imaging", "super-resolution",
    "point spread function", "wavefront", "diffraction", "interferometry",
    "metrology", "profilometry", "subpixel", "sub-pixel", "sub-micron", "submicron",
    "precision measurement", "dimensional measurement",
    "cnn", "convolutional", "deep learning", "machine learning",
]

WINDOW_HOURS = 72          # 时间窗：最近 N 小时（按 UTC 比对）
MAX_RESULTS_PER_PAGE = 100
MAX_PAGES = 3
TOP_N = 12                 # 最终保留篇数上限
API = "http://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
ARXIV_ID_RE = re.compile(r"\b(\d{4}\.\d{4,5})\b")   # 匹配 2607.04675 这类新式 arXiv ID
# ----------------------------------------------------


def load_past_reported_ids():
    """扫描历史 digests/*.md，收集所有已报道过的 arXiv ID（不含版本号）。"""
    ids = set()
    if not (DEDUP_AGAINST_PAST and os.path.isdir(DIGEST_DIR)):
        return ids
    for fn in os.listdir(DIGEST_DIR):
        if fn.endswith(".md"):
            try:
                with open(os.path.join(DIGEST_DIR, fn), encoding="utf-8") as f:
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
            if p["arxiv_id"] in seen:          # 单次运行内去重
                continue
            if not within_window(p["published"]):
                continue
            seen.add(p["arxiv_id"])
            if p["arxiv_id"] in past:           # 跨天去重：历史已报道过的跳过
                skipped_past += 1
                continue
            p["score"] = relevance_score(p)
            if p["score"] > 0:
                collected.append(p)

    collected.sort(key=lambda x: (x["score"], x["published"]), reverse=True)
    top = collected[:TOP_N]

    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")   # 用本地(JST)日期打标签
    with open("papers.json", "w", encoding="utf-8") as f:
        json.dump({"date": today, "count": len(top), "papers": top},
                  f, ensure_ascii=False, indent=2)

    lines = [f"# arXiv 候选论文 {today}（共 {len(top)} 篇，已跳过 {skipped_past} 篇历史重复）\n"]
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

    print(f"完成：{len(top)} 篇候选（日期 {today}，窗口 {WINDOW_HOURS}h，"
          f"跳过历史重复 {skipped_past} 篇），已写入 papers.json 与 candidates.md")


if __name__ == "__main__":
    main()
