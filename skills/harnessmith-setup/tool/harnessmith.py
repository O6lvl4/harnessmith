#!/usr/bin/env python3
"""harnessmith: make a harness for an agent. It does the task without the answer, is scored, and the setup grows by the score.

    harnessmith.py init <dir> --name <name>        make a bench (this file is copied into it)
    harnessmith.py draft --parts "<what to score>" a separate Claude writes the rubric from the answer
    harnessmith.py audit                           another Claude checks the rubric against the answer
    harnessmith.py reference <dir> --name N --saw-answer yes|no|unknown --provenance "..."
    harnessmith.py trial -c <comp> [--parent P]    one unattended run, scored and recorded
    harnessmith.py check                           the four checks to pass before tuning
    harnessmith.py reflect <run dir> [base comp]   a Claude that cannot see the answer proposes one change
    harnessmith.py auto --parent P [--next C]      run, decide, reflect, repeat until a stop condition
    harnessmith.py decide <comp> adopt|reject --reason "..."
    harnessmith.py status [--json]

A bench is a folder with harnessmith.json. What the worker may read (materials) and what
it may not (the answer, the rubric, every run's record) live outside it, under "home".
Four roles never share what they read: the worker has the materials, the judges have
the rubric, the reflector has the materials, the deliverable and points per part, and
whoever drives this file sees points and counts only.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

DEFAULTS = {
    "deliverable": "out",        # where the worker leaves its result, relative to its work folder
    "start": "bare",             # the configuration that adds nothing
    "model": "opus", "effort": "high", "minutes": 180,
    "judge_model": "opus", "judge_effort": "high",   # the judges, the rubric's drafter and auditor, the reflector
    "max_usd_per_trial": 30, "budget_total_usd": 200, "budget_per_gen_usd": 20,
    "tolerance": 5.0,            # points: a smaller difference is not a difference
    "patience": 3,               # generations without improvement before stopping
    "trials_per_candidate": 2, "spread_trials": 3, "judges": 2,
    "scorers": [{"type": "rubric"}],
    "targets": {},               # part -> points to reach; or "target_from": a reference's name
    "forbidden": [],             # extra regexes: a transcript that matches is not scored
    "judge_ignore": ["node_modules", ".git", "*.db", "*.db-*", "__pycache__", ".venv", "dist", "build"],
    "goal": "", "may_add": [], "must_not": [],
}
VALUE = {"covered": 1.0, "partial": 0.5, "missing": 0.0}
ISOLATE = ["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "project,local"]
CONFIG_DIR = os.path.expanduser(os.environ.get("HARNESSMITH_CONFIG_DIR", "~/.harnessmith/config"))


def die(msg):
    sys.exit(f"harnessmith: {msg}")


def log(msg):
    print(f"harnessmith: {msg}", flush=True)


# ---- the bench ----

class Bench:
    def __init__(self, root):
        self.root = root
        cfg = json.load(open(os.path.join(root, "harnessmith.json"), encoding="utf-8"))
        self.cfg = {**DEFAULTS, **cfg}
        self.name = self.cfg["name"]
        self.home = os.path.expanduser(self.cfg.get("home") or f"~/.harnessmith/{self.name}")
        self.materials = os.path.expanduser(self.cfg.get("materials") or os.path.join(self.home, "materials"))
        self.private = os.path.join(self.home, "private")
        self.runs = os.path.join(self.home, "runs")
        self.rubric_path = os.path.join(self.private, "rubric.json")
        self.ledger = os.path.join(root, "ledger.jsonl")

    def __getitem__(self, k):
        return self.cfg[k]

    def comp(self, name):
        return os.path.join(self.root, "comp", name)

    def uses_rubric(self):
        return any(s["type"] == "rubric" for s in self["scorers"])

    def rubric(self):
        return json.load(open(self.rubric_path, encoding="utf-8"))

    def rubric_hash(self):
        if not os.path.exists(self.rubric_path):
            return None
        return hashlib.sha256(open(self.rubric_path, "rb").read()).hexdigest()[:12]

    def parts(self):
        out = []
        for s in self["scorers"]:
            if s["type"] == "rubric":
                if os.path.exists(self.rubric_path):
                    out += [p["id"] for p in self.rubric()["parts"]]
            else:
                out.append(s["part"])
        return out

    # the ledger: one event per line, appended, never rewritten
    def events(self):
        if not os.path.exists(self.ledger):
            return []
        return [json.loads(l) for l in open(self.ledger, encoding="utf-8") if l.strip()]

    def append(self, ev):
        ev = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), **ev}
        with open(self.ledger, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return ev


def find_bench(start):
    d = os.path.abspath(start)
    while True:
        if os.path.exists(os.path.join(d, "harnessmith.json")):
            return Bench(d)
        if os.path.dirname(d) == d:
            die("harnessmith.json が見つかりません。ベンチのフォルダで実行するか、--bench で指定してください")
        d = os.path.dirname(d)


def claude_env(extra=None):
    if not os.path.isdir(CONFIG_DIR):
        die(f"{CONFIG_DIR} がありません。一度だけ CLAUDE_CONFIG_DIR={CONFIG_DIR} claude を起動して /login してください")
    return {**os.environ, "CLAUDE_CONFIG_DIR": CONFIG_DIR, **(extra or {})}


def trust(path):
    """Permissions in .claude/settings.json are ignored in a folder that is not trusted."""
    p = os.path.join(CONFIG_DIR, ".claude.json")
    d = json.load(open(p)) if os.path.exists(p) else {}
    d.setdefault("projects", {}).setdefault(os.path.realpath(path), {})["hasTrustDialogAccepted"] = True
    json.dump(d, open(p, "w"), indent=2)


def rule(path):
    return "/" + os.path.realpath(path)  # an absolute path in a permission rule starts with //


def comp_hash(b, comp):
    h = hashlib.sha256()
    root = b.comp(comp)
    for dp, _, fs in sorted(os.walk(root)):
        for f in sorted(fs):
            if f == "HYPOTHESIS.md":
                continue
            p = os.path.join(dp, f)
            h.update(os.path.relpath(p, root).encode())
            h.update(open(p, "rb").read())
    return h.hexdigest()[:12]


def stream_result(path):
    """The last result line of a stream-json transcript: (text, cost, turns)."""
    text, cost, turns = "", 0.0, None
    if os.path.exists(path):
        for line in open(path, encoding="utf-8", errors="replace"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") == "result":
                text, cost, turns = d.get("result") or "", d.get("total_cost_usd") or 0.0, d.get("num_turns")
    return text, round(cost, 2), turns


# ---- init ----

BARE_PROMPT = """materials/ に、課題とその資料があります。課題に取り組んでください。

