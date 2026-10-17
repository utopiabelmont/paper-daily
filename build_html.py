#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把模型生成的卡片片段按 cards.py 的版式重排，合入固定模板，产出当日图卡 HTML。
用法: python build_html.py cards_fragment.html digests_html/2026-07-24.html

原始片段另存在页尾的 <template id="cards-src"> 里（不显示），build_email.py 从这里取片段
排邮件正文，两边用同一份解析与版式。"""
import html
import os
import sys

import cards

TITLES = {"main": "每日论文图卡", "am": "交叉方向论文图卡"}


def main():
    if len(sys.argv) < 3:
        sys.exit("用法: python build_html.py <片段文件> <输出路径>")
    frag_path, out_path = sys.argv[1], sys.argv[2]
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "card_template.html"), encoding="utf-8") as f:
        tpl = f.read()
    with open(frag_path, encoding="utf-8") as f:
        frag = f.read()
    date = os.path.basename(out_path).replace(".html", "")
    profile = cards.profile_of(out_path)
    scores = cards.load_scores(profile, date)
    stats = cards.stats_of(scores)
    body = (cards.kpi_row(stats) if stats else "") + cards.render(frag, scores, page=True)
    body += f'\n<template id="cards-src">{frag}</template>'
    page = (tpl.replace("/*CARD_VARS*/", cards.css_vars())
               .replace("/*CARD_CSS*/", cards.css())
               .replace("{{TITLE}}", html.escape(TITLES[profile]))
               .replace("{{DATE}}", date)
               .replace("<!--LEGEND-->", cards.legend())
               .replace("<!--CARDS-->", body))
    d = os.path.dirname(out_path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"已生成 {out_path}")


if __name__ == "__main__":
    main()
