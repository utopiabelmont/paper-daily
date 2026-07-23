#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把模型生成的卡片片段合入固定模板，产出当日图卡 HTML。
用法: python build_html.py cards_fragment.html digests_html/2026-07-24.html"""
import os
import sys

def main():
    if len(sys.argv) < 3:
        sys.exit("用法: python build_html.py <片段文件> <输出路径>")
    frag_path, out_path = sys.argv[1], sys.argv[2]
    with open("card_template.html", encoding="utf-8") as f:
        tpl = f.read()
    with open(frag_path, encoding="utf-8") as f:
        frag = f.read()
    date = os.path.basename(out_path).replace(".html", "")
    html = tpl.replace("{{DATE}}", date).replace("<!--CARDS-->", frag)
    d = os.path.dirname(out_path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"已生成 {out_path}")

if __name__ == "__main__":
    main()
