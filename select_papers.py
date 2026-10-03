#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""论文精选：预筛 → 两次独立评分 → 合并入选 → 评测校准。

思路改编自 AIHOT（github.com/KKKKhazix/AIHOT，MIT）的精选流程：
  - 预筛只判方向相关（PASS / BLOCK / UNKNOWN），宽进；BLOCK 要有正面依据。
  - 同一评分标准独立打两次，两次之和 ≥ 2 × 门槛才入选；门槛按信源分级区分。
  - 评分模型只给内容类型和五维整数分，合成总分由本脚本按 selection/config.json 的权重完成，
    评分时看不到关键词得分和门槛。
  - 每天把全部候选的评分写进 selection_log/，人工标注后用 eval 子命令校准门槛。

用法（定时任务里按顺序执行，判断步骤由模型完成）：
    python select_papers.py prefilter-input --profile main   # 生成 work/prefilter_main.md
    # 模型读 work/prefilter_main.md，写 work/prefilter_main.json
    python select_papers.py score-input --profile main       # 生成 work/score_main.md
    # 两个互不可见的评分者各读一遍，分别写 work/score_main_A.json 与 work/score_main_B.json
    python select_papers.py merge --profile main             # 生成 work/selected_main.md，写评分日志
    python select_papers.py eval --gold selection/gold.jsonl # 在标注样本上扫门槛
