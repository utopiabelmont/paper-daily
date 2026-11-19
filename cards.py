#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图卡片段的解析与重排，build_html.py（附件页）和 build_email.py（邮件正文）共用。

定时任务按固定类名写卡片片段（见 selection/routines/*.md 第 7 步），片段格式不变；
这里把它解析成字段，再按成熟的阅读版式重排（2026-10-17）：
  - 结论放在标题下第一块（BLUF / 倒金字塔），扫一眼就知道这篇与自己有没有关系；
  - 问题、方法、结果排成带左侧标签的要点表，仿 JAMA「Key Points」的 Question–Findings–Meaning，
    取代原来七八个等宽色块的堆叠；
  - 颜色只留给状态，且配 ✓ ! ✗ 图标，不靠颜色单独表意（Tufte 的 data-ink、WCAG 1.4.1）；
  - 评分画成同色系进度条（meter），分项五格只高亮贴近度（emphasis：高亮一项、其余置灰）。
版式只用表格和可内联的样式，邮件客户端也能显示；解析不了的卡片原样输出，不丢内容。
"""

import html
import json
import os
import re
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "selection_log")
CARD_SPLIT_RE = re.compile(r'(?=<div class="card">)')
SCORE_RE = re.compile(r"(?:评分|相关度)\s*(\d+)")
PILL_SCORE_RE = re.compile(r"\s*[·・|｜]?\s*(?:评分|相关度)\s*\d+\s*$")
VOID = {"br", "img", "hr", "meta", "input", "wbr", "link"}
INLINE_KEEP = {"b", "strong", "i", "em", "sub", "sup", "code", "br"}
# 分项按对读者的重要程度排：贴近度最先、且唯一高亮
AXES = (("fit", "贴近"), ("act", "迁移"), ("sig", "份量"), ("nov", "新意"), ("cred", "证据"))
STATUS = {"good": "✓", "warn": "!", "bad": "✗", "info": "–"}  # info：片段没写等级时的中性标记

# 颜色令牌。邮件只用浅色值（多数客户端不支持 CSS 变量）；附件页用变量，并另给深色值。
LIGHT = {
    "bg": "#FAF9F5", "surface": "#FFFFFF", "border": "#E3E1D9", "rule": "#EFEDE6",
    "ink": "#22221F", "ink2": "#55544E", "ink3": "#8A8880", "accent": "#1C5CAB",
    "bar": "#2A78D6", "track": "#CDE2FB", "barmute": "#A9A79F", "trackmute": "#ECEBE5",
    "ledebg": "#FCF5E8", "ledebar": "#E09A2E", "tagbg": "#F1EFE8",
    "good": "#0CA30C", "warn": "#FAB219", "bad": "#D03B3B", "onstatus": "#FFFFFF", "onwarn": "#22221F",
}
DARK = {
    "bg": "#141413", "surface": "#1D1D1B", "border": "#34332F", "rule": "#2A2A27",
    "ink": "#F0EFEC", "ink2": "#C3C2B7", "ink3": "#8F8E86", "accent": "#86B6EF",
    "bar": "#3987E5", "track": "#1F3556", "barmute": "#77756E", "trackmute": "#2C2C29",
    "ledebg": "#2A2418", "ledebar": "#C98500", "tagbg": "#2A2A27",
    "good": "#0CA30C", "warn": "#FAB219", "bad": "#D03B3B", "onstatus": "#FFFFFF", "onwarn": "#22221F",
}

# 版式规则。$name 由 css() 换成颜色值或 var(--name)。只用表格、块级元素与行内样式，不用 flex。
CSS = """
.ov,.pc,.pc-head,.sc,.mt,.kp,.dm,.kpi{border-collapse:collapse;border-spacing:0}
.kpi{margin:0 0 6px}
.kpi-c{padding:0 30px 0 0;vertical-align:bottom}
.kpi-v{font-size:22px;font-weight:600;color:$ink;line-height:1.2}
.kpi-l{font-size:12px;color:$ink3}
.note{font-size:13px;color:$ink2;margin:8px 0 0}
.sec{font-size:13px;font-weight:600;color:$ink3;margin:26px 0 10px;letter-spacing:.04em}
.ov{width:100%;background:$surface;border:1px solid $border;border-radius:10px}
.ov-c{padding:10px 10px;vertical-align:top}
.sep{border-top:1px solid $rule}
.ov-no{width:22px;font-size:12px;color:$ink3;padding-top:12px}
.ov-ti a{font-size:14px;font-weight:600;color:$accent;text-decoration:none}
.ov-q{font-size:12px;color:$ink2;margin-top:2px;line-height:1.55}
.ov-sc{width:96px;padding-top:12px}
.sc-c{padding:0;vertical-align:middle}
.sc-num{font-size:13px;font-weight:600;color:$ink;padding-left:8px;white-space:nowrap}
.mt-on,.mt-off,.mt-mon,.mt-moff{padding:0;height:6px;line-height:0;font-size:0}
.mt-on{background:$bar}
.mt-off{background:$track}
.mt-mon{background:$barmute}
.mt-moff{background:$trackmute}
.pc{width:100%;background:$surface;border:1px solid $border;border-radius:12px;margin:0 0 16px}
.pc-in{padding:16px 18px 14px}
.pc-head{width:100%;margin:0 0 8px}
.ph-c{padding:0;vertical-align:middle}
.pc-no{font-size:12px;font-weight:600;color:$ink3;margin-right:8px}
.pc-tag{font-size:12px;color:$ink2;background:$tagbg;border-radius:10px;padding:2px 9px}
.pc-title{font-size:17px;font-weight:600;line-height:1.45;margin:0;color:$ink}
.pc-title a{color:$ink;text-decoration:none}
.pc-en{font-size:12px;color:$ink3;margin:3px 0 12px;line-height:1.5}
.pc-en a{color:$accent;text-decoration:none}
.lede{background:$ledebg;border-left:3px solid $ledebar;border-radius:0 8px 8px 0;padding:9px 12px;margin:0 0 12px}
.lede-l{font-size:12px;font-weight:600;color:$ink3;margin:0 0 2px}
.lede-t{font-size:15px;color:$ink;line-height:1.65}
.lede-r{font-size:13px;color:$ink2;line-height:1.6;margin-top:4px}
a.ask{color:inherit;text-decoration:none}
.go{color:$accent;font-size:12px;padding-left:3px}
.kp{width:100%}
.kp-k{width:44px;font-size:12px;font-weight:600;color:$ink3;padding:9px 0 7px;vertical-align:top}
.kp-v{font-size:14px;color:$ink;line-height:1.65;padding:7px 0;vertical-align:top}
.kp-li{margin:0 0 4px}
.kp-b{font-weight:600}
.kp-s{color:$ink2}
.dot{color:$ink3;padding-right:6px}
.st{display:inline-block;width:16px;height:16px;line-height:16px;border-radius:8px;text-align:center;
    font-size:11px;font-weight:700;margin-right:6px;vertical-align:1px}
.st-good{background:$good;color:$onstatus}
.st-warn{background:$warn;color:$onwarn}
.st-bad{background:$bad;color:$onstatus}
.st-info{background:$tagbg;color:$ink2}
.dm-c{padding:2px 14px 0 0;vertical-align:top}
.dm-l{font-size:12px;color:$ink3;white-space:nowrap;margin-bottom:3px}
.dm-l b{color:$ink;font-weight:600}
.dm-hi b{color:$accent}
.terms{margin-top:10px;border-top:1px solid $rule;padding-top:8px}
.terms summary{cursor:pointer;font-size:12px;color:$ink3}
.term{font-size:13px;color:$ink2;line-height:1.6;margin-top:6px}
.term b{color:$ink}
.nw-it{background:$surface;border:1px solid $border;border-radius:10px;padding:11px 14px;margin:0 0 10px}
.nw-t{font-size:14px;font-weight:600;color:$accent;text-decoration:none}
.nw-m{font-size:12px;color:$ink3;margin:2px 0 4px}
.nw-b{font-size:13px;color:$ink2;line-height:1.6}
.notice{background:$surface;border:1px dashed $border;border-radius:10px;padding:12px 14px;
        font-size:14px;color:$ink2;margin:0 0 10px}
.notice b{color:$ink}
.legend{font-size:12px;color:$ink3;line-height:1.8}
.legend .st{width:14px;height:14px;line-height:14px;font-size:10px;margin:0 3px 0 0}
"""


def css(tokens=None):
    """tokens 为空时输出 var(--name)，供附件页配合 :root 变量与深色模式使用。"""
    return re.sub(r"\$(\w+)", lambda m: tokens[m.group(1)] if tokens else f"var(--{m.group(1)})", CSS)


def css_vars():
    light = ";".join(f"--{k}:{v}" for k, v in LIGHT.items())
    dark = ";".join(f"--{k}:{v}" for k, v in DARK.items())
    return (f":root{{{light}}}\n"
            f"@media (prefers-color-scheme: dark){{:root:not([data-theme=\"light\"]){{{dark}}}}}\n"
            f":root[data-theme=\"dark\"]{{{dark}}}\n")


# ---------------- 解析 ----------------

class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs=(), parent=None):
        self.tag, self.attrs, self.children, self.parent = tag, dict(attrs), [], parent

    def classes(self):
        return (self.attrs.get("class") or "").split()


class _Builder(HTMLParser):
    """宽容的树构建：未闭合的标签随上层闭合一并结束，多余的闭合标签忽略。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = self.cur = Node("#root")

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, attrs, self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def parse_html(text):
    b = _Builder()
    b.feed(text)
    b.close()
    return b.root


def walk(node):
    for c in node.children:
        if isinstance(c, Node):
            yield c
            yield from walk(c)


def find(node, pred):
    return next((n for n in walk(node) if pred(n)), None) if node else None


def cls(name):
    return lambda n: name in n.classes()


def text_of(node):
    return "".join(c if isinstance(c, str) else text_of(c) for c in node.children) if node else ""


def inline(node, skip=None):
    """节点内容转成安全的行内 HTML：文本转义，只保留少数行内标签；内层链接去掉（外层已是追问链接）。"""
    if node is None:
        return ""
    out = []
    for c in node.children:
        if isinstance(c, str):
            out.append(html.escape(c, quote=False))
        elif c.tag in ("script", "style") or (skip and skip(c)):
            continue
        elif c.tag == "br":
            out.append("<br>")
        elif c.tag in INLINE_KEEP:
            out.append(f"<{c.tag}>{inline(c, skip)}</{c.tag}>")
        else:
            out.append(inline(c, skip))
    return re.sub(r"\s+", " ", "".join(out)).strip()


def safe_url(url):
    url = (url or "").strip()
    return html.escape(url, quote=True) if re.match(r"https?://", url, re.I) else ""


def href_of(node):
    while node is not None and node.tag != "#root":
        if node.tag == "a" and node.attrs.get("href"):
            return safe_url(node.attrs["href"])
        node = node.parent
    return ""


def item(node, prefix=None, split_span=True):
    """一个色块 → {main, sub, href}。split_span 时 <span> 是补充说明，单独取出。"""
    is_span = (lambda n: n.tag == "span") if split_span else None
    main = inline(node, is_span)
    if prefix:
        main = re.sub(prefix, "", main)
    span = find(node, lambda n: n.tag == "span") if split_span else None
    return {"main": main, "sub": inline(span), "href": href_of(node)}


def split_relevance(text):
    """结论里「；与我的方向……」之后的部分单独成行。"""
    for m in re.finditer(r"[；;]", text):
        if "我" in text[m.end():m.end() + 12]:
            return text[:m.start()].rstrip("。 "), text[m.end():].strip()
    return text, ""


def parse_card(raw):
    """一张卡片的 HTML → 字段。kind 为 paper / news / notice；认不出结构的标为 raw，原样输出。"""
    root = parse_html(raw)
    card = find(root, cls("card")) or root
    meta = find(card, cls("meta"))
    pill = " ".join(text_of(find(card, cls("pill"))).split())
    link = find(meta, lambda n: n.tag == "a")
    h2 = find(card, lambda n: n.tag == "h2")
    c = {"raw": raw, "pill": pill, "link": safe_url(link.attrs.get("href")) if link else "",
         "title": inline(h2), "en": inline(find(card, cls("en")))}
    body = inline(find(card, cls("news")))
    if "行业动态" in pill:
        return dict(c, kind="news", body=body)
    if "无入选" in pill or (h2 and not find(card, cls("q")) and body):
        return dict(c, kind="notice", body=body)
    blocks = [n for n in walk(card) if {"q", "m", "res", "conc"} & set(n.classes())]
    if not h2 or not blocks:
        return dict(c, kind="raw")
    q = find(card, cls("q"))
    conc = find(card, cls("conc"))
    c.update(
        kind="paper",
        q=item(q, r"^研究问题[：:]\s*", split_span=False) if q else None,
        methods=[item(n) for n in walk(card) if "m" in n.classes()],
        results=[dict(item(n), level=next((l for l in ("good", "warn", "bad") if l in n.classes()), "info"))
                 for n in walk(card) if "res" in n.classes()],
        conc=item(conc, r"^结论[：:]\s*", split_span=False) if conc else None,
        terms=[inline(n) for n in walk(card) if "term" in n.classes()],
    )
    m = SCORE_RE.search(pill)
    c["score"] = int(m.group(1)) if m else None
    c["tag"] = html.escape(PILL_SCORE_RE.sub("", pill).strip(), quote=False)
    aid = re.search(r"(\d{4}\.\d{4,5})", c["link"])
    c["arxiv_id"] = aid.group(1) if aid else ""
    return c


def split_cards(fragment):
    return [p.strip() for p in CARD_SPLIT_RE.split(fragment) if p.strip().startswith('<div class="card">')]


def profile_of(path):
    """digests_am/、digests_am_html/ 属交叉方向，其余属主方向。"""
    return "am" if os.path.basename(os.path.dirname(os.path.abspath(path))).startswith(("digests_am",)) \
        else "main"


def load_scores(profile, date):
    """读当日评分日志，按 arxiv_id 索引；没有日志时返回空表，图卡退回只用片段里的分数。"""
    out = {}
    try:
        with open(os.path.join(LOG_DIR, f"{date}-{profile}.jsonl"), encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    r = json.loads(line)
                    out[r["arxiv_id"]] = r
    except (OSError, ValueError, KeyError):
        pass
    return out


def stats_of(scores):
    if not scores:
        return None
    n = lambda k: sum(1 for r in scores.values() if r.get("decision") == k)
    return [(len(scores), "候选"), (n("blocked"), "预筛排除"), (n("selected"), "入选"), (n("shortlist"), "备选")]


# ---------------- 渲染 ----------------

def sep(i):
    """表格第二行起加分隔线。用显式类名而非 `.x td` 后代选择器：premailer 内联时
    后代选择器会连嵌套的评分条单元格一起命中。"""
    return " sep" if i else ""


def meter(val, width, muted=False):
    val = max(0, min(100, val))
    on = round(width * val / 100)
    a, b = ("mt-mon", "mt-moff") if muted else ("mt-on", "mt-off")
    cells = (f'<td class="{a}" width="{on}" height="6"></td>' if on else "") + \
            (f'<td class="{b}" width="{width - on}" height="6"></td>' if width - on else "")
    return (f'<table class="mt" role="presentation" cellpadding="0" cellspacing="0" width="{width}">'
            f'<tr>{cells}</tr></table>')


def score_of(c, rec):
    """评分优先取日志里的均分，没有日志时用卡片标签里写的分数。"""
    avg = (rec or {}).get("avg")
    return avg if avg is not None else c.get("score")


def score_block(score, width=56):
    if score is None:
        return ""
    return (f'<table class="sc" role="presentation" cellpadding="0" cellspacing="0" align="right"><tr>'
            f'<td class="sc-c">{meter(score, width)}</td><td class="sc-c sc-num">{score}</td></tr></table>')


def kpi_row(stats):
    cells = "".join(f'<td class="kpi-c"><div class="kpi-v">{v}</div><div class="kpi-l">{html.escape(l)}</div></td>'
                    for v, l in stats)
    return f'<table class="kpi" role="presentation" cellpadding="0" cellspacing="0"><tr>{cells}</tr></table>'


def ask(content, href):
    """可追问的内容：有深链就包成链接并加 ↗，没有就原样。"""
    if not href:
        return content
    return f'<a class="ask" href="{href}" target="_blank">{content}<span class="go">↗</span></a>'


def dims_row(rec):
    ps = (rec or {}).get("passes") or {}
    if not all(k in ps for k in ("A", "B")):
        return ""
    cells = []
    for ax, label in AXES:
        v = (ps["A"].get(ax, 0) + ps["B"].get(ax, 0)) / 2
        hi = ax == "fit"
        cells.append(f'<td class="dm-c"><div class="dm-l{" dm-hi" if hi else ""}">{label} <b>{v:g}</b></div>'
                     f'{meter(round(v * 10), 40, muted=not hi)}</td>')
    return f'<table class="dm" role="presentation" cellpadding="0" cellspacing="0"><tr>{"".join(cells)}</tr></table>'


def method_line(m):
    sub = f'<span class="kp-s">　{m["sub"]}</span>' if m["sub"] else ""
    body = f'<span class="dot">•</span><span class="kp-b">{m["main"]}</span>{sub}'
    return f'<div class="kp-li">{ask(body, m["href"])}</div>'


def result_line(r):
    sub = f'<span class="kp-s">：{r["sub"]}</span>' if r["sub"] else ""
    icon = f'<span class="st st-{r["level"]}">{STATUS[r["level"]]}</span>'
    body = f'{icon}<span class="kp-b">{r["main"]}</span>{sub}'
    return f'<div class="kp-li">{ask(body, r["href"])}</div>'


def render_paper(c, no, rec, with_terms):
    rec = rec or {}
    score = score_of(c, rec)
    tag = c["tag"] or html.escape(rec.get("primary_category", ""), quote=False)
    aid = c["arxiv_id"] or rec.get("arxiv_id", "")
    if c["link"]:
        title = f'<a href="{c["link"]}" target="_blank">{c["title"]}</a>'
        en = c["en"] + (f' · <a href="{c["link"]}" target="_blank">arXiv {aid}</a>' if aid else "")
    else:
        title, en = c["title"], c["en"]
    tag_html = f'<span class="pc-tag">{tag}</span>' if tag else ""
    out = [f'<table class="pc" id="p{no}" role="presentation" width="100%" cellpadding="0" cellspacing="0">'
           '<tr><td class="pc-in">',
           '<table class="pc-head" role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
           f'<td class="ph-c"><span class="pc-no">{no:02d}</span>{tag_html}</td>'
           f'<td class="ph-c" align="right">{score_block(score)}</td></tr></table>',
           f'<h2 class="pc-title">{title}</h2>',
           f'<div class="pc-en">{en}</div>']
    if c["conc"] and c["conc"]["main"]:
        main, rel = split_relevance(c["conc"]["main"])
        rel_html = f'<div class="lede-r">↳ {rel}</div>' if rel else ""
        out.append(f'<div class="lede"><div class="lede-l">结论</div>'
                   f'<div class="lede-t">{ask(main, c["conc"]["href"])}</div>{rel_html}</div>')
    rows = []
    if c["q"] and c["q"]["main"]:
        rows.append(("问题", ask(c["q"]["main"], c["q"]["href"])))
    if c["methods"]:
        rows.append(("方法", "".join(method_line(m) for m in c["methods"])))
    if c["results"]:
        rows.append(("结果", "".join(result_line(r) for r in c["results"])))
    dims = dims_row(rec)
    if dims:
        rows.append(("评分", dims))
    if rows:
        out.append('<table class="kp" role="presentation" width="100%" cellpadding="0" cellspacing="0">'
                   + "".join(f'<tr><td class="kp-k{sep(i)}">{k}</td><td class="kp-v{sep(i)}">{v}</td></tr>'
                             for i, (k, v) in enumerate(rows))
                   + "</table>")
    if with_terms and c["terms"]:
        out.append('<details class="terms"><summary>术语速查（点开）</summary>'
                   + "".join(f'<div class="term">{t}</div>' for t in c["terms"]) + "</details>")
    out.append("</td></tr></table>")
    return "\n".join(out)


def render_overview(papers, scores, page):
    rows = []
    for no, c in enumerate(papers, 1):
        rec = scores.get(c["arxiv_id"])
        href = f"#p{no}" if page else c["link"]
        title = f'<a href="{href}">{c["title"]}</a>' if href else c["title"]
        q = f'<div class="ov-q">{c["q"]["main"]}</div>' if c["q"] and c["q"]["main"] else ""
        rows.append(f'<tr><td class="ov-c ov-no{sep(no - 1)}">{no}</td>'
                    f'<td class="ov-c ov-ti{sep(no - 1)}">{title}{q}</td>'
                    f'<td class="ov-c ov-sc{sep(no - 1)}">{score_block(score_of(c, rec))}</td></tr>')
    return ('<table class="ov" role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            + "".join(rows) + "</table>")


def render_news(c):
    title = f'<a class="nw-t" href="{c["link"]}" target="_blank">{c["title"]}</a>' if c["link"] else \
        f'<span class="nw-t">{c["title"]}</span>'
    return (f'<div class="nw-it">{title}<div class="nw-m">{c["en"]}</div>'
            f'<div class="nw-b">{c["body"]}</div></div>')


def legend():
    return ('<span class="st st-good">✓</span>达标或正面　<span class="st st-warn">!</span>背离或意外　'
            '<span class="st st-bad">✗</span>失效或未达标<br>'
            '↗ 点击可跳转 claude.ai 继续追问（需登录）。评分条为两名评分者均分（0–100）；'
            '分项为两人均值（0–10），只高亮最关键的方向贴近度。')


def render(fragment, scores=None, page=False, shortlist_html=""):
    """片段 → 正文 HTML 各段。page=True 为附件页：保留术语速查，速览链接跳到页内锚点。"""
    scores = scores or {}
    cards = [parse_card(raw) for raw in split_cards(fragment)]
    papers = [c for c in cards if c["kind"] == "paper"]
    parts = []
    if papers:
        parts.append(f'<div class="sec">今日速览 · {len(papers)} 篇</div>')
        parts.append(render_overview(papers, scores, page))
    notices = [c for c in cards if c["kind"] == "notice"]
    if papers or notices or any(c["kind"] == "raw" for c in cards):
        parts.append('<div class="sec">论文图卡</div>')
    for c in notices:
        parts.append(f'<div class="notice"><b>{c["title"]}</b>' + (f'<br>{c["body"]}' if c["body"] else "")
                     + "</div>")
    for no, c in enumerate(papers, 1):
        parts.append(render_paper(c, no, scores.get(c["arxiv_id"]), with_terms=page))
    parts += [c["raw"] for c in cards if c["kind"] == "raw"]
    if shortlist_html:
        parts.append(shortlist_html)
    news = [c for c in cards if c["kind"] == "news"]
    if news:
        parts.append(f'<div class="sec">行业与商业动态 · {len(news)} 条</div>')
        parts += [render_news(c) for c in news]
    return "\n".join(parts)