- 成果物は {deliverable}/ の下に置いてください。
- この作業は無人で行われます。途中で人に確認することはできません。
"""
BARE_SETTINGS = {"permissions": {
    "allow": ["Read", "Edit", "Write", "Glob", "Grep", "Bash", "WebSearch", "WebFetch", "TodoWrite", "Task"],
    "deny": ["Read(//Users/*/workspace/**)", "Read(//Users/*/.claude*/**)", "Read(//Users/*/.ssh/**)",
             "Read(//Users/*/.config/gh/**)", "Read(//home/*/.ssh/**)", "Read(//home/*/.claude*/**)",
             "Bash(git push:*)", "Bash(git remote:*)", "Bash(gh:*)", "Bash(ssh:*)", "Bash(scp:*)",
             "PushNotification", "RemoteTrigger", "CronCreate", "ScheduleWakeup", "SendMessage"]}}
README = """# {name}（harnessmith のベンチ）

エージェントに解答を見せずに仕事をさせ、点数で比べながら構成を育てるための作業場所。

| 場所 | 中身 | commit |
| --- | --- | --- |
| `harnessmith.json` | 設定（成果物の置き場所、採点の方法、予算、止める条件） | する |
| `comp/<名前>/` | 構成。作業エージェントに渡す指示と権限。`{start}` は何も足さない構成 | する |
| `ledger.jsonl` | 試行と採否の台帳。部分ごとの点数だけ | する |
| `notes/` | 試行ごとの振り返り | する |
| `{home}/materials/` | 作業エージェントに渡す資料（解答を含めない） | しない |
| `{home}/private/` | 解答と採点表。作業エージェントにも進行役にも読ませない | しない |
| `{home}/runs/` | 試行の記録 | しない |

```sh
python3 tool/harnessmith.py status     # いまの状態
python3 tool/harnessmith.py check      # 構成をいじる前の点検 4 つ
```
"""


def cmd_init(a):
    root = os.path.abspath(a.dir)
    if os.path.exists(os.path.join(root, "harnessmith.json")):
        die(f"{root} にはもうベンチがあります")
    home = f"~/.harnessmith/{a.name}"
    os.makedirs(os.path.join(root, "comp", "bare", ".claude"), exist_ok=True)
    os.makedirs(os.path.join(root, "notes"), exist_ok=True)
    os.makedirs(os.path.join(root, "tool"), exist_ok=True)
    cfg = {"name": a.name, "home": home, "deliverable": a.deliverable, "goal": "",
           "scorers": [{"type": "rubric"}], "targets": {},
           "model": "opus", "effort": "high", "minutes": 180,
           "max_usd_per_trial": 30, "budget_total_usd": 200, "budget_per_gen_usd": 20,
           "tolerance": 5.0, "patience": 3, "forbidden": [], "may_add": [], "must_not": []}
    json.dump(cfg, open(os.path.join(root, "harnessmith.json"), "w"), ensure_ascii=False, indent=2)
    open(os.path.join(root, "comp", "bare", "PROMPT.md"), "w").write(BARE_PROMPT.format(deliverable=a.deliverable))
    json.dump(BARE_SETTINGS, open(os.path.join(root, "comp", "bare", ".claude", "settings.json"), "w"), indent=2)
    open(os.path.join(root, "ledger.jsonl"), "a").close()
    open(os.path.join(root, "notes", ".gitkeep"), "a").close()
    open(os.path.join(root, "README.md"), "w").write(README.format(name=a.name, home=home, start="bare"))
    shutil.copy(os.path.abspath(__file__), os.path.join(root, "tool", "harnessmith.py"))
    for d in ("materials", "private", "runs"):
        os.makedirs(os.path.join(os.path.expanduser(home), d), exist_ok=True)
    log(f"ベンチを作りました: {root}")
    log(f"資料は {home}/materials/ に、解答は {home}/private/ に置いてください")


# ---- the rubric: drafted and audited by Claudes other than the one driving ----

RUBRIC_FORMAT = """{
  "parts": [
    {"id": "<英小文字と_の短い名前>", "title": "<この部分で何を見るか>",
     "criteria": "<判定の基準。covered / partial / missing の境目、順序や向きや担い手の扱いを、迷わない形で書く>",
     "items": [{"id": "<部分の中で一意の番号>", "text": "<成果物に含まれているべきこと 1 つ>",
                "group": "<任意。担い手や章など、点数を分けて見たい単位>", "note": "<任意。この項目だけの判定の注>"}]}
  ]
}"""
LIST_SCHEMA = {"type": "object", "properties": {
    "checked": {"type": "integer"}, "fixed": {"type": "array", "items": {"type": "string"}},
    "unsure": {"type": "array", "items": {"type": "string"}}, "items_per_part": {"type": "object"}},
    "required": ["checked", "fixed", "unsure"]}


def private_claude(b, prompt, what):
    """Run a Claude inside private/: it may read the answer and write the rubric and its own notes."""
    trust(b.private)
    settings = {"permissions": {"allow": ["Read", "Glob", "Grep", "Write", "Edit"],
                                "deny": ["Bash", "WebFetch", "WebSearch", "Task"]}}
    out = subprocess.run(
        ["claude", "-p", prompt, "--model", b["judge_model"], "--effort", b["judge_effort"], *ISOLATE,
         "--settings", json.dumps(settings), "--json-schema", json.dumps(LIST_SCHEMA),
         "--output-format", "json", "--no-session-persistence"],
        cwd=b.private, env=claude_env(), capture_output=True, text=True, timeout=3600)
    if out.returncode != 0:
        die(f"{what} に失敗しました: {out.stderr[-400:]} {out.stdout[-400:]}")
    res = json.loads(out.stdout)
    data = res.get("structured_output") or json.loads(res["result"])
    b.append({"event": "aux", "what": what, "cost_usd": round(res.get("total_cost_usd") or 0, 2)})
    return data


def cmd_draft(a):
    b = find_bench(a.bench)
    if os.path.exists(b.rubric_path) and not a.force:
        die("採点表はもうあります。書き直すなら --force を付けてください")
    if not [f for f in os.listdir(b.private) if f not in ("rubric.json", "audit.md")]:
        die(f"{b.private} に解答がありません")
    data = private_claude(b, f"""あなたは採点表を書く役です。カレントディレクトリにある解答（手本）を読み、rubric.json を書いてください。画像や PDF があれば、それも読んでください。

採点したいこと:
{a.parts}

形式（JSON、このキーだけを使う）:
{RUBRIC_FORMAT}

- 項目は、解答にあることを 1 件ずつ、漏らさず、足さずに書きます。解答にないことを項目にしません。
- 1 項目は 1 つのことだけにします。「A と B」は 2 項目に分けます。
- つながり・順序・向きを見る部分では、どこからどこへかを、解答のとおりに書きます。広い相手宛てのものを、特定の相手宛てに狭めません。
- criteria は、あとで別の判定者が成果物だけを読んで判定するための基準です。言葉が違っても同じことを指していれば covered とする、などの一般の基準に加えて、この部分で迷いそうな境目を書いてください。
- 書けるのは rubric.json だけです。