"""

import argparse
import glob
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone

LOCAL_TZ = timezone(timedelta(hours=9))
HERE = os.path.dirname(os.path.abspath(__file__))
SEL_DIR = os.path.join(HERE, "selection")
WORK_DIR = os.path.join(HERE, "work")
LOG_DIR = os.path.join(HERE, "selection_log")
PAPERS_FILE = {"main": "papers.json", "am": "papers_am.json"}
AXES = ("sig", "nov", "cred", "fit", "act")
LABELS = ("PASS", "BLOCK", "UNKNOWN")


class SelectionError(Exception):
    """模型输出不合规或缺文件。抛出后以退出码 2 结束，定时任务据此如实报告。"""


# ---------------- 读取与渲染 ----------------

def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def load_json(path, what):
    if not os.path.exists(path):
        raise SelectionError(f"缺少{what}：{os.path.relpath(path, HERE)}")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as ex:
        raise SelectionError(f"{what}不是合法 JSON（{os.path.relpath(path, HERE)}）：{ex}")


def load_config():
    return load_json(os.path.join(SEL_DIR, "config.json"), "配置")


def load_papers(profile):
    data = load_json(os.path.join(HERE, PAPERS_FILE[profile]), f"{profile} 抓取结果")
    return data, data.get("papers", [])


def interest(profile):
    return read(os.path.join(SEL_DIR, f"interest-{profile}.md")).strip()


def prompt_version(profile, cfg):
    """提示词版本 = 预筛、评分、方向说明与权重内容的哈希。改了任何一项，日志里的版本就变。"""
    h = hashlib.sha1()
    for name in ("prefilter.md", "score.md", f"interest-{profile}.md"):
        h.update(read(os.path.join(SEL_DIR, name)).encode("utf-8"))
    h.update(json.dumps(cfg["weights"], sort_keys=True).encode("utf-8"))
    return h.hexdigest()[:8]


def render(template, profile):
    return read(os.path.join(SEL_DIR, template)).replace("{{interest}}", interest(profile))


def material(papers):
    """评分者看到的材料：标题、分类、摘要。不给关键词得分、作者、门槛。"""
    out = []
    for i, p in enumerate(papers, 1):
        out.append(f"### {i}. arxiv_id: {p['arxiv_id']}\n"
                   f"- 标题: {p['title']}\n"
                   f"- arXiv 分类: {p.get('primary_category', '')}\n"
                   f"- 摘要: {p.get('summary', '')}\n")
    return "\n".join(out)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# ---------------- 子命令：生成预筛与评分输入 ----------------

def cmd_prefilter_input(args):
    data, papers = load_papers(args.profile)
    out = os.path.join(WORK_DIR, f"prefilter_{args.profile}.md")
    target = f"work/prefilter_{args.profile}.json"
    if data.get("fetch_failed"):
        write(out, "抓取失败，本次没有候选论文需要预筛。不要创建预筛结果文件。\n")
        print(f"[{args.profile}] 抓取失败，跳过预筛输入")
        return
    if not papers:
        write(out, f"今日没有候选论文。请写入 {target}，内容为 []。\n")
        print(f"[{args.profile}] 无候选，已生成空预筛说明")
        return
    task = (f"\n## 任务说明\n\n下面共 {len(papers)} 篇候选论文。逐篇判定后，把 JSON 数组写入 `{target}`。\n\n"
            f"## 候选论文\n\n{material(papers)}")
    write(out, render("prefilter.md", args.profile) + task)
    print(f"[{args.profile}] 已生成 {os.path.relpath(out, HERE)}（{len(papers)} 篇）")


def check_prefilter(papers, pre):
    if not isinstance(pre, list):
        raise SelectionError("预筛结果必须是数组")
    by_id = {}
    for r in pre:
        if not isinstance(r, dict) or r.get("label") not in LABELS:
            raise SelectionError(f"预筛结果有不合规项：{r}")
        by_id[str(r.get("arxiv_id"))] = r
    missing = [p["arxiv_id"] for p in papers if p["arxiv_id"] not in by_id]
    if missing:
        raise SelectionError(f"预筛结果缺少 {len(missing)} 篇：{', '.join(missing)}")
    return by_id


def cmd_score_input(args):
    data, papers = load_papers(args.profile)
    out = os.path.join(WORK_DIR, f"score_{args.profile}.md")
    if data.get("fetch_failed") or not papers:
        write(out, "今日没有需要评分的论文。不要创建评分结果文件。\n")
        print(f"[{args.profile}] 无需评分")
        return
    pre = check_prefilter(papers, load_json(
        os.path.join(WORK_DIR, f"prefilter_{args.profile}.json"), "预筛结果"))
    todo = [p for p in papers if pre[p["arxiv_id"]]["label"] != "BLOCK"]
    if not todo:
        write(out, "全部候选在预筛中被判为 BLOCK，今日没有需要评分的论文。不要创建评分结果文件。\n")
        print(f"[{args.profile}] 预筛后无需评分（{len(papers)} 篇全部 BLOCK）")
        return
    task = (f"\n## 任务说明\n\n下面共 {len(todo)} 篇论文。逐篇独立评分后，把 JSON 数组写入调用方指定的文件"
            f"（`work/score_{args.profile}_A.json` 或 `work/score_{args.profile}_B.json`，只写你被指定的那一个）。"
            f"不要读取另一个评分文件。\n\n## 待评论文\n\n{material(todo)}")
    write(out, render("score.md", args.profile) + task)
    print(f"[{args.profile}] 已生成 {os.path.relpath(out, HERE)}（{len(todo)} 篇待评，"
          f"预筛 BLOCK {len(papers) - len(todo)} 篇）")


# ---------------- 子命令：合并 ----------------

def check_pass(rows, ids, weights, name):
    if not isinstance(rows, list):
        raise SelectionError(f"评分 {name} 必须是数组")
    by_id = {}
    for r in rows:
        if not isinstance(r, dict):
            raise SelectionError(f"评分 {name} 有不合规项：{r}")
        rid = str(r.get("arxiv_id"))
        if r.get("type") not in weights:
            raise SelectionError(f"评分 {name} 中 {rid} 的 type 不合规：{r.get('type')}")
        for ax in AXES:
            v = r.get(ax)
            if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 10:
                raise SelectionError(f"评分 {name} 中 {rid} 的 {ax} 不是 0–10 整数：{v}")
        by_id[rid] = r
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise SelectionError(f"评分 {name} 缺少 {len(missing)} 篇：{', '.join(missing)}")
    return by_id


def weighted(r, weights):
    w = weights[r["type"]]
    return sum(r[ax] * w[ax] for ax in AXES)


def decide(total, fit_avg, threshold, cfg):
    """与 AIHOT 相同的入选规则：两次之和 ≥ 2 × 门槛；另加方向贴近度下限。"""
    if fit_avg < cfg["min_fit"]:
        return "rejected"
    if total >= 2 * threshold:
        return "selected"
    if total // 2 >= cfg["shortlist_floor"]:
        return "shortlist"
    return "rejected"


def cmd_merge(args):
    cfg = load_config()
    data, papers = load_papers(args.profile)
    today = data.get("date") or datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    version = prompt_version(args.profile, cfg)
    tier = cfg["source_tier"]["arxiv"]
    threshold = cfg["thresholds"][tier]
    out_md = os.path.join(WORK_DIR, f"selected_{args.profile}.md")
    label = "主方向" if args.profile == "main" else "交叉方向"

    if data.get("fetch_failed"):
        write(out_md, f"# {label}精选结果 {today}\n\n**⚠ 抓取失败**：{data.get('pages_failed')} 页请求全部失败，"
                      "一条 entry 都没取到。这是故障，不是空结果。简报中须如实记录抓取失败，"
                      "不得写成「今日无匹配新论文」。\n")
        print(f"[{args.profile}] 抓取失败，已写入故障说明")
        return

    records = []
    if papers:
        pre = check_prefilter(papers, load_json(
            os.path.join(WORK_DIR, f"prefilter_{args.profile}.json"), "预筛结果"))
        scored_ids = [p["arxiv_id"] for p in papers if pre[p["arxiv_id"]]["label"] != "BLOCK"]
        passes = {}
        if scored_ids:
            for name in ("A", "B"):
                passes[name] = check_pass(load_json(
                    os.path.join(WORK_DIR, f"score_{args.profile}_{name}.json"), f"评分 {name}"),
                    scored_ids, cfg["weights"], name)
        for p in papers:
            pid = p["arxiv_id"]
            rec = {
                "date": today, "profile": args.profile, "arxiv_id": pid,
                "title": p["title"], "primary_category": p.get("primary_category", ""),
                "published": p.get("published", ""), "keyword_score": p.get("score"),
                "prefilter": {"label": pre[pid]["label"], "reason": pre[pid].get("reason", "")},
                "tier": tier, "threshold": threshold, "min_fit": cfg["min_fit"],
                "prompt_version": version, "independent": not args.not_independent,
            }
            if pre[pid]["label"] == "BLOCK":
                rec.update(decision="blocked", total=None, avg=None, fit_avg=None)
            else:
                ps = {}
                for name in ("A", "B"):
                    r = passes[name][pid]
                    ps[name] = {ax: r[ax] for ax in AXES}
                    ps[name].update(type=r["type"], note=r.get("note", ""),
                                    score=weighted(r, cfg["weights"]))
                total = ps["A"]["score"] + ps["B"]["score"]
                fit_avg = (ps["A"]["fit"] + ps["B"]["fit"]) / 2
                rec.update(passes=ps, total=total, avg=total // 2, fit_avg=fit_avg,
                           decision=decide(total, fit_avg, threshold, cfg))
            records.append(rec)

    # 版面上限：入选超出上限的降为备选，备选超出上限的不展示（日志里照常保留）
    rank = sorted((r for r in records if r["decision"] == "selected"),
                  key=lambda r: (-r["total"], r["arxiv_id"]))
    cap = cfg["max_selected"][args.profile]
    for r in rank[cap:]:
        r["decision"] = "shortlist"
        r["capped"] = True
    selected = [r for r in rank[:cap]]
    shortlist = sorted((r for r in records if r["decision"] == "shortlist"),
                       key=lambda r: (-r["total"], r["arxiv_id"]))[:cfg["max_shortlist"]]

    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"{today}-{args.profile}.jsonl")
    with open(log_path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    by_id = {p["arxiv_id"]: p for p in papers}
    counts = {k: sum(1 for r in records if r["decision"] == k)
              for k in ("selected", "shortlist", "rejected", "blocked")}
    lines = [f"# {label}精选结果 {today}\n",
             f"> 候选 {len(records)} 篇 ｜ 预筛 BLOCK {counts['blocked']} ｜ 入选 {len(selected)} ｜ "
             f"备选 {counts['shortlist']} ｜ 未入选 {counts['rejected']}",
             f"> 门槛：两次评分之和 ≥ 2×{threshold}（{tier}），且方向贴近度均值 ≥ {cfg['min_fit']}；"
             f"备选为贴近度达标、均分 ≥ {cfg['shortlist_floor']} 的未入选论文。提示词版本 {version}。",
             f"> 抓取健康：成功 {data.get('pages_ok')} 页 / 失败 {data.get('pages_failed')} 页 / "
             f"entry {data.get('entries_seen')} 条，跳过历史重复 {data.get('skipped_past')} 篇。\n"]
    if not records:
        lines.append("今日无匹配新论文（抓取正常，召回为空）。\n")
    elif not selected:
        lines.append("今日没有论文达到入选门槛。简报中如实写明，并列出下方备选。\n")
    lines.append("## 入选论文（按评分排序，简报只详写这些）\n")
    for i, r in enumerate(selected, 1):
        p = by_id[r["arxiv_id"]]
        a, b = r["passes"]["A"], r["passes"]["B"]
        lines += [f"### {i}. {p['title']}",
                  f"- arXiv: {p['arxiv_id']} ｜ 分类: {p.get('primary_category', '')} ｜ 投稿: {p.get('published', '')[:10]}",
                  f"- 评分: {r['avg']}（A {a['score']} / B {b['score']}）｜ 类型: {a['type']} ｜ 贴近度 {r['fit_avg']:g}",
                  f"- 评分者备注: A「{a['note']}」 B「{b['note']}」",
                  f"- 作者: {', '.join(p.get('authors', []))}",
                  f"- 链接: {p.get('link', '')}",
                  f"- 摘要原文: {p.get('summary', '')}\n"]
    lines.append("## 备选（未达门槛，简报中每篇只列一行）\n")
    if not shortlist:
        lines.append("无。\n")
    for r in shortlist:
        p = by_id[r["arxiv_id"]]
        why = "超出当日版面上限" if r.get("capped") else "总分未达门槛"
        lines.append(f"- {p['title']} ｜ arXiv {p['arxiv_id']} ｜ {p.get('link', '')} ｜ 评分 {r['avg']} ｜ {why}"
                     f" ｜ 备注：{r['passes']['A']['note']}")
    write(out_md, "\n".join(lines) + "\n")
    print(f"[{args.profile}] 入选 {len(selected)} / 备选 {counts['shortlist']} / 未入选 {counts['rejected']} / "
          f"BLOCK {counts['blocked']}；已写入 {os.path.relpath(out_md, HERE)} 与 {os.path.relpath(log_path, HERE)}")


# ---------------- 子命令：评测 ----------------

def load_scored(paths):
    """读评分日志（或回测得到的同格式文件），同一 arxiv_id 取最后一条。"""
    out = {}
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    out[r["arxiv_id"]] = r
    return out


def predict(rec, threshold, min_fit):
    if rec.get("decision") == "blocked" or rec.get("total") is None:
        return "reject"
    return "select" if rec["total"] >= 2 * threshold and rec["fit_avg"] >= min_fit else "reject"


def metrics(pairs):
    tp = sum(1 for g, p in pairs if g == "select" and p == "select")
    fp = sum(1 for g, p in pairs if g == "reject" and p == "select")
    fn = sum(1 for g, p in pairs if g == "select" and p == "reject")
    tn = sum(1 for g, p in pairs if g == "reject" and p == "reject")
    n = tp + fp + fn + tn
    prec = tp / (tp + fp) if tp + fp else float("nan")
    rec = tp / (tp + fn) if tp + fn else float("nan")
    f1 = 2 * prec * rec / (prec + rec) if tp else 0.0
    return {"n": n, "acc": (tp + tn) / n if n else float("nan"), "prec": prec, "rec": rec, "f1": f1,
            "selected": tp + fp}


def cmd_eval(args):
    cfg = load_config()
    paths = sorted(glob.glob(os.path.join(args.scores, "*.jsonl"))) if os.path.isdir(args.scores) else [args.scores]
    scored = load_scored(paths)
    gold = []
    with open(args.gold, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                gold.append(json.loads(line))
    for g in gold:
        # 标注里没写 split 的，按 ID 哈希固定分出约 1/5 做留出集：调提示词只看开发集，最后再看留出集
        if not g.get("split"):
            g["split"] = "holdout" if int(hashlib.sha1(g["arxiv_id"].encode()).hexdigest(), 16) % 5 == 0 \
                else "development"
    if args.split:
        gold = [g for g in gold if g["split"] == args.split]
    if args.profile:
        gold = [g for g in gold if g.get("profile") == args.profile]
    cases = [(g, scored[g["arxiv_id"]]) for g in gold
             if g.get("gold") in ("select", "reject") and g["arxiv_id"] in scored]
    unscored = [g["arxiv_id"] for g in gold if g["arxiv_id"] not in scored]
    either = sum(1 for g in gold if g.get("gold") == "either")
    min_fit = cfg["min_fit"] if args.min_fit is None else args.min_fit
    versions = sorted({r.get("prompt_version", "?") for _, r in cases})
    print(f"样本 {len(cases)} 条（两可 {either} 条不计；无评分记录 {len(unscored)} 条）｜ "
          f"方向贴近度下限 {min_fit} ｜ 提示词版本 {', '.join(versions) or '-'}")
    if not cases:
        return
    sel = sum(1 for g, _ in cases if g["gold"] == "select")
    print(f"标注：该选 {sel} / 不该选 {len(cases) - sel}\n")
    print(" 门槛  入选数  准确率  查准率  查全率    F1")
    current = cfg["thresholds"][cfg["source_tier"]["arxiv"]]
    for t in range(args.lo, args.hi + 1, 2):
        m = metrics([(g["gold"], predict(r, t, min_fit)) for g, r in cases])
        mark = "  ← 当前" if t == current else ""
        print(f" {t:>4}  {m['selected']:>6}  {m['acc']:.3f}  {m['prec']:.3f}  {m['rec']:.3f}  {m['f1']:.3f}{mark}")
    t = args.threshold or current
    errs = [(g, r) for g, r in cases if predict(r, t, min_fit) != g["gold"]]
    print(f"\n门槛 {t} 下判错 {len(errs)} 条：")
    for g, r in sorted(errs, key=lambda x: -(x[1].get("total") or 0)):
        kind = "误选" if g["gold"] == "reject" else "漏选"
        avg = r.get("avg")
        note = (r.get("passes") or {}).get("A", {}).get("note", r.get("prefilter", {}).get("reason", ""))
        print(f"  [{kind}] {r['arxiv_id']} 均分 {avg if avg is not None else 'BLOCK'} "
              f"贴近度 {r.get('fit_avg')} ｜ {r['title'][:70]} ｜ {note}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("prefilter-input", "score-input", "merge"):
        sp = sub.add_parser(name)
        sp.add_argument("--profile", required=True, choices=sorted(PAPERS_FILE))
        if name == "merge":
            sp.add_argument("--not-independent", action="store_true",
                            help="两次评分不是由互不可见的评分者完成时标记，写入日志")
    ev = sub.add_parser("eval")
    ev.add_argument("--gold", required=True, help="标注文件 JSONL：arxiv_id / profile / gold / split")
    ev.add_argument("--scores", default=LOG_DIR, help="评分日志目录或单个 JSONL（默认 selection_log/）")
    ev.add_argument("--profile", choices=sorted(PAPERS_FILE))
    ev.add_argument("--split", help="只评某个子集，如 development 或 holdout")
    ev.add_argument("--min-fit", type=float, help="覆盖 config.json 的方向贴近度下限")
    ev.add_argument("--threshold", type=int, help="列出判错样本时用的门槛（默认用 config.json）")
    ev.add_argument("--lo", type=int, default=30)
    ev.add_argument("--hi", type=int, default=80)
    args = ap.parse_args()
    try:
        {"prefilter-input": cmd_prefilter_input, "score-input": cmd_score_input,
         "merge": cmd_merge, "eval": cmd_eval}[args.cmd](args)
    except SelectionError as ex:
        sys.stderr.write(f"[error] {ex}\n")
        sys.exit(2)


if __name__ == "__main__":
    main()
