# 论文精选

每日简报不再按关键词命中次数直接推送，改为“宽召回、严精选”。流程和规则改编自 [AIHOT](https://github.com/KKKKhazix/AIHOT)（MIT）的精选流程。

## 流程

1. **召回**（`fetch_arxiv.py`）：arXiv 检索式召回，分层关键词（核心词、泛化词、负面词）只用于排序，每个方向最多 30 篇进入下一步。已推送过的、此前各天已评过分的论文不再召回。交叉方向的增材与激光加工专有词组（`domain_terms`）不限 arXiv 分类，泛化词组仍要落在指定分类里（2026-10-17 起；此前主分类为 cs.LG 的熔池与增材论文整篇召回不到）。
2. **预筛**（`prefilter.md`）：只判断是否与方向相关，输出 PASS、BLOCK 或 UNKNOWN。BLOCK 需要能确认无关的正面依据，拿不准的 UNKNOWN 继续评分。
3. **两次独立评分**（`score.md`）：两个互不可见的评分者，各自给出内容类型和五个维度（领域份量 sig、信息增量 nov、证据强度 cred、方向贴近度 fit、可迁移性 act）的 0–10 整数分。评分时看不到关键词得分和门槛。
4. **合并**（`select_papers.py merge`）：脚本按 `config.json` 里的类型权重算出两次总分。两次之和 ≥ 2 × 门槛，且方向贴近度均值 ≥ `min_fit`，才入选；贴近度均值达到 `fast_track.min_fit` 的论文走方向直通，门槛降为 `fast_track.threshold`。未入选但贴近度达标、均分 ≥ `shortlist_floor` 的列为备选。

   2026-10-17 放宽：门槛 50 → 45，新增方向直通（贴近度 ≥ 5、均分 ≥ 40），备选下限 40 → 35。依据是 10-03 至 10-17 的评分日志与回测：贴近度 5–6 的工业缺陷检测、边缘检测论文均分多在 40–47，原规则下全部停在备选。按新规则重算，回测的 117 篇已推送论文入选数从 11 增至 24，每日日志从 17 增至 23。
5. **写简报**：只详写入选论文，备选每篇一行。全部候选的评分写进 `selection_log/`。

方向的定义在 `interest-main.md` 和 `interest-am.md`。觉得某类论文被高估或低估时，先改这两个文件和 `score.md` 里的“必须正常评价 / 必须压住”，再动门槛：门槛只能整体移动，解决不了“哪一类判错了”。

## 校准

1. 标注：把一批论文标成该选 `select`、不该选 `reject` 或两可 `either`，存为 `selection/gold.jsonl`，每行一条：
   ```json
   {"arxiv_id": "2609.21866", "profile": "main", "gold": "select"}
   ```
   没写 `split` 的，脚本按 ID 固定分出约五分之一做留出集。
2. 评测：
   ```bash
   python select_papers.py eval --gold selection/gold.jsonl --split development
   ```
   输出门槛从 30 到 80 每隔 2 分的入选数、准确率、查准率、查全率，以及当前门槛下判错的论文。方向直通默认按 `config.json` 生效，加 `--no-fast-track` 可只看总分门槛。
3. 看判错的论文，改方向说明或评分标准，等新的评分日志积累后再跑；最后用 `--split holdout` 检查一遍，避免把标准调成只会做这几道题。

`selection_log/backtest-2026-10-03-*.jsonl` 是用新标准对 2026-09-20 至 10-03 已推送的 117 篇论文的回测评分，可直接作为第一批标注的评分来源。

## 文件

| 文件 | 内容 |
|---|---|
| `interest-main.md`、`interest-am.md` | 两个方向的读者与方向定义（正中、关注、无交集） |
| `prefilter.md` | 预筛规则 |
| `score.md` | 评分标准：内容类型、五个维度、品味规则、输入安全 |
| `config.json` | 门槛、方向直通、贴近度下限、备选下限、版面上限、类型权重 |
| `writing-rules.md` | 写摘要时的防幻觉约束 |
| `routines/main.md`、`routines/am.md` | 两个定时任务当前使用的说明（与定时任务里的内容保持一致） |
| `routines/*.prev.md` | 2026-10-03 改版前的定时任务说明，回退时把它贴回定时任务即可 |