終わったら、checked に項目の総数、items_per_part に部分ごとの項目数を返してください。fixed と unsure は空の配列にしてください。""", "draft")
    b.append({"event": "rubric", "by": "drafter", "hash": b.rubric_hash(), "items": data.get("checked")})
    log(f"採点表を書きました: {data.get('checked')} 項目 {data.get('items_per_part') or ''}")
    log("中身は読まずに、次は audit で別の目に照らさせてください")


def cmd_audit(a):
    b = find_bench(a.bench)
    if not os.path.exists(b.rubric_path):
        die("採点表がありません。先に draft してください")
    data = private_claude(b, """あなたは採点表の点検役です。採点表を書いた者とは別の目として、カレントディレクトリの rubric.json を、同じ場所にある解答（手本）と 1 項目ずつ照らしてください。画像や PDF があれば、それも読んでください。

- 各項目について、解答にそのとおりあるかを確かめます。見るのは、内容の取り違え、行き先や向きの取り違え、広い相手宛てのものを狭めた書き方、解答にない項目、解答にあるのに抜けている項目です。
- criteria が、判定者が迷わない形で書かれているかも見ます。「一部」と「ない」の境目、順序や向きの扱いが書かれていなければ、書き足します。
- 誤りは rubric.json を直接直してください。直した理由は audit.md に、項目の番号ごとに書いてください。
- 解答から決められない項目は直さず、unsure に番号を入れてください。
- 書けるのは rubric.json と audit.md だけです。

checked に照らした項目数、fixed に直した項目の番号、unsure に決められなかった項目の番号を返してください。""", "audit")
    ev = b.append({"event": "audit", "by": "auditor", "hash": b.rubric_hash(), "checked": data["checked"],
                   "fixed": data["fixed"], "unsure": data["unsure"]})
    log(f"点検しました: {ev['checked']} 項目、直した {len(ev['fixed'])} 件、決められなかった {len(ev['unsure'])} 件")
    if ev["fixed"]:
        log("直した項目があります。もう一度 audit を回して、直しが 0 件になることを確かめてください")
    if ev["unsure"]:
        log(f"決められなかった項目は、人が {b.private}/audit.md と解答を見て決めてください: " + "、".join(ev["unsure"]))


# ---- scoring ----

ITEM = {"type": "object", "properties": {"id": {"type": "string"}, "verdict": {"enum": list(VALUE)},
                                         "evidence": {"type": "string"}}, "required": ["id", "verdict", "evidence"]}
JUDGE_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": ITEM}}, "required": ["items"]}


def judge_prompt(b, part):
    lines = [f"""あなたは採点者です。カレントディレクトリの {b['deliverable']}/ にある成果物を読み、下の採点表の各項目が成果物に含まれているかを判定してください。
