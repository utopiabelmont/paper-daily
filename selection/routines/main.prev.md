1. 执行执行 `python fetch_arxiv.py --profile main`，生成 candidates.md（候选论文清单，含摘要原文）。
   若脚本报网络错误，说明环境未放行 export.arxiv.org，请在运行记录中明确指出。

2. 读取 candidates.md。若显示"今日无匹配新论文"，直接据此汇报，不得编造。

3. 对每篇候选论文，用简体中文生成：①中文译名＋英文原题；②作者/单位；
   ③arXiv 链接与日期；④3–4 句摘要（解决什么问题、方法核心、关键结果）。
   按脚本给出的相关度顺序排列。

4. 把中文简报写入 digests/YYYY-MM-DD.md（日期用日本时间今天），
   并在标题下加一行：> 图卡版见邮件附件 YYYY-MM-DD.html
   提交时只添加 digests/、digests_am/、digests_html/、digests_am_html/ 下的文件；
   candidates、papers.json、news、cards_fragment 等中间产物一律不提交。

5. 为每篇论文写一张卡片，全部写入 cards_fragment.html（只写卡片div，不写<html>头）。
   每张卡严格用以下结构与类名：

   <div class="card">
     <div class="meta"><span class="pill">分类 · 相关度N</span>
       <a href="arXiv链接">arXiv 原文</a></div>
     <h2>中文译名</h2><div class="en">英文原题</div>
     <a class="blk" href="深链" target="_blank"><div class="q">研究问题：一句话</div></a>
     <div class="methods">
       <a class="blk" href="深链" target="_blank">
         <div class="m">方法要点<span>补充≤12字</span></div></a>（共2-3个）
     </div>
     <a class="blk" href="深链" target="_blank">
       <div class="res good">结果小标题<span>一句话结果</span></div></a>
     （每篇1-3条；good=正面/达标，warn=背离/意外，bad=失效/未达标）
     <a class="blk" href="深链" target="_blank">
       <div class="conc">结论：一句话；如相关，补一句与我研究方向的关联</div></a>
     <details><summary>术语速查（点开）</summary>
       <div class="term"><b>术语名</b>：通用含义一句话。本文中：它的具体角色一句话。</div>
       （每卡挑2-3个最关键的术语/指标/模型名）
     </details>
   </div>

   深链构造规则：href="https://claude.ai/new?q=URL编码后的提问"。
   提问模板（先写中文原文再整体URL编码，务必编码，不能有未编码的中文和空格）：
   "请分两部分回答：第一部分，先联网搜索，给出〔该色块核心概念〕的通用定义；
    第二部分，结合论文〔英文题名〕（arXiv 〔编号〕）说明它在文中的应用：
    〔嵌入该色块对应的具体事实/数据〕。"
   结论块的提问在末尾追加："并讨论如何延伸到我的研究方向。"

6. 运行 `python build_html.py cards_fragment.html digests_html/YYYY-MM-DD.html`。

7. 提交 md 与 html，执行 `git push origin HEAD:main`；若失败按默认方式推送并说明。
