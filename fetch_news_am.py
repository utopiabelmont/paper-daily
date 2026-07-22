#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交叉方向行业/商业动态抓取 —— 基于 Google News RSS（无需 API key，仅标准库）。
覆盖：增材制造在线监测的商用系统、公司动态、产业化新闻（中英双语查询）。

流程：多条查询拉 RSS → 时间窗过滤 → 按链接对历史简报去重 → 按日期排序
      → 输出 news_am.md 交给模型翻译/概述。

前提：routine 环境需能访问 news.google.com（Network access 为 Full 即可）。
"""

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
URL_RE = re.compile(r"https?://\S+")


def load_past_links():
    links = set()
    for d in DEDUP_DIRS:
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if fn.endswith(".md"):
                try:
                    with open(os.path.join(d, fn), encoding="utf-8") as f:
                        links |= set(URL_RE.findall(f.read()))
                except Exception:
                    pass
    return links


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


def main():
    past_links = load_past_links()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=NEWS_WINDOW_HOURS)
    seen_links, seen_titles, items, errors = set(), set(), [], 0

    for q, hl, gl, ceid in QUERIES:
        try:
            for it in parse_items(fetch_rss(q, hl, gl, ceid)):
                if not it["title"] or not it["link"]:
                    continue
                if it["dt"] is None or it["dt"] < cutoff:
                    continue
                nt = norm_title(it["title"])
                if it["link"] in seen_links or nt in seen_titles:
                    continue
                if it["link"] in past_links:
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
    with open("news_am.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"完成：{len(top)} 条动态（失败查询 {errors}/{len(QUERIES)}），已写入 news_am.md")


if __name__ == "__main__":
    main()
