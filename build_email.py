#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把当日简报做成邮件正文 HTML：开头一张概览表，后面是图卡，最后是备选清单。

用法: python build_email.py <简报 md> <图卡 html> <输出 html>

- 图卡直接取 build_html.py 生成的当日图卡页，去掉「术语速查」折叠块（邮件客户端
  大多不支持 <details>，会整段展开），术语留在附件里。
- 样式用 premailer 内联到每个元素上：Gmail（非 Google 账号收信时）和部分 Outlook
  会丢掉 <style>，内联后仍能保持排版。premailer 缺失时退回只保留 <style>。
- 邮件客户端普遍不支持 flex，方法色块改为上下排列。
"""

import html
import re
import sys

CARD_SPLIT_RE = re.compile(r'(?=<div class="card">)')
DETAILS_RE = re.compile(r"<details>.*?</details>", re.S)
TAG_RE = re.compile(r"<[^>]+>")
SCORE_RE = re.compile(r"评分\s*(\d+)")
REL_RE = re.compile(r"相关度\s*(\d+)")
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
BARE_URL_RE = re.compile(r"(?<![\"'(>])(https?://[^\s)\]<>，、｜|]+)")

CSS = """
body{margin:0;padding:0;background:#FAF9F5;color:#2C2C2A;
     font-family:-apple-system,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;line-height:1.6}
