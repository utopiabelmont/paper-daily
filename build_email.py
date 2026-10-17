#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把当日简报做成邮件正文 HTML：开头是统计与速览表，后面是图卡、备选清单和行业动态。

用法: python build_email.py <简报 md> <图卡 html> <输出 html>

- 图卡与附件页共用 cards.py 的解析与版式（结论前置、要点表、评分条）。片段取自图卡页里的
  <template id="cards-src">；2026-10-17 之前生成的旧图卡页没有它，就从页面正文里取原卡片。
- 邮件里不放「术语速查」折叠块（邮件客户端大多不支持 <details>，会整段展开），术语留在附件里。
- 样式用 premailer 内联到每个元素上：Gmail（非 Google 账号收信时）和部分 Outlook
  会丢掉 <style>，内联后仍能保持排版。premailer 缺失时退回只保留 <style>。
- 邮件客户端普遍不支持 flex 和 CSS 变量，版式只用表格，颜色用浅色固定值。
"""

import html
import os
import re
import sys

import cards

SRC_RE = re.compile(r'<template id="cards-src">(.*?)</template>', re.S)
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
BARE_URL_RE = re.compile(r"(?<![\"'(>])(https?://[^\s)\]<>，、｜|]+)")

BASE_CSS = """
body{margin:0;padding:0;background:#FAF9F5;color:#22221F;
     font-family:-apple-system,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;line-height:1.6}
.wrap{max-width:640px;margin:0 auto;padding:20px 14px}
h1{font-size:19px;font-weight:600;margin:0 0 12px;color:#22221F}
.stats{font-size:12px;color:#6B6A63;margin:0 0 12px}
ul.short{margin:0;padding:0 0 0 18px;font-size:13px;color:#22221F}
ul.short li{margin-bottom:6px;line-height:1.6}
ul.short a{color:#1C5CAB;text-decoration:none}
.foot{margin-top:24px;border-top:1px solid #EFEDE6;padding-top:12px}
.card{background:#ffffff;border:1px solid #E3E1D9;border-radius:12px;padding:16px 16px;margin-bottom:14px}
.card .meta{margin-bottom:8px;font-size:12px}
.card .pill{background:#F1EFE8;color:#55544E;border-radius:12px;padding:2px 10px;margin-right:8px}
.card .meta a{color:#1C5CAB;text-decoration:none}
.card h2{font-size:16px;font-weight:600;margin:0 0 2px;color:#22221F}
.card .en{color:#8A8880;font-size:12px;margin-bottom:10px}
.card a.blk{display:block;text-decoration:none;color:inherit}
.card .q,.card .m,.card .res,.card .conc,.card .news{background:#F1EFE8;color:#22221F;border-radius:8px;
     padding:8px 12px;font-size:13px;margin-bottom:6px}
.card .m span,.card .res span{display:block;font-size:12px;color:#55544E;margin-top:2px}
"""



def md_inline(s):
    """备选清单里的一行 Markdown 转成安全的 HTML：只处理链接、裸网址和加粗。"""
    links = []

    def keep(m):
        links.append((m.group(1), m.group(2)))
        return f"\x00{len(links) - 1}\x00"

    s = MD_LINK_RE.sub(keep, s)
    s = html.escape(s, quote=False)

    def bare(m):
        url = m.group(1)
        aid = re.search(r"arxiv\.org/abs/(\d{4}\.\d{4,5})", url)
        return f'<a href="{url}">{"arXiv " + aid.group(1) if aid else url}</a>'

    s = BARE_URL_RE.sub(bare, s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    return re.sub(r"\x00(\d+)\x00", lambda m: '<a href="{1}">{0}</a>'.format(
        html.escape(links[int(m.group(1))][0], quote=False), links[int(m.group(1))][1]), s)


def parse_md(text):
    lines = text.splitlines()
    title = next((l.lstrip("# ").strip() for l in lines if l.startswith("# ")), "每日简报")
    stats = [l.lstrip("> ").strip() for l in lines if re.match(r"^>\s*精选", l)]
    # 统计行之下、第一个二级标题之前的正文段落：抓取或索引异常的说明
    notes, started = [], False
    for l in lines:
        if l.startswith("## "):
            break
        if l.startswith("# "):
            started = True
        elif started and l.strip() and not l.startswith(">"):
            notes.append(l.strip())
    shortlist, in_short = [], False
    for l in lines:
        if re.match(r"^#{2,4}\s", l):
            in_short = "备选" in l
            continue
        if in_short and re.match(r"^\s*[-*]\s+", l):
            item = re.sub(r"^\s*[-*]\s+", "", l).strip()
            if item and item != "无" and item != "无。":
                shortlist.append(item)
    return title, stats, notes, shortlist


def fragment_of(page):
    """图卡页里的原始片段；旧图卡页没有 <template>，就取标题区与页脚之间的正文。"""
    m = SRC_RE.search(page)
    if m:
        return m.group(1)
    start = page.find("</div>", page.find('<div class="date">'))
    end = page.find('<div class="foot')
    return page[start + 6 if start >= 0 else 0: end if end >= 0 else len(page)]


def build(md_text, page, profile="main", date=""):
    title, stats, notes, shortlist = parse_md(md_text)
    scores = cards.load_scores(profile, date) if date else {}
    parts = [f"<h1>{html.escape(title, quote=False)}</h1>"]
    kpi = cards.stats_of(scores)
    if kpi:
        parts.append(cards.kpi_row(kpi))
    elif stats:
        parts.append('<p class="stats">' + "<br>".join(md_inline(s) for s in stats) + "</p>")
    parts += [f'<p class="note">{md_inline(n)}</p>' for n in notes]
    short_html = ""
    if shortlist:
        short_html = (f'<div class="sec">备选（未达门槛） · {len(shortlist)} 篇</div>'
                      '<ul class="short">' + "".join(f"<li>{md_inline(s)}</li>" for s in shortlist) + "</ul>")
    parts.append(cards.render(fragment_of(page), scores, page=False, shortlist_html=short_html))
    parts.append(f'<div class="foot legend">{cards.legend()}<br>术语速查与完整文字版见附件。</div>')
    doc = ("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
           "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
           "<meta name=\"color-scheme\" content=\"light\"><meta name=\"supported-color-schemes\" content=\"light\">"
           f"<title>{html.escape(title, quote=False)}</title>"
           f"<style>{BASE_CSS}{cards.css(cards.LIGHT)}</style></head>"
           f"<body><div class=\"wrap\">{''.join(parts)}</div></body></html>")
    try:
        import logging
        from premailer import Premailer
        return Premailer(doc, keep_style_tags=True, remove_classes=False, strip_important=False,
                         disable_validation=True, cssutils_logging_level=logging.CRITICAL).transform()
    except ImportError:
        sys.stderr.write("[warn] 未安装 premailer，只保留 <style>，部分邮件客户端会丢失样式\n")
        return doc


def main():
    if len(sys.argv) != 4:
        sys.exit("用法: python build_email.py <简报 md> <图卡 html> <输出 html>")
    md_path, page_path, out_path = sys.argv[1:]
    with open(md_path, encoding="utf-8") as f:
        md_text = f.read()
    try:
        with open(page_path, encoding="utf-8") as f:
            page = f.read()
    except FileNotFoundError:
        page = ""
    date = os.path.basename(md_path).replace(".md", "")
    out = build(md_text, page, cards.profile_of(md_path), date)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"已生成 {out_path}")


if __name__ == "__main__":
    main()