- 読むのは {b['deliverable']}/ の下だけです。画像があれば画像も読んでください。
- 言葉が違っても、同じことを指していれば含まれているとします。同じ言葉でも、指しているものが違えば含まれていません。
- verdict は covered（含まれている）・partial（一部だけ）・missing（ない）のどれかにしてください。
- evidence には、根拠になったファイルと行、または「見つからない」と書いてください。
- 採点表のすべての id について、1 件ずつ返してください。
- 項目に「注」が付いているときは、その注を一般の基準より優先してください。
""", f"## {part['title']}", "", part.get("criteria", ""), ""]
    for it in part["items"]:
        group = f"［{it['group']}］" if it.get("group") else ""
        note = f"（注：{it['note']}）" if it.get("note") else ""
        lines.append(f"- {it['id']}: {group}{it['text']}{note}")
    return "\n".join(lines)


def judge(b, cwd, prompt):
    out = subprocess.run(
        ["claude", "-p", prompt, "--model", b["judge_model"], "--effort", b["judge_effort"], *ISOLATE,
         "--allowedTools", "Read", "Glob", "Grep",
         "--disallowedTools", "Bash", "Edit", "Write", "WebFetch", "WebSearch", "Task",
         "--json-schema", json.dumps(JUDGE_SCHEMA), "--output-format", "json", "--no-session-persistence"],
        cwd=cwd, env=claude_env(), capture_output=True, text=True, timeout=1800)
    if out.returncode != 0:
        raise RuntimeError(f"judge failed: {out.stderr[-500:]} {out.stdout[-500:]}")
    res = json.loads(out.stdout)
    data = res.get("structured_output") or json.loads(res["result"])
    return {i["id"]: i for i in data["items"]}, res.get("total_cost_usd", 0)


def score_rubric(b, jdir, report, public, md):
    r = b.rubric()
    jobs = [p for p in r["parts"] for _ in range(b["judges"])]
    with ThreadPoolExecutor(max(len(jobs), 1)) as ex:
        results = list(ex.map(lambda p: judge(b, jdir, judge_prompt(b, p)), jobs))
    report["judge_cost_usd"] += sum(c for _, c in results)
    for part in r["parts"]:
        verdicts = [items for p, (items, _) in zip(jobs, results) if p is part]
        vals, split, groups, detail = [], [], {}, {}
        for it in part["items"]:
            vs = [v[it["id"]] for v in verdicts if it["id"] in v]
            got = [VALUE[v["verdict"]] for v in vs] or [0.0]
            val = sum(got) / len(got)
            vals.append(val)
            detail[it["id"]] = vs
            if len({v["verdict"] for v in vs}) > 1:
                split.append(it["id"])
            if it.get("group"):
                groups.setdefault(it["group"], []).append(val)
        pct = round(100 * sum(vals) / max(len(vals), 1), 1)
        report["parts"][part["id"]] = {"score_pct": pct, "items": len(vals), "judges_disagree": split, "detail": detail}
        public["parts"][part["id"]] = pct
        public["judges_disagree"][part["id"]] = len(split)
        if groups:
            public["by_group"][part["id"]] = {g: round(100 * sum(v) / len(v), 1) for g, v in groups.items()}
        md += [f"## {part['id']}: {pct}%（{len(vals)} 項目）", ""]
        for it, val in zip(part["items"], vals):
            if val < 1.0:
                md.append(f"- {it['id']}: " + " / ".join(f"{v['verdict']}（{v['evidence'][:80]}）" for v in detail[it["id"]]))
        if split:
            md.append(f"- 判定が割れた: {', '.join(split)}")
        md.append("")


def score_command(b, s, src, run_dir, report, public, md):
    """A command scores one part: its last line of output as 0-100, or 100 / 0 by how it exits.

    It runs where the worker left its result, with what the worker installed there. Hidden tests
    stay in private/ and are reached through HARNESSMITH_PRIVATE.
    """
    env = {**os.environ, "HARNESSMITH_PRIVATE": b.private, "HARNESSMITH_RUN": run_dir, "HARNESSMITH_DELIVERABLE": src}
    try:
        out = subprocess.run(s["cmd"], shell=True, cwd=src, env=env,
                             capture_output=True, text=True, timeout=s.get("timeout", 1800))
        code, text = out.returncode, out.stdout
        open(os.path.join(run_dir, f"score_{s['part']}.log"), "w").write(out.stdout + "\n--- stderr ---\n" + out.stderr)
    except subprocess.TimeoutExpired:
        code, text = 124, ""
    last = text.strip().splitlines()[-1].strip() if text.strip() else ""
    try:
        pct = round(min(100.0, max(0.0, float(last.rstrip("%")))), 1)
    except ValueError:
        pct = 100.0 if code == 0 else 0.0
    report["parts"][s["part"]] = {"score_pct": pct, "exit": code}
    public["parts"][s["part"]] = pct
    md += [f"## {s['part']}: {pct}%（コマンド、終了コード {code}）", ""]


def score(b, run_dir, src=None):
    src = src or os.path.join(run_dir, "work", b["deliverable"])
    if not os.path.isdir(src):
        return None
    # the judges read a copy, so nothing they do can touch the run's record
    jdir = os.path.join(run_dir, "judge")
    shutil.rmtree(jdir, ignore_errors=True)
    shutil.copytree(src, os.path.join(jdir, b["deliverable"]), ignore=shutil.ignore_patterns(*b["judge_ignore"]))
    report = {"run": os.path.basename(run_dir), "judges": b["judges"], "judge_cost_usd": 0.0, "parts": {},
              "rubric_hash": b.rubric_hash()}
    public = {"run": report["run"], "parts": {}, "by_group": {}, "judges_disagree": {}}
    md = [f"# 採点 {report['run']}", ""]
    for s in b["scorers"]:
        if s["type"] == "rubric":
            score_rubric(b, jdir, report, public, md)
        else:
            score_command(b, s, src, run_dir, report, public, md)
    report["judge_cost_usd"] = public["judge_cost_usd"] = round(report["judge_cost_usd"], 2)
    public["rubric_hash"] = report["rubric_hash"]
    # score.json and score.md hold the items: private. score_public.json holds points only
    json.dump(report, open(os.path.join(run_dir, "score.json"), "w"), ensure_ascii=False, indent=1)
    open(os.path.join(run_dir, "score.md"), "w").write("\n".join(md))
    json.dump(public, open(os.path.join(run_dir, "score_public.json"), "w"), ensure_ascii=False, indent=1)
    return public


def cmd_reference(a):
    """Score something made elsewhere (a person's version, an older system) and record what it was made from."""
    b = find_bench(a.bench)
    src = os.path.abspath(a.dir)
    run_dir = os.path.join(b.runs, f"reference-{a.name}")
    os.makedirs(run_dir, exist_ok=True)
    pub = score(b, run_dir, src)
    ev = b.append({"event": "reference", "name": a.name, "saw_answer": a.saw_answer, "provenance": a.provenance,
                   "parts": pub["parts"], "cost_usd": pub["judge_cost_usd"], "rubric_hash": pub["rubric_hash"]})
    print(json.dumps(ev, ensure_ascii=False, indent=1))


# ---- one trial ----

def write_events(run_dir):
    """What the worker did, in order: one line per tool call. The reflector reads this."""
    out = []
    for line in open(os.path.join(run_dir, "transcript.jsonl"), encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") != "assistant":
            continue
        for blk in (d.get("message") or {}).get("content") or []:
            if blk.get("type") == "tool_use":
                inp = blk.get("input") or {}
                tgt = (inp.get("file_path") or inp.get("description") or inp.get("pattern") or inp.get("url")
                       or inp.get("query") or inp.get("command") or "")
                tgt = " ".join(str(tgt).split())[:160].split("/work/", 1)[-1]
                out.append({"n": len(out) + 1, "tool": blk.get("name"), "target": tgt,
                            "sub": bool(d.get("parent_tool_use_id"))})
            elif blk.get("type") == "text" and blk.get("text", "").strip():
                out.append({"n": len(out) + 1, "say": " ".join(blk["text"].split())[:300]})
    with open(os.path.join(run_dir, "events.jsonl"), "w", encoding="utf-8") as f:
        for e in out:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def leak_traces(b, run_dir):
    """Lines of the transcript that touch what the worker must not see."""
    pats = [re.escape(os.path.realpath(b.root)), re.escape(os.path.realpath(b.private))] + list(b["forbidden"])
    rx = re.compile("|".join(pats))
    hits = []
    for n, line in enumerate(open(os.path.join(run_dir, "transcript.jsonl"), encoding="utf-8", errors="replace"), 1):
        if '"type":"system"' in line:
            continue
        m = rx.search(line)
        if m:
            hits.append(f"{n}: {m.group(0)}")
    return hits


def run_trial(b, comp, parent=None, blind=True):
    src = b.comp(comp)
    if not os.path.isfile(os.path.join(src, "PROMPT.md")):
        die(f"comp/{comp}/PROMPT.md がありません")
    if not os.path.isdir(b.materials) or not os.listdir(b.materials):
        die(f"{b.materials} に資料がありません")
    run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{comp}"
    run_dir = os.path.join(b.runs, run_id)
    work = os.path.join(run_dir, "work")
    os.makedirs(os.path.join(run_dir, "tmp"))
    shutil.copytree(src, work, ignore=shutil.ignore_patterns("HYPOTHESIS.md"))
    shutil.copytree(b.materials, os.path.join(work, "materials"))
    shutil.move(os.path.join(work, "PROMPT.md"), os.path.join(run_dir, "PROMPT.md"))
    # the worker may not read the bench or the answer. The rules go on the command line: in a file
    # of the work folder, the worker could read where those are
    deny = {"permissions": {"deny": [f"Read({rule(b.root)}/**)", f"Read({rule(b.private)}/**)",
                                     f"Grep({rule(b.private)}/**)", f"Glob({rule(b.private)}/**)"]}}
    subprocess.run("git init -q && git add -A && git commit -qm start --no-gpg-sign", shell=True, cwd=work,
                   capture_output=True)
    trust(work)
    spec = {"run_id": run_id, "comp": comp, "comp_hash": comp_hash(b, comp), "parent": parent, "blind": blind,
            "model": b["model"], "effort": b["effort"], "minutes": b["minutes"],
            "started": datetime.now().astimezone().isoformat(timespec="seconds")}
    log(f"{run_id} を開始（上限 {b['minutes']} 分・{b['max_usd_per_trial']} USD）。記録は {run_dir}")
    cmd = ["claude", "-p", open(os.path.join(run_dir, "PROMPT.md"), encoding="utf-8").read(),
           "--model", b["model"], "--effort", b["effort"], "--max-budget-usd", str(b["max_usd_per_trial"]),
           *ISOLATE, "--settings", json.dumps(deny), "--output-format", "stream-json", "--verbose"]
    if shutil.which("caffeinate"):
        cmd = ["caffeinate", "-i"] + cmd
    timed_out = False
    with open(os.path.join(run_dir, "transcript.jsonl"), "w") as out, open(os.path.join(run_dir, "stderr.log"), "w") as err:
        p = subprocess.Popen(cmd, cwd=work, env=claude_env({"TMPDIR": os.path.join(run_dir, "tmp")}),
                             stdin=subprocess.DEVNULL, stdout=out, stderr=err)
        try:
            p.wait(timeout=b["minutes"] * 60)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            timed_out = True
    spec.update(exit=p.returncode, timed_out=timed_out, ended=datetime.now().astimezone().isoformat(timespec="seconds"))
    json.dump(spec, open(os.path.join(run_dir, "spec.json"), "w"), ensure_ascii=False, indent=1)
    write_events(run_dir)
    _, cost, turns = stream_result(os.path.join(run_dir, "transcript.jsonl"))
    base = {"run": run_id, "comp": comp, "comp_hash": spec["comp_hash"], "parent": parent, "blind": blind,
            "model": b["model"], "effort": b["effort"], "turns": turns, "timed_out": timed_out,
            "minutes": round((datetime.fromisoformat(spec["ended"]) - datetime.fromisoformat(spec["started"])).total_seconds() / 60)}
    hits = leak_traces(b, run_dir)
    if hits:
        open(os.path.join(run_dir, "leak.txt"), "w").write("\n".join(hits))
        b.append({"event": "trial", **base, "cost_usd": {"agent": cost, "judge": 0}, "void": "見てはいけないものに触れた跡がある"})
        log(f"見てはいけないものに触れた跡があります（{run_dir}/leak.txt）。採点しません")
        return None
    pub = score(b, run_dir)
    if not pub:
        b.append({"event": "trial", **base, "cost_usd": {"agent": cost, "judge": 0},
                  "void": f"成果物 {b['deliverable']}/ がない" + ("（時間切れ）" if timed_out else "")})
        log(f"成果物 {b['deliverable']}/ がありません。採点しません")
        return None
    b.append({"event": "trial", **base, "cost_usd": {"agent": cost, "judge": pub["judge_cost_usd"]},
              "parts": pub["parts"], "by_group": pub["by_group"], "rubric_hash": pub["rubric_hash"]})
    log(f"{run_id}: " + " / ".join(f"{k} {v}" for k, v in pub["parts"].items()))
    return run_dir


def cmd_trial(a):
    b = find_bench(a.bench)
    if a.comp != b["start"] and not a.force:
        fails = [c for c in checks(b) if not c[1]]
        if fails:
            die("点検が済んでいません（harnessmith.py check）。先に済ませるか、--force を付けてください: "
                + "、".join(c[0] for c in fails))
    for _ in range(a.n):
        why = stop_reason(b)
        if why:
            die("止める: " + why)
        run_trial(b, a.comp, a.parent, blind=not a.unblind)


def cmd_score(a):
    b = find_bench(a.bench)
    pub = score(b, os.path.abspath(a.run_dir))
    print(json.dumps(pub, ensure_ascii=False, indent=1))
    log("採点し直しただけで、台帳には書いていません")


# ---- the state, rebuilt from the ledger ----

def state(b):
    evs = b.events()
    voided = {e["run"] for e in evs if e["event"] == "void"}
    all_trials = [e for e in evs if e["event"] == "trial"]
    scored = [t for t in all_trials if "parts" in t and t["run"] not in voided]
    # a trial scored with another rubric cannot be compared: it is kept out of the numbers
    cur = b.rubric_hash()
    stale_rubric = [t for t in scored if t.get("rubric_hash") != cur]
    trials = [t for t in scored if t.get("rubric_hash") == cur and t.get("blind", True)]
    unblind = [t for t in scored if t.get("rubric_hash") == cur and not t.get("blind", True)]
    parts = b.parts()
    comps = {}
    for t in trials:
        comps.setdefault(t["comp"], []).append(t)
    stats = {}
    for c, ts in comps.items():
        stats[c] = {"n": len(ts)}
        for p in parts:
            v = [t["parts"][p] for t in ts if p in t["parts"]]
            if v:
                stats[c][p] = {"mean": round(sum(v) / len(v), 1), "min": min(v), "max": max(v)}
    # noise: what the same configuration moves by. A difference inside it is not a difference
    noise = {}
    for p in parts:
        spreads = [s[p]["max"] - s[p]["min"] for s in stats.values() if s["n"] >= b["spread_trials"] and p in s]
        noise[p] = round(max([b["tolerance"]] + spreads), 1)
    decisions, adopted, stale = {}, b["start"], 0
    for e in evs:
        if e["event"] == "decision":
            decisions[e["comp"]] = e
            if e["decision"] == "adopt":
                adopted, stale = e["comp"], 0
            else:
                stale += 1
        elif e["event"] == "consult":
            stale = 0
    refs = {e["name"]: e for e in evs if e["event"] == "reference" and e.get("rubric_hash") == cur}
    targets = dict(b["targets"])
    tf = b.cfg.get("target_from")
    if tf and tf in refs:
        targets = {p: round(refs[tf]["parts"][p] - b["tolerance"], 1) for p in parts if p in refs[tf]["parts"]}
    done = None
    if targets:
        ok = [all(t["parts"].get(p, 0) >= v for p, v in targets.items()) for t in trials]
        for i in range(1, len(trials)):
            if ok[i] and ok[i - 1] and trials[i]["comp_hash"] == trials[i - 1]["comp_hash"]:
                done = trials[i]["comp"]
    spent = sum(t["cost_usd"]["agent"] + t["cost_usd"]["judge"] for t in all_trials)
    spent += sum(e.get("cost_usd", 0) for e in evs if e["event"] in ("aux", "reference"))
    return {"parts": parts, "trials": trials, "unblind": unblind, "stale_rubric": stale_rubric,
            "unscored": [t for t in all_trials if "parts" not in t or t["run"] in voided],
            "stats": stats, "noise": noise, "decisions": decisions, "adopted": adopted, "stale": stale,
            "refs": refs, "targets": targets, "done": done, "spent": round(spent, 2),
            "audits": [e for e in evs if e["event"] == "audit"]}


def checks(b, s=None):
    """The four checks before tuning. Skipping any of them made the first use of this method lose days."""
    s = s or state(b)
    out = []
    if b.uses_rubric():
        if not os.path.exists(b.rubric_path):
            out += [("採点表がある", False, "draft で書かせる"), ("採点表を別の目で照らした", False, "audit を回す")]
        else:
            last = s["audits"][-1] if s["audits"] else None
            ok = bool(last) and last["hash"] == b.rubric_hash() and not last["fixed"]
            why = ("点検していない" if not last else "点検のあとで採点表が変わった" if last["hash"] != b.rubric_hash()
                   else f"前回の点検で {len(last['fixed'])} 件直した。直しが 0 件になるまで回す" if last["fixed"] else
                   f"{last['checked']} 項目を照らし、直しは 0 件" + (f"（未決 {len(last['unsure'])} 件）" if last["unsure"] else ""))
            out.append(("採点表を別の目で照らした", ok, why))
            lacking = [p["id"] for p in b.rubric()["parts"] if len(p.get("criteria", "")) < 40]
            out.append(("判定の基準が書いてある", not lacking, "基準が短すぎる部分: " + "、".join(lacking) if lacking else "すべての部分にある"))
    tf = b.cfg.get("target_from")
    if tf:
        ref = s["refs"].get(tf)
        ok = bool(ref) and ref["saw_answer"] == "no"
        why = (f"比べる相手 {tf} が採点されていない" if not ref else
               f"{tf} は解答を見て作られた、または不明（saw_answer={ref['saw_answer']}）。同じ条件の版を基準にする" if not ok
               else f"{tf}: {ref['provenance']}")
        out.append(("比べる相手は同じ条件で作られた", ok, why))
    else:
        out.append(("比べる相手は同じ条件で作られた", True, "比べる相手を置いていない（目標は targets で直接決める）"))
    n = s["stats"].get(b["start"], {}).get("n", 0)
    out.append(("ぶれの幅を測った", n >= b["spread_trials"],
                f"素の構成 {b['start']} を {n} / {b['spread_trials']} 回回した"
                + ("。幅 " + "、".join(f"{p} ±{v}" for p, v in s["noise"].items()) if n >= b["spread_trials"] else "")))
    return out


def stop_reason(b, s=None):
    s = s or state(b)
    if s["done"]:
        return f"達成: 構成 {s['done']} が 2 回続けて目標に届いた"
    if s["spent"] + b["budget_per_gen_usd"] > b["budget_total_usd"]:
        return f"予算: {s['spent']} USD 使っていて、次の世代で上限 {b['budget_total_usd']} USD を超えるおそれ"
    if s["stale"] >= b["patience"]:
        return (f"改善のない世代が {s['stale']} 続いた。構成ではなく測る側（採点表・判定の基準・比べる相手）を点検し直す。"
                "続けるなら consult で理由を残す")
    return None


def cmd_check(a):
    b = find_bench(a.bench)
    res = checks(b)
    for name, ok, why in res:
        print(f"{'○' if ok else '×'} {name}: {why}")
    sys.exit(0 if all(ok for _, ok, _ in res) else 1)


def cmd_status(a):
    b = find_bench(a.bench)
    s = state(b)
    if a.json:
        print(json.dumps({**{k: s[k] for k in ("parts", "stats", "noise", "adopted", "stale", "targets", "done", "spent")},
                          "checks": [{"name": n, "ok": ok, "why": w} for n, ok, w in checks(b, s)],
                          "stop": stop_reason(b, s)}, ensure_ascii=False, indent=1))
        return
    parts = s["parts"]
    print(f"# {b.name}")
    print("\n## 点検")
    for name, ok, why in checks(b, s):
        print(f"{'○' if ok else '×'} {name}: {why}")
    print("\n## 構成ごとの点（解答を見ずに回した試行の平均。かっこは最小〜最大）")
    for c, st in s["stats"].items():
        d = s["decisions"].get(c)
        mark = "採用中" if c == s["adopted"] else {"adopt": "採用", "reject": "見送り"}.get(d["decision"], "") if d else "未判断"
        cells = "  ".join(f"{p} {st[p]['mean']}（{st[p]['min']}〜{st[p]['max']}）" for p in parts if p in st)
        print(f"- {c}（{st['n']} 回・{mark}）: {cells}")
    if s["targets"]:
        print("目標: " + "  ".join(f"{p} {v} 以上" for p, v in s["targets"].items()))
    print("ぶれの幅（これを超えた差だけを差と読む）: " + "  ".join(f"{p} {v}" for p, v in s["noise"].items()))
    for name, r in s["refs"].items():
        print(f"比べる相手 {name}（解答を見たか: {r['saw_answer']}。{r['provenance']}）: "
              + "  ".join(f"{p} {v}" for p, v in r["parts"].items()))
    for t in s["unblind"]:
        print(f"条件を外した試行（成果に数えない）{t['run']}: " + "  ".join(f"{p} {v}" for p, v in t["parts"].items()))
    if s["stale_rubric"]:
        print(f"採点表が変わる前の試行 {len(s['stale_rubric'])} 件は数えていない（score で採点し直せる）")
    for t in s["unscored"]:
        print(f"採点しなかった試行 {t['run']}: {t.get('void', '無効にした')}")
    for c, d in s["decisions"].items():
        print(f"判断 {c}: {d['decision']}（{d['by']}）{d['reason']}")
    print(f"\n使った額 {s['spent']} / {b['budget_total_usd']} USD、改善のない世代 {s['stale']} / {b['patience']}")
    why = stop_reason(b, s)
    print("止める: " + why if why else "続けられる")


# ---- deciding ----

def decide(b, cand, parent, by="rule"):
    s = state(b)
    c, p = s["stats"].get(cand), s["stats"].get(parent)
    if not c or not p:
        die(f"{cand} か {parent} に、採点された試行がありません")
    delta = {k: round(c[k]["mean"] - p[k]["mean"], 1) for k in s["parts"] if k in c and k in p}
    rose = [k for k, v in delta.items() if v > s["noise"][k]]
    fell = [k for k, v in delta.items() if v < -s["noise"][k]]
    adopt = bool(rose) and not fell
    reason = (f"親 {parent} との差（試行の平均。{cand} {c['n']} 回、{parent} {p['n']} 回）"
              + "、".join(f"{k} {v:+}（ぶれ {s['noise'][k]}）" for k, v in delta.items())
              + ("。ぶれの幅を超えて上がり、下がったものはない" if adopt else "。ぶれの幅を超える改善がない、または下がったものがある"))
    b.append({"event": "decision", "comp": cand, "decision": "adopt" if adopt else "reject", "reason": reason, "by": by})
    log(f"{cand}: {'採用' if adopt else '見送り'}（{reason}）")
    return adopt


def cmd_decide(a):
    b = find_bench(a.bench)
    if a.decision == "auto":
        decide(b, a.comp, a.parent or state(b)["adopted"])
    else:
        if not a.reason:
            die("--reason が要ります")
        print(json.dumps(b.append({"event": "decision", "comp": a.comp, "decision": a.decision, "reason": a.reason,
                                   "by": a.by}), ensure_ascii=False))


def cmd_void(a):
    print(json.dumps(find_bench(a.bench).append({"event": "void", "run": a.run, "reason": a.reason}), ensure_ascii=False))


def cmd_consult(a):
    print(json.dumps(find_bench(a.bench).append({"event": "consult", "note": a.note, "by": "human"}), ensure_ascii=False))


# ---- the reflector ----

def leakcheck(b, new, parent):
    """Words that are in the rubric and nowhere in the materials must not enter a configuration."""
    if not os.path.exists(b.rubric_path):
        return []

    def text_of(root, skip=()):
        out = []
        for dp, _, fs in os.walk(root):
            for f in fs:
                if f in skip:
                    continue
                try:
                    out.append(open(os.path.join(dp, f), encoding="utf-8").read())
                except (UnicodeDecodeError, OSError):
                    pass
        return "\n".join(out)

    words = set()
    for part in b.rubric()["parts"]:
        for it in part["items"]:
            words.add(it["text"])
            # pieces of 4 characters or more, so a paraphrase that keeps a phrase is caught
            words.update(w for w in re.split(r"[、。・／/（）()「」\s,.:;]+", it["text"]) if len(w) >= 4)
    a, p, m = text_of(b.comp(new), ("HYPOTHESIS.md",)), text_of(b.comp(parent), ("HYPOTHESIS.md",)), text_of(b.materials)
    return sorted(w for w in words if w in a and w not in p and w not in m)


def cmd_leakcheck(a):
    b = find_bench(a.bench)
    leak = leakcheck(b, a.new, a.parent)
    if leak:
        die(f"comp/{a.new} に、採点表にだけある言葉が {len(leak)} 件入っています。採用しないでください")
    log("採点表にだけある言葉は入っていない")


def reflect(b, run_dir, base):
    spec = json.load(open(os.path.join(run_dir, "spec.json"), encoding="utf-8"))
    comp, run_id = spec["comp"], spec["run_id"]
    base = base or comp
    inp = os.path.join(run_dir, "reflect")
    shutil.rmtree(inp, ignore_errors=True)
    os.makedirs(inp)
    for f in ("score_public.json", "events.jsonl", "spec.json"):
        shutil.copy(os.path.join(run_dir, f), inp)
    shutil.copytree(os.path.join(run_dir, "work", b["deliverable"]), os.path.join(inp, b["deliverable"]),
                    ignore=shutil.ignore_patterns(*b["judge_ignore"]))
    status = subprocess.run([sys.executable, os.path.abspath(__file__), "--bench", b.root, "status"],
                            capture_output=True, text=True).stdout
    open(os.path.join(inp, "ledger_status.txt"), "w").write(status)
    # .claude cannot be written without a prompt, so the copy carries it under another name
    shutil.copytree(b.comp(base), os.path.join(inp, "proposal"))
    if os.path.isdir(os.path.join(inp, "proposal", ".claude")):
        os.rename(os.path.join(inp, "proposal", ".claude"), os.path.join(inp, "proposal", "dot-claude"))
    trust(b.root)
    i = rule(inp)
    hidden = ("score.json", "score.md", "judge/**", "work/**", "transcript.jsonl", "leak.txt")
    settings = {"permissions": {
        "allow": ["Read", "Glob", "Grep", f"Write({i}/**)", f"Edit({i}/**)"],
        "deny": [f"Read({rule(b.private)}/**)", f"Grep({rule(b.private)}/**)", f"Glob({rule(b.private)}/**)",
                 "Bash", "WebFetch", "WebSearch", "Task"]
        + [f"Read({rule(b.runs)}/*/{h})" for h in hidden]}}
    gen = 1 + max([int(m.group(1)) for d in os.listdir(os.path.join(b.root, "comp"))
                   for m in [re.match(r"g(\d+)-", d)] if m] or [1])
    extra = lambda title, xs: (f"\n## {title}\n" + "\n".join(f"- {x}" for x in xs) + "\n") if xs else ""
    prompt = f"""あなたは、作業エージェントに解答を見せずに仕事をさせるハーネスの「振り返り役」です。
カレントディレクトリはベンチのフォルダです。README.md、comp/ の各構成（HYPOTHESIS.md があれば親と変更点）、notes/ のこれまでの振り返りを読んでください。

## ゴール
{b['goal'] or '作業エージェントが解答を見ずに、採点の各部分で高い点を取れる構成を見つける。'}
採点の部分: {'、'.join(b.parts())}

## 今回の材料（{inp}）
- score_public.json: 今回の点数（部分ごと、あればグループごと）。項目ごとの結果は、あなたには見せません
- ledger_status.txt: これまでの構成の点、ぶれの幅、判断の一覧
- events.jsonl: 作業エージェントが何をしたかの順番
- {b['deliverable']}/: 作業エージェントの成果物
- {b.materials}/: 作業エージェントに渡したのと同じ資料
- 試行 {run_id} の構成は comp/{comp}。次の案の親は comp/{base}（試行の構成と違うときは、試行の構成は見送られた）

## やること
1. 点数の低い部分・グループについて、成果物の何が原因になっていそうかを、成果物そのものから推し量る。
   点数だけでは何が欠けたかは分からない。資料を自分で読み、資料が求めているのに成果物に現れていないものを探す。
   それがなぜ落ちたのか（読まなかった、読んだが外した、外した理由は何か）を、events.jsonl と成果物の記述から確かめる。
   見つけたものの名前は構成に書き込まない。落ちた理由をなくす手当てにする。
2. 原因の仮説を 1 つに絞る。
3. {inp}/proposal/ は親（comp/{base}）の写しです（.claude は dot-claude という名前にしてあります）。ここに変更を 1 つだけ入れる。
4. 同じ proposal/ に HYPOTHESIS.md を書く。親・変えたこと・仮説・期待する効果（どの部分がどう動くか）・効かなかったときに分かること。
5. 新しい構成の名前を {inp}/name.txt に 1 行で書き、HYPOTHESIS.md の最初の見出しにも「# <名前>」として書く。名前は g{gen}-<変更の短い英語名>（例 g{gen}-scope-rule）。
   どの部分も十分で、変えるところがないと判断したら、proposal/ は変えず、name.txt も書かず、notes.md にその理由を書く。
6. 振り返りを {inp}/notes.md に書く。見出しは「条件」「結果」「何が起きたか」「よかったこと」「足りなかったこと」「次の手当て」。
書き込めるのは、この 3 つ（proposal/、name.txt、notes.md）だけです。コマンドは使えません。

## 手当ての選び方
- 素の構成で十分な部分には、何も足さない。足すと、ほかの部分が下がる。
- 効きやすいのは、課題文の読み方が割れるところを、どちらに読むか決める指示。工程（見直し、対応表、別の目）を足す指示は、効かないことが多い。
- 足してよいのは、作業エージェントが資料から知りえないこと（置き場所、提出の決まり、道具の使い方）と、読み方の決めごと。
{extra('このベンチで足してよいもの', b['may_add'])}
## してはいけないこと
- 解答や採点表を探したり読んだりすること。点数から足りない項目を当てて、その名前を構成に書き込むこと
- 資料を読めば分かることを、答えとして与えること
- 一度に 2 つ以上を変えること
- 1 回の結果だけで良し悪しを断定すること（同じ構成でも点は ledger_status.txt の「ぶれの幅」だけ動く）
{extra('このベンチでしてはいけないこと', b['must_not'])}
最後の 1 行は、次の形にしてください。
HYPOTHESIS=<仮説を 1 行で>"""
    log(f"{run_id} を振り返り中")
    with open(os.path.join(run_dir, "reflect.jsonl"), "w") as out, open(os.path.join(run_dir, "reflect.stderr"), "w") as err:
        subprocess.run(["claude", "-p", prompt, "--model", b["judge_model"], "--effort", b["judge_effort"], *ISOLATE,
                        "--settings", json.dumps(settings), "--add-dir", inp, "--add-dir", b.materials,
                        "--output-format", "stream-json", "--verbose"],
                       cwd=b.root, env=claude_env(), stdin=subprocess.DEVNULL, stdout=out, stderr=err)
    text, cost, _ = stream_result(os.path.join(run_dir, "reflect.jsonl"))
    b.append({"event": "aux", "what": "reflect", "run": run_id, "cost_usd": cost})
    name_file = os.path.join(inp, "name.txt")
    name = "".join(open(name_file).read().split()) if os.path.exists(name_file) else ""
    hyp_file = os.path.join(inp, "proposal", "HYPOTHESIS.md")
    if not name and os.path.exists(hyp_file):  # name.txt could not be written: the first heading has it too
        m = re.search(r"^# *(g\d+-[a-z0-9-]+)", open(hyp_file, encoding="utf-8").read(), re.M)
        name = m.group(1) if m else ""
    if os.path.exists(os.path.join(inp, "notes.md")):
        os.makedirs(os.path.join(b.root, "notes"), exist_ok=True)
        shutil.copy(os.path.join(inp, "notes.md"), os.path.join(b.root, "notes", f"{run_id}.md"))
    same = subprocess.run(["diff", "-rq", "-x", "HYPOTHESIS.md", "-x", "dot-claude", "-x", ".claude",
                           b.comp(base), os.path.join(inp, "proposal")], capture_output=True).returncode == 0
    dot = os.path.join(inp, "proposal", "dot-claude")
    if same and os.path.isdir(dot):
        same = subprocess.run(["diff", "-rq", os.path.join(b.comp(base), ".claude"), dot], capture_output=True).returncode == 0
    if same:
        log(f"振り返り役は、変えるところがないと判断しました（notes/{run_id}.md）")
        return None
    if not re.fullmatch(r"g\d+-[a-z0-9-]+", name):
        log(f"振り返り役が構成の名前を書けませんでした（{run_dir}/reflect.jsonl）")
        return None
    if os.path.exists(b.comp(name)):
        log(f"comp/{name} はもうあります")
        return None
    if not os.path.exists(os.path.join(inp, "proposal", "HYPOTHESIS.md")):
        log("HYPOTHESIS.md がありません")
        return None
    shutil.copytree(os.path.join(inp, "proposal"), b.comp(name))
    if os.path.isdir(os.path.join(b.comp(name), "dot-claude")):
        os.rename(os.path.join(b.comp(name), "dot-claude"), os.path.join(b.comp(name), ".claude"))
    leak = leakcheck(b, name, base)
    if leak:
        # the words themselves are the answer: they stay in the private folder
        open(os.path.join(b.private, f"leak-{name}.txt"), "w").write("\n".join(leak))
        shutil.rmtree(b.comp(name))
        log(f"提案 {name} に、採点表にだけある言葉が {len(leak)} 件入っていたので捨てました")
        return None
    hyp = [l.split("=", 1)[1] for l in text.splitlines() if l.startswith("HYPOTHESIS=")]
    log(f"次の案 comp/{name}: {hyp[-1] if hyp else ''}")
    return name


def cmd_reflect(a):
    b = find_bench(a.bench)
    name = reflect(b, os.path.abspath(a.run_dir), a.base)
    if not name:
        sys.exit(1)
    print(f"NEXT_COMP={name}")


# ---- the loop ----

def cmd_auto(a):
    b = find_bench(a.bench)
    fails = [c for c in checks(b) if not c[1]]
    if fails and not a.force:
        die("点検が済んでいません（harnessmith.py check）: " + "、".join(c[0] for c in fails))
    parent = a.parent or state(b)["adopted"]
    cand = a.next
    if not cand:
        ts = [t for t in state(b)["trials"] if t["comp"] == parent]
        if not ts:
            die(f"{parent} に採点された試行がありません")
        cand = reflect(b, os.path.join(b.runs, ts[-1]["run"]), parent)
        if not cand:
            die("振り返り役が次の案を出せませんでした")
    for _ in range(a.max_rounds):
        why = stop_reason(b)
        if why:
            log("止める: " + why)
            return
        log(f"{cand} を {b['trials_per_candidate']} 回回す（親 {parent}）")
        last = None
        for _ in range(b["trials_per_candidate"]):
            last = run_trial(b, cand, parent)
            if not last:
                log("止める: 試行が採点されなかった")
                return
        if state(b)["done"] == cand:
            decide(b, cand, parent)
            log("達成: " + stop_reason(b))
            return
        if decide(b, cand, parent):
            parent = cand
        cand = reflect(b, last, parent)
        if not cand:
            log("止める: 振り返り役が次の案を出せなかった、または漏れの検査に引っかかった")
            return
    log("止める: 決めた周回数に達した")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bench", default=".", help="the bench folder (default: found from the current folder)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init"); p.add_argument("dir"); p.add_argument("--name", required=True)
    p.add_argument("--deliverable", default="out"); p.set_defaults(fn=cmd_init)
    p = sub.add_parser("draft"); p.add_argument("--parts", required=True); p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_draft)
    sub.add_parser("audit").set_defaults(fn=cmd_audit)
    p = sub.add_parser("reference"); p.add_argument("dir"); p.add_argument("--name", required=True)
    p.add_argument("--saw-answer", required=True, choices=["yes", "no", "unknown"])
    p.add_argument("--provenance", required=True); p.set_defaults(fn=cmd_reference)
    p = sub.add_parser("trial"); p.add_argument("-c", "--comp", required=True); p.add_argument("--parent")
    p.add_argument("-n", type=int, default=1); p.add_argument("--force", action="store_true")
    p.add_argument("--unblind", action="store_true", help="the configuration was written knowing the answer: kept apart")
    p.set_defaults(fn=cmd_trial)
    p = sub.add_parser("score"); p.add_argument("run_dir"); p.set_defaults(fn=cmd_score)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    p = sub.add_parser("status"); p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_status)
    p = sub.add_parser("decide"); p.add_argument("comp"); p.add_argument("decision", choices=["adopt", "reject", "auto"])
    p.add_argument("--parent"); p.add_argument("--reason"); p.add_argument("--by", default="human"); p.set_defaults(fn=cmd_decide)
    p = sub.add_parser("void"); p.add_argument("run"); p.add_argument("--reason", required=True); p.set_defaults(fn=cmd_void)
    p = sub.add_parser("consult"); p.add_argument("--note", required=True); p.set_defaults(fn=cmd_consult)
    p = sub.add_parser("leakcheck"); p.add_argument("new"); p.add_argument("parent"); p.set_defaults(fn=cmd_leakcheck)
    p = sub.add_parser("reflect"); p.add_argument("run_dir"); p.add_argument("base", nargs="?"); p.set_defaults(fn=cmd_reflect)
    p = sub.add_parser("auto"); p.add_argument("--parent"); p.add_argument("--next")
    p.add_argument("--max-rounds", type=int, default=4); p.add_argument("--force", action="store_true"); p.set_defaults(fn=cmd_auto)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