.wrap{max-width:640px;margin:0 auto;padding:20px 14px}
h1{font-size:19px;font-weight:600;margin:0 0 4px;color:#2C2C2A}
.stats{font-size:12px;color:#6B6A63;margin:0 0 16px}
.sec{font-size:13px;font-weight:600;color:#6B6A63;margin:22px 0 8px;letter-spacing:.04em}
table.ov{width:100%;border-collapse:collapse;background:#ffffff;border:1px solid #E3E1D9;border-radius:10px}
table.ov td{padding:9px 10px;border-top:1px solid #EFEDE6;font-size:13px;vertical-align:top;color:#2C2C2A}
table.ov tr:first-child td{border-top:0}
td.no{width:18px;color:#888780;font-variant-numeric:tabular-nums}
td.ti a{color:#185FA5;text-decoration:none;font-weight:600}
td.ti .q{display:block;color:#5F5E57;font-size:12px;font-weight:400;margin-top:2px;background:none;padding:0}
td.sc{width:92px;white-space:nowrap}
table.bar{border-collapse:collapse;width:60px;height:6px}
td.bar-on{background:#1F5F8B;padding:0;height:6px;border:0}
td.bar-off{background:#E3E1D9;padding:0;height:6px;border:0}
.num{font-size:12px;color:#2C2C2A;font-variant-numeric:tabular-nums}
.card{background:#ffffff;border:1px solid #E3E1D9;border-radius:12px;padding:16px 16px;margin-bottom:14px}
.meta{margin-bottom:8px;font-size:12px}
.pill{background:#E6F1FB;color:#0C447C;border-radius:12px;padding:2px 10px;margin-right:8px}
.meta a{color:#185FA5;text-decoration:none}
.card h2{font-size:16px;font-weight:600;margin:0 0 2px;color:#2C2C2A}
.en{color:#888780;font-size:12px;margin-bottom:10px}
a.blk{display:block;text-decoration:none;color:inherit}
.q{background:#F1EFE8;color:#444441;border-radius:8px;padding:8px 12px;font-size:13px;margin-bottom:8px}
.methods{margin-bottom:2px}
.m{background:#EEEDFE;color:#3C3489;border-radius:8px;padding:8px 10px;font-size:13px;font-weight:600;margin-bottom:6px}
.m span{display:block;font-weight:400;font-size:12px;color:#534AB7;margin-top:2px}
.res{border-radius:8px;padding:8px 12px;font-size:13px;font-weight:600;margin-bottom:6px}
.res span{display:block;font-weight:400;font-size:12px;margin-top:2px}
.res.good{background:#E1F5EE;color:#085041}
.res.good span{color:#0F6E56}
.res.warn{background:#FAECE7;color:#712B13}
.res.warn span{color:#993C1D}
.res.bad{background:#FCEBEB;color:#791F1F}
.res.bad span{color:#A32D2D}
.conc{background:#FAEEDA;color:#633806;border-radius:8px;padding:8px 12px;font-size:13px;margin-top:2px}
.news{background:#E6F1FB;color:#0C447C;border-radius:8px;padding:8px 12px;font-size:13px}
ul.short{margin:0;padding:0 0 0 18px;font-size:13px;color:#2C2C2A}
ul.short li{margin-bottom:6px}
ul.short a{color:#185FA5;text-decoration:none}
.foot{color:#8A8880;font-size:12px;margin-top:20px;line-height:1.7}
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
    shortlist, in_short = [], False
    for l in lines:
        if re.match(r"^#{2,4}\s", l):
            in_short = "备选" in l
            continue
        if in_short and re.match(r"^\s*[-*]\s+", l):
            item = re.sub(r"^\s*[-*]\s+", "", l).strip()
            if item and item != "无" and item != "无。":
                shortlist.append(item)
    return title, stats, shortlist


def parse_cards(page):
    start = page.find("</div>", page.find('<div class="date">'))
    end = page.find('<div class="foot">')
    body = page[start + 6 if start >= 0 else 0: end if end >= 0 else len(page)]
    body = DETAILS_RE.sub("", body)
    cards = [c.strip() for c in CARD_SPLIT_RE.split(body) if c.strip().startswith('<div class="card">')]
    rows = []
    for c in cards:
        pill = TAG_RE.sub("", (re.search(r'class="pill">(.*?)</span>', c, re.S) or [None, ""])[1]).strip()
        if "行业动态" in pill or "无入选" in pill:
            continue
        h2 = TAG_RE.sub("", (re.search(r"<h2>(.*?)</h2>", c, re.S) or [None, ""])[1]).strip()
        link = (re.search(r'<div class="meta">.*?<a href="([^"]+)"', c, re.S) or [None, ""])[1]
        q = TAG_RE.sub("", (re.search(r'<div class="q">(.*?)</div>', c, re.S) or [None, ""])[1]).strip()
        q = re.sub(r"^研究问题[：:]\s*", "", q)
        m = SCORE_RE.search(pill)
        kind, val = ("评分", int(m.group(1))) if m else ("相关度", int(REL_RE.search(pill).group(1))) \
            if REL_RE.search(pill) else ("", None)
        rows.append({"title": h2, "link": link, "q": q, "kind": kind, "val": val})
    return "\n".join(cards), rows


def overview(rows):
    out = ['<table class="ov" role="presentation">']
    for i, r in enumerate(rows, 1):
        if r["kind"] == "评分":
            on = max(0, min(100, r["val"]))
            bar = (f'<table class="bar" role="presentation"><tr>'
                   f'<td class="bar-on" width="{on * 60 // 100}"></td>'
                   f'<td class="bar-off" width="{60 - on * 60 // 100}"></td></tr></table>')
            sc = f'{bar}<span class="num">评分 {r["val"]}</span>'
        elif r["kind"]:
            sc = f'<span class="num">相关度 {r["val"]}</span>'
        else:
            sc = ""
        title = html.escape(r["title"], quote=False)
        title = f'<a href="{r["link"]}">{title}</a>' if r["link"] else title
        q = f'<span class="q">{html.escape(r["q"], quote=False)}</span>' if r["q"] else ""
        out.append(f'<tr><td class="no">{i}</td><td class="ti">{title}{q}</td><td class="sc">{sc}</td></tr>')
    out.append("</table>")
    return "\n".join(out)


def build(md_text, page):
    title, stats, shortlist = parse_md(md_text)
    cards, rows = parse_cards(page)
    parts = [f"<h1>{html.escape(title, quote=False)}</h1>"]
    if stats:
        parts.append('<p class="stats">' + "<br>".join(md_inline(s) for s in stats) + "</p>")
    if rows:
        parts.append(f'<div class="sec">今日概览 · {len(rows)} 篇</div>')
        parts.append(overview(rows))
    if cards:
        parts.append('<div class="sec">详细图卡</div>')
        parts.append(cards)
    if shortlist:
        parts.append(f'<div class="sec">备选（未达门槛） · {len(shortlist)} 篇</div>')
        parts.append('<ul class="short">' + "".join(f"<li>{md_inline(s)}</li>" for s in shortlist) + "</ul>")
    parts.append('<div class="foot">点击色块可跳转 claude.ai 继续追问。术语速查与完整文字版见附件。<br>'
                 '色块：紫＝方法　绿＝达标　橙＝背离或意外　红＝失效　黄＝结论</div>')
    doc = ("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
           "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
           "<meta name=\"color-scheme\" content=\"light\"><meta name=\"supported-color-schemes\" content=\"light\">"
           f"<title>{html.escape(title, quote=False)}</title><style>{CSS}</style></head>"
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
    out = build(md_text, page)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"已生成 {out_path}")


if __name__ == "__main__":
    main()
