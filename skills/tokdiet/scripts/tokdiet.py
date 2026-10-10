#!/usr/bin/env python3
"""tokdiet M0 -- Claude Code ログの原因別「無駄」診断（ローカル完結・標準ライブラリのみ）

ネットワークアクセスなし。~/.claude/projects/**/*.jsonl を読み、集計値だけのレポート
(Markdown + 自己完結HTML) を出力する。プロンプト本文・パス・リポジトリ名は出力しない。

使い方: python3 tokdiet.py [--root ~/.claude/projects] --out <出力ディレクトリ>
"""
import argparse, bisect, hashlib, html, json, os, re, secrets, statistics, sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

VERSION = "M0.1"
BUILTIN_TOOLS = {"Read", "Edit", "Write", "Bash", "Grep", "Glob", "Task", "Agent", "WebFetch",
                 "WebSearch", "TodoWrite", "Skill", "NotebookEdit", "AskUserQuestion", "ToolSearch",
                 "MultiEdit", "BashOutput", "KillShell", "TaskStop", "Monitor", "SendMessage"}
AGENT_TOOLS = {"Task", "Agent"}
# 概算の API 単価 ($/100万トークン, 入力, 出力)。同梱の固定値でネットワーク取得はしない。要・公式で再確認。
PRICES = {"opus": (5.0, 25.0), "sonnet": (3.0, 15.0), "haiku": (1.0, 5.0)}
BIG_RESULT_CHARS = 40_000       # 約1万トークン超のツール結果を「巨大」とみなす（推定）
CTX_DROP_RATIO = 0.5            # compaction 推定: 文脈が直前の半分未満に落ちる
CTX_DROP_MIN = 60_000           # かつ直前が6万トークン以上
INSTR_CHARS_WARN = 20_000       # 常駐指示ファイルの目安（推定）
SKILLS_WARN = 30


def ts_parse(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def h(*parts):
    return hashlib.sha1("\x00".join(map(str, parts)).encode("utf-8", "replace")).hexdigest()[:12]


def norm(s):
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")


def family(model):
    m = (model or "").lower()
    for f in PRICES:
        if f in m:
            return f
    return None


def safe_model(model):
    return model if re.fullmatch(r"claude-[a-z0-9.\-]{1,40}", model or "") else "other-model"


def parse_skill_listing(content, names=None):
    """スキル一覧テキスト -> {スキル名: 説明の文字数}。名前に ':' を含むもの(plugin:skill)も扱う。
    names(attachmentの名前一覧)があれば、その名前の行頭だけを区切りにする(説明内の「- x: y」を誤分割しない)。"""
    if not isinstance(content, str):
        return {}
    ms = [m for m in re.finditer(r"(?m)^- (\S+?): ", content) if (not names) or m.group(1) in names]
    out = {}
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(content)
        out[m.group(1)] = len(content[m.end():end].strip())
    return out


def text_len(c):
    if isinstance(c, str):
        return len(c)
    if isinstance(c, list):
        return sum(len(x.get("text", "")) for x in c if isinstance(x, dict) and isinstance(x.get("text"), str))
    return 0


# ---------------------------------------------------------------- 読み込み
def iter_files(root):
    for dp, _, fns in os.walk(root):
        for fn in fns:
            if fn.endswith(".jsonl"):
                yield os.path.join(dp, fn)


def scan_skill_calls(root):
    """root 配下の全ログでの Skill 呼出（tool_use の一意id）と <command-name> の回数。軽量走査（他の集計はしない）。"""
    ids, slash = {}, Counter()
    for path in iter_files(root):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"name":"Skill"' in line:
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    for b in ((d.get("message") or {}).get("content") or []) if isinstance(d.get("message"), dict) else []:
                        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Skill" and isinstance(b.get("input"), dict):
                            ids[b.get("id")] = str(b["input"].get("skill", ""))
                elif "<command-name>" in line:
                    for m in re.findall(r"<command-name>/?([^<\s]{1,80})</command-name>", line):
                        slash[m] += 1
    c = Counter(ids.values())
    for k, v in slash.items():
        c[k] += v
    return c


def locate(root, path):
    """(project_dir, session_id, is_subagent_file)"""
    rel = os.path.relpath(path, root).split(os.sep)
    proj = rel[0]
    if len(rel) >= 3 and "subagents" in rel:
        return proj, rel[1], True
    return proj, os.path.splitext(rel[-1])[0], False


def load(root):
    reqs = {}          # requestId -> dict
    tools = {}         # tool_use_id -> dict
    results = {}       # tool_use_id -> chars
    sess = defaultdict(lambda: {"skill_listing": 0, "skill_count": 0, "instr_chars": 0, "mcp_announced": set(),
                                "mcp_block_chars": 0, "agent_files": set(), "markers": 0, "skills": {}, "skills_logged": 0})
    limits = []        # rate_limit event timestamps
    files = 0
    usage_files = set()
    ident = {"proj": set(), "cwd": set(), "sid": set()}
    detail = {"instr": {}, "skill_listing": "", "agent_type": {}, "cwds": set(), "slash": Counter(), "first_cmd": {}}   # ローカル詳細版(actionbrief.py)専用。匿名レポートには使わない
    for path in iter_files(root):
        files += 1
        proj, sid, is_sub = locate(root, path)
        ident["proj"].add(proj); ident["sid"].add(sid)
        atype = None
        if is_sub:
            sess[(proj, sid)]["agent_files"].add(path)
            try:
                with open(path[:-6] + ".meta.json", encoding="utf-8") as mf:
                    atype = str(json.load(mf).get("agentType") or "") or None
            except Exception:
                atype = None
            detail["agent_type"][path] = atype
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if not isinstance(d, dict):
                    continue
                t = d.get("type")
                if d.get("cwd"):
                    ident["cwd"].add(d["cwd"])
                    detail["cwds"].add(d["cwd"])
                key = (proj, sid)
                if t == "assistant" and isinstance(d.get("message"), dict):
                    m = d["message"]
                    ts = ts_parse(d.get("timestamp", ""))
                    if d.get("error") == "rate_limit" and ts:
                        limits.append(ts)
                    if m.get("model") == "<synthetic>" or not isinstance(m.get("usage"), dict):
                        continue
                    rid = d.get("requestId") or m.get("id") or d.get("uuid")
                    u = m["usage"]
                    cc = u.get("cache_creation") or {}
                    cur = {"inp": u.get("input_tokens") or 0, "out": u.get("output_tokens") or 0,
                           "cr": u.get("cache_read_input_tokens") or 0,
                           "cw": u.get("cache_creation_input_tokens") or 0,
                           "cw1h": cc.get("ephemeral_1h_input_tokens") or 0}
                    usage_files.add(path)
                    r = reqs.get(rid)
                    if r is None:
                        reqs[rid] = {"proj": proj, "sid": sid, "side": bool(d.get("isSidechain")) or is_sub,
                                     "model": m.get("model") or "", "ts": ts, "afile": path if is_sub else None,
                                     "agent": atype, **cur}
                    else:   # 同一requestIdの重複行(ブロック分割)は畳む: 各値は最大を採る
                        for k, v in cur.items():
                            r[k] = max(r[k], v)
                        if ts and (r["ts"] is None or ts < r["ts"]):
                            r["ts"] = ts
                    for b in m.get("content") or []:
                        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") not in tools:
                            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                            name = b.get("name") or ""
                            rec = {"name": name, "proj": proj, "sid": sid, "side": reqs[rid]["side"],
                                   "agent": atype if is_sub else None}
                            if name == "Read":
                                rec["pathkey"] = h(sid, inp.get("file_path", ""))
                                rec["path"] = inp.get("file_path", "")
                                rec["ranged"] = ("offset" in inp) or ("limit" in inp)
                            elif name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                                rec["epath"] = inp.get("file_path", "") or inp.get("notebook_path", "")
                                rec["editkey"] = h(sid, inp.get("file_path", "") or inp.get("notebook_path", ""))
                            elif name == "Bash":
                                c = str(inp.get("command", "")).strip()
                                rec["cmdkey"] = h(c) if len(c) >= 30 else None
                                rec["cmd"] = c if len(c) >= 30 else None
                            elif name == "Skill":
                                rec["skill"] = h(inp.get("skill", ""))
                                rec["skill_name"] = str(inp.get("skill", ""))
                            tools[b["id"]] = rec
                elif t == "user" and isinstance(d.get("message"), dict):
                    if d.get("isCompactSummary"):
                        sess[key]["markers"] += 1
                    c = d["message"].get("content")
                    if isinstance(c, str) and "<command-name>" in c:
                        mm = re.search(r"<command-name>/?([^<\s]{1,80})</command-name>", c)
                        if mm:
                            detail["slash"][mm.group(1)] += 1
                            detail["first_cmd"].setdefault(key, mm.group(1))
                    if isinstance(c, list):
                        for b in c:
                            if isinstance(b, dict) and b.get("type") == "tool_result":
                                tid = b.get("tool_use_id")
                                results[tid] = max(results.get(tid, 0), text_len(b.get("content")))
                elif t == "system" and d.get("subtype") == "compact_boundary":
                    sess[key]["markers"] += 1
                elif t == "attachment" and isinstance(d.get("attachment"), dict):
                    a = d["attachment"]; at = a.get("type")
                    if at == "skill_listing" and not is_sub:   # サブエージェントのログは除く（メインセッションの一覧のみ）
                        sess[key]["skills_ts"] = max(sess[key].get("skills_ts") or 0, ts_parse(d.get("timestamp", "")) or 0)
                        sess[key]["skill_listing"] = max(sess[key]["skill_listing"], text_len(a.get("content")))
                        sess[key]["skill_count"] = max(sess[key]["skill_count"], a.get("skillCount") or 0)
                        sess[key]["skills_logged"] = max(sess[key]["skills_logged"], a.get("skillCount") or 0)
                        nm = set(a["names"]) if isinstance(a.get("names"), list) else None
                        for k2, v2 in parse_skill_listing(a.get("content"), nm).items():
                            sess[key]["skills"][k2] = max(sess[key]["skills"].get(k2, 0), v2)
                    elif at == "instructions":
                        n = sum(len(f.get("content", "")) for f in a.get("files") or [] if isinstance(f, dict))
                        sess[key]["instr_chars"] = max(sess[key]["instr_chars"], n)
                        for f in a.get("files") or []:
                            if isinstance(f, dict) and f.get("path") and isinstance(f.get("content"), str):
                                e = detail["instr"].setdefault(f["path"], {"content": "", "sessions": set()})
                                e["sessions"].add(key)
                                if len(f["content"]) > len(e["content"]):
                                    e["content"] = f["content"]
                    elif at == "mcp_instructions_delta":
                        sess[key]["mcp_announced"].update(norm(x) for x in a.get("addedNames") or [])
                        sess[key]["mcp_block_chars"] += sum(len(str(x)) for x in a.get("addedBlocks") or [])
    return dict(reqs=reqs, tools=tools, results=results, sess=sess, limits=limits, files=files, ident=ident, detail=detail, usage_files=usage_files)


# ---------------------------------------------------------------- 集計
def cost_of(r):
    f = family(r["model"])
    if not f:
        return None
    pi, po = PRICES[f]
    cw5 = r["cw"] - r["cw1h"]
    return (r["inp"] * pi + r["out"] * po + r["cr"] * pi * 0.1 + cw5 * pi * 1.25 + r["cw1h"] * pi * 2.0) / 1e6


def pct(a, b):
    return round(100.0 * a / b, 1) if b else 0.0


def analyze(L):
    reqs, tools, results, sess = L["reqs"], L["tools"], L["results"], L["sess"]
    R = list(reqs.values())
    A = {"files": L["files"]}
    tot = lambda rs, k: sum(r[k] for r in rs)
    for r in R:
        r["ctx"] = r["inp"] + r["cr"] + r["cw"]
    # 匿名ID: トークン量順
    pv = defaultdict(int)
    for r in R:
        pv[r["proj"]] += r["ctx"] + r["out"]
    pid = {p: "P%d" % (i + 1) for i, (p, _) in enumerate(sorted(pv.items(), key=lambda x: -x[1]))}
    A["projects"] = len(pid)
    ts = [r["ts"] for r in R if r["ts"]]
    A["period"] = (datetime.fromtimestamp(min(ts), timezone.utc).strftime("%Y-%m-%d") if ts else "-",
                   datetime.fromtimestamp(max(ts), timezone.utc).strftime("%Y-%m-%d") if ts else "-")
    A["requests"] = len(R)
    A["sessions"] = len({(r["proj"], r["sid"]) for r in R})
    A["tok"] = {k: tot(R, k) for k in ("inp", "out", "cr", "cw")}
    ctx_total = A["tok"]["inp"] + A["tok"]["cr"] + A["tok"]["cw"]
    A["cache_rate"] = pct(A["tok"]["cr"], ctx_total)
    side = [r for r in R if r["side"]]
    A["side_req_share"] = pct(len(side), len(R))
    A["side_tok_share"] = pct(sum(r["ctx"] + r["out"] for r in side), ctx_total + A["tok"]["out"])
    # モデル別
    bym = defaultdict(list)
    for r in R:
        bym[safe_model(r["model"])].append(r)
    A["models"] = []
    unpriced = 0
    for m, rs in sorted(bym.items(), key=lambda x: -sum(r["ctx"] + r["out"] for r in x[1])):
        cs = [cost_of(r) for r in rs]
        priced = [c for c in cs if c is not None]
        unpriced += len(cs) - len(priced)
        A["models"].append({"model": m, "req": len(rs), "inp": tot(rs, "inp"), "out": tot(rs, "out"),
                            "cr": tot(rs, "cr"), "cw": tot(rs, "cw"),
                            "cache": pct(tot(rs, "cr"), sum(r["ctx"] for r in rs)),
                            "cost": round(sum(priced), 2) if priced else None})
    A["cost_total"] = round(sum(m["cost"] or 0 for m in A["models"]), 2)
    A["unpriced_req"] = unpriced
    # プロジェクト別（匿名）
    byp = defaultdict(list)
    for r in R:
        byp[pid[r["proj"]]].append(r)
    A["by_project"] = []
    for p, rs in sorted(byp.items(), key=lambda x: int(x[0][1:])):
        A["by_project"].append({"id": p, "sessions": len({r["sid"] for r in rs}), "req": len(rs),
                                "tok": sum(r["ctx"] + r["out"] for r in rs),
                                "cache": pct(tot(rs, "cr"), sum(r["ctx"] for r in rs))})
    # ツール別
    mcp_ids = {}
    def tname(n):
        if n in BUILTIN_TOOLS:
            return n
        if n.startswith("mcp__"):
            s = norm(n.split("__")[1] if n.count("__") >= 2 else n)
            mcp_ids.setdefault(s, "MCP-%d" % (len(mcp_ids) + 1))
            return mcp_ids[s]
        return "other"
    tc, tr_chars, tr_n = Counter(), Counter(), Counter()
    for tid, t in tools.items():
        n = tname(t["name"]); tc[n] += 1
        if tid in results:
            tr_chars[n] += results[tid]; tr_n[n] += 1
    A["tools"] = [{"tool": n, "calls": c, "result_chars": tr_chars[n],
                   "avg_chars": round(tr_chars[n] / tr_n[n]) if tr_n[n] else 0}
                  for n, c in tc.most_common(15)]
    # --- 原因別 ---
    # 1) 常駐オーバーヘッド
    firsts, per_sess_reqs = [], defaultdict(list)
    for r in R:
        if not r["side"] and r["ts"]:
            per_sess_reqs[(r["proj"], r["sid"])].append(r)
    for k, rs in per_sess_reqs.items():
        rs.sort(key=lambda x: x["ts"])
    for rs in per_sess_reqs.values():
        firsts.append(rs[0]["ctx"])
    main_ctx = sum(r["ctx"] for rs in per_sess_reqs.values() for r in rs)
    resident = sum(rs[0]["ctx"] * len(rs) for rs in per_sess_reqs.values())
    inst = [s["instr_chars"] for s in sess.values() if s["instr_chars"]]
    skl = [s["skill_listing"] for s in sess.values() if s["skill_listing"]]
    skc = [s["skill_count"] for s in sess.values() if s["skill_count"]]
    mcpb = [s["mcp_block_chars"] for s in sess.values() if s["mcp_block_chars"]]
    A["resident"] = {"sessions": len(firsts),
                     "first_ctx_median": int(statistics.median(firsts)) if firsts else 0,
                     "share_of_main_ctx": pct(resident, main_ctx),
                     "instr_chars_median": int(statistics.median(inst)) if inst else None,
                     "instr_chars_max": max(inst) if inst else None,
                     "skill_chars_median": int(statistics.median(skl)) if skl else None,
                     "skill_count_median": int(statistics.median(skc)) if skc else None,
                     "mcp_block_chars_median": int(statistics.median(mcpb)) if mcpb else None}
    # 2) 未使用 MCP / スキル
    used_srv = set()
    for t in tools.values():
        if t["name"].startswith("mcp__") and t["name"].count("__") >= 2:
            used_srv.add(norm(t["name"].split("__")[1]))
    ann = set().union(*[s["mcp_announced"] for s in sess.values()]) if sess else set()
    unused = [a for a in ann if not any(a in u or u in a for u in used_srv if u and a)]
    A["mcp"] = {"announced": len(ann), "unused": len(unused), "used_servers": len(used_srv)}
    skills_used = {t["skill"] for t in tools.values() if t.get("skill")}
    A["skills"] = {"listed_median": int(statistics.median(skc)) if skc else None, "used_distinct": len(skills_used)}
    # 3) 再読込 / 範囲読み
    reads = [(tid, t) for tid, t in tools.items() if t["name"] == "Read"]
    pc = Counter(t["pathkey"] for _, t in reads)
    # 再読込: 前回の読込以降に同ファイルへの編集が無い読込のみ数える（編集後の確認読みは除く）
    last_read, dirty, rer, re_chars = set(), set(), 0, 0
    for tid, t in tools.items():
        if t.get("editkey"):
            dirty.add(t["editkey"])
        elif t["name"] == "Read":
            k = t["pathkey"]
            if k in last_read and k not in dirty:
                rer += 1
                re_chars += results.get(tid, 0)
            last_read.add(k)
            dirty.discard(k)
    big_unranged = [results[tid] for tid, t in reads if not t["ranged"] and results.get(tid, 0) >= BIG_RESULT_CHARS // 2]
    A["reread"] = {"reads": len(reads), "distinct": len(pc), "rereads": rer,
                   "reread_rate": pct(rer, len(reads)), "reread_chars": re_chars,
                   "big_unranged": len(big_unranged), "big_unranged_chars": sum(big_unranged),
                   "repeat_bash": sum(c - 1 for c in Counter(t.get("cmdkey") for t in tools.values()
                                                             if t["name"] == "Bash" and t.get("cmdkey")).values() if c > 2)}
    # 4) 巨大ツール結果
    allr = sorted(((results[tid], tname(tools[tid]["name"])) for tid in results if tid in tools), reverse=True)
    big = [x for x in allr if x[0] >= BIG_RESULT_CHARS]
    total_chars = sum(c for c, _ in allr)
    A["bigres"] = {"results": len(allr), "big": len(big), "big_chars": sum(c for c, _ in big),
                   "share": pct(sum(c for c, _ in big), total_chars),
                   "by_tool": Counter(n for _, n in big).most_common(5)}
    # 5) compaction
    marker = sum(s["markers"] for s in sess.values())
    est = 0
    for rs in per_sess_reqs.values():
        for a, b in zip(rs, rs[1:]):
            if a["ctx"] >= CTX_DROP_MIN and b["ctx"] < a["ctx"] * CTX_DROP_RATIO:
                est += 1
    A["compaction"] = {"marker": marker, "est_drops": est, "sessions": len(per_sess_reqs),
                       "per_100_sessions": round(100.0 * est / len(per_sess_reqs), 1) if per_sess_reqs else 0}
    # 6) キャッシュ率低下セッション
    bys = defaultdict(list)
    for r in R:
        bys[(r["proj"], r["sid"])].append(r)
    low = []
    for k, rs in bys.items():
        c = sum(r["ctx"] for r in rs)
        if c >= 500_000 and pct(sum(r["cr"] for r in rs), c) < 60:
            low.append(c)
    A["lowcache"] = {"sessions": len(low), "ctx": sum(low), "share": pct(sum(low), ctx_total)}
    # 7) サブエージェント
    nag = sum(1 for t in tools.values() if t["name"] in AGENT_TOOLS)
    per_s = Counter((t["proj"], t["sid"]) for t in tools.values() if t["name"] in AGENT_TOOLS)
    A["agents"] = {"invocations": nag, "sessions_with": len(per_s), "max_in_session": max(per_s.values()) if per_s else 0,
                   "sessions_ge10": sum(1 for v in per_s.values() if v >= 10)}
    # 8) 上位モデル比率（候補のみ）
    top = [m for m in A["models"] if family(m["model"]) == "opus"]
    A["top_tier_cost_share"] = pct(sum(m["cost"] or 0 for m in top), A["cost_total"]) if A["cost_total"] else None
    # 上限到達（代理）
    A["limits"] = limit_proxy(R, L["limits"])
    return A


def limit_proxy(R, limits):
    pts = sorted((r["ts"], r["inp"] + r["out"] + r["cw"]) for r in R if r["ts"])
    xs = [p[0] for p in pts]
    cum = [0]
    for _, w in pts:
        cum.append(cum[-1] + w)
    W = 5 * 3600
    mx, j = 0, 0
    for i in range(len(pts)):
        while xs[i] - xs[j] > W:
            j += 1
        mx = max(mx, cum[i + 1] - cum[j])
    before = []
    for t in limits:
        lo = bisect.bisect_left(xs, t - W); hi = bisect.bisect_right(xs, t)
        before.append(cum[hi] - cum[lo])
    days = {datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d") for t in limits}
    return {"events": len(limits), "days": len(days), "max_5h": mx, "before_limit": sorted(before)}


# ---------------------------------------------------------------- レポート構造
def fmt(n):
    return "{:,}".format(int(n))


def build(A):
    """ブロック列 [(kind, ...)] を返す。kind: h1,h2,p,table,ul,note"""
    B = []
    rid = "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(6))
    B.append(("h1", "Claude Code 利用ログ診断レポート"))
    B.append(("note", "レポートID: %s ／ 生成: tokdiet %s ／ 集計値のみ（プロンプト本文・パス・リポジトリ名を含みません）" % (rid, VERSION)))
    B.append(("h2", "0. 概況"))
    B.append(("p", "期間 %s 〜 %s ／ プロジェクト %d（匿名ID）／ セッション %s ／ リクエスト %s（同一requestIdの重複行を畳み済み）"
              % (A["period"][0], A["period"][1], A["projects"], fmt(A["sessions"]), fmt(A["requests"]))))
    if not A["requests"]:
        B.append(("p", "対象のリクエストが見つかりませんでした。"))
        return B, rid
    B.append(("p", "キャッシュ読取率 %.1f%%（文脈トークンのうちキャッシュ読取の割合）／ サブエージェント経由 リクエスト %.1f%%・トークン %.1f%%  〔根拠: 実測〕"
              % (A["cache_rate"], A["side_req_share"], A["side_tok_share"])))
    B.append(("table", ["モデル", "リクエスト", "入力", "出力", "キャッシュ読取", "キャッシュ作成", "キャッシュ率%", "API単価推計($)"],
              [[m["model"], fmt(m["req"]), fmt(m["inp"]), fmt(m["out"]), fmt(m["cr"]), fmt(m["cw"]), m["cache"],
                ("%.2f" % m["cost"]) if m["cost"] is not None else "単価不明"] for m in A["models"]]))
    B.append(("note", "金額はAPI単価での推計・契約により異なります（同梱の概算単価を使用・ネットワーク取得なし・公式価格での再確認が必要）。"
              "定額プラン（Pro・Max・Team席）では円の削減ではなく「枠の拡大」（同じ枠でより多く使える）と読み替えてください。〔根拠: 推定〕"
              + ("（単価不明モデル %d リクエストは金額に含まず）" % A["unpriced_req"] if A["unpriced_req"] else "")))
    B.append(("table", ["匿名ID", "セッション", "リクエスト", "トークン(文脈+出力)", "キャッシュ率%"],
              [[p["id"], fmt(p["sessions"]), fmt(p["req"]), fmt(p["tok"]), p["cache"]] for p in A["by_project"][:15]]))
    B.append(("table", ["ツール", "呼出数", "結果サイズ平均(文字)"],
              [[t["tool"], fmt(t["calls"]), fmt(t["avg_chars"])] for t in A["tools"]]))

    r, rr, bg, cp = A["resident"], A["reread"], A["bigres"], A["compaction"]
    B.append(("h2", "1. クイックウィン（品質影響が出にくいと考えられる変更）"))
    B.append(("note", "「品質リスクなし」とは断定しません。いずれも出力品質への影響は出にくいと考えられるだけで、変更後に確認してください。"
              "ここに示す数値は「その要因が占める量（上限の目安）」であり、削減額・削減率ではありません（削減幅は 根拠なし）。"))
    rows = []
    rows.append(["常駐オーバーヘッド（指示ファイル・スキル説明・MCP定義）",
                 "各セッション最初のリクエストの文脈 中央値 %s トークン。この常駐部分が全体の文脈量に占める割合 %.1f%%（上限の目安。Claude Code本体のシステムプロンプト分を分離できないため過大側）。指示ファイル 中央値 %s 文字（最大 %s）／スキル説明 中央値 %s 文字・%s件／MCP定義 中央値 %s 文字"
                 % (fmt(r["first_ctx_median"]), r["share_of_main_ctx"], fmt_none(r["instr_chars_median"]), fmt_none(r["instr_chars_max"]),
                    fmt_none(r["skill_chars_median"]), fmt_none(r["skill_count_median"]), fmt_none(r["mcp_block_chars_median"])),
                 "実測（文字数・初回文脈）／推定（占有率）", "CLAUDE.mdの整理、使わないスキルの無効化"])
    rows.append(["未使用のMCPサーバー・スキル",
                 "MCP: 通知された %d 件中、ログ上で呼出が見当たらないもの %d 件（名称の突合は近似）／スキル: 一覧 %s 件に対し、呼ばれた種類 %d"
                 % (A["mcp"]["announced"], A["mcp"]["unused"], fmt_none(A["skills"]["listed_median"]), A["skills"]["used_distinct"]),
                 "推定（名称突合）", "使っていないMCPの切断・スキルの無効化"])
    rows.append(["同一ファイルの再読込",
                 "Read %s 回中、同一セッションで編集を挟まず同じファイルを再度読んだ回数 %s（%.1f%%）、その結果文字数 %s。回数は数えた値で、無駄かどうかは文脈次第"
                 % (fmt(rr["reads"]), fmt(rr["rereads"]), rr["reread_rate"], fmt(rr["reread_chars"])),
                 "実測（回数）／無駄かは推定", "再読込を避ける運用、必要範囲のみ読む"])
    rows.append(["範囲指定なしの大きなファイル読み",
                 "offset/limitなしで約%s文字以上を返したRead %s 回（計 %s 文字）" % (fmt(BIG_RESULT_CHARS // 2), fmt(rr["big_unranged"]), fmt(rr["big_unranged_chars"])),
                 "実測", "大きなファイルは範囲読み・除外設定"])
    rows.append(["巨大なツール結果",
                 "%s 文字以上の結果 %s 件（全結果の %.1f%%の文字量）。内訳（匿名ツール別）: %s" % (fmt(BIG_RESULT_CHARS), fmt(bg["big"]), bg["share"],
                                                  ", ".join("%s×%d" % x for x in bg["by_tool"]) or "なし"),
                 "実測", "出力の絞り込み・除外設定"])
    B.append(("table", ["原因", "実測できたこと", "根拠ラベル", "変更案"], rows))

    B.append(("h2", "2. 要検証（品質確認が要る変更）"))
    B.append(("note", "以下は候補の列挙のみです。**御社のログで品質同等を測らないと効果は言えません**（効果幅は 根拠なし。他者報告は品質未計測のため採用していません）。"))
    rows = []
    rows.append(["モデル段の引き下げ", "上位モデル（Opus系）の推計金額シェア %s。引き下げ対象かどうかはタスク別の品質比較が必要"
                 % ("%.1f%%" % A["top_tier_cost_share"] if A["top_tier_cost_share"] is not None else "判定不能"), "御社ログで測らないと言えない"])
    rows.append(["決定論化（繰り返しの手作業をスクリプトへ）", "30文字以上の同一Bashコマンドが3回超繰り返された回数 %s（候補の目安）" % fmt(rr["repeat_bash"]), "御社ログで測らないと言えない"])
    rows.append(["サブエージェントの多重起動の見直し（フロー再設計）",
                 "起動 %s 回／起動したセッション %d／1セッション最大 %d 回／10回以上のセッション %d。サブエージェント経由はトークンの %.1f%%"
                 % (fmt(A["agents"]["invocations"]), A["agents"]["sessions_with"], A["agents"]["max_in_session"], A["agents"]["sessions_ge10"], A["side_tok_share"]),
                 "御社ログで測らないと言えない"])
    rows.append(["compaction頻度（文脈設計の見直し）",
                 "文脈の急落（直前の半分未満・6万トークン以上から）を compaction と推定: %d 回（100セッションあたり %.1f）。ログ上の明示マーカー: %d 件。0件は「検出されなかった」であり、無かったことの証明ではありません"
                 % (cp["est_drops"], cp["per_100_sessions"], cp["marker"]), "御社ログで測らないと言えない（検出は推定）"])
    rows.append(["キャッシュ率の低いセッション", "キャッシュ読取率60%%未満で文脈量50万トークン以上のセッション %d（全体の文脈量の %.1f%%）。原因（長時間の中断・指示の変更等）はログから断定できません"
                 % (A["lowcache"]["sessions"], A["lowcache"]["share"]), "推定（原因は測れない）"])
    B.append(("table", ["候補", "ログから見えたこと", "効果・品質の根拠"], rows))

    B.append(("h2", "3. 測れなかった項目"))
    lim = A["limits"]
    B.append(("ul", [
        "品質（出力の良し悪し）: ログからは測れません。",
        "契約プランの実際の枠の大きさ・Time at limit（管理画面の指標）: ローカルログからは直接取れません。代理指標のみ（下記）。",
        "上限到達（代理指標）: ログ内の rate limit 到達記録 %d 件（%d 日）。到達直前5時間の消費トークン（入力+出力+キャッシュ作成。この端末のログ合算）: %s。期間内の任意の5時間窓での最大消費 %s。〔根拠: 実測（記録件数）／代理（消費量）〕"
        % (lim["events"], lim["days"], ", ".join(fmt(x) for x in lim["before_limit"][:10]) or "該当なし", fmt(lim["max_5h"])),
        "MCPツール定義の個別サイズ・指示ファイルのどの記述が不要か: 測れません（文字数の総量のみ）。",
        "他の端末・Web版の利用: ローカルログに無いものは含まれません。",
        "金額: 同梱の概算単価による推計であり、契約・割引・為替により異なります。",
    ]))

    B.append(("h2", "4. 管理者向け1枚（転送用）"))
    B.append(("p", "この利用者のClaude Code利用ログ（ローカルで集計。本文・パスは含みません）の所見です。"))
    B.append(("ul", [
        "所見: %s〜%s の %s セッション。キャッシュ読取率 %.1f%%。上限到達の記録 %d 件（%d 日）。"
        % (A["period"][0], A["period"][1], fmt(A["sessions"]), A["cache_rate"], lim["events"], lim["days"]),
        "使い方の偏り: 常駐オーバーヘッドの占有率 %.1f%%（上限の目安）／サブエージェント経由のトークン %.1f%%／巨大ツール結果の文字量 %.1f%%。" % (r["share_of_main_ctx"], A["side_tok_share"], bg["share"]),
        "格上げした場合のコスト: 席課金の上位プランへの格上げは1席あたりの月額差が大きい場合があります。最新の価格は公式の料金ページで確認してください。",
        "管理画面で可能な対処（公式機能・管理者が自力で可能）: 支出上限の設定、既定モデルの指定、利用状況の分析。",
        "管理画面では直せない領域＝使い方の設計: 共有Skills・CLAUDE.mdの整理、決定論化、文脈量の管理。品質を保ったまま変えられるかは、御社のログで品質同等を測らないと言えません。",
        "枠の見立て: 定額プランでは円の削減ではなく枠の拡大（同じ枠でより多く使える状態）として読んでください。",
    ]))
    B.append(("note", "根拠ラベル: 実測＝ログから直接数えた値／推定＝閾値・突合・単価に仮定を含む値／根拠なし＝裏付けがなく数値を示さない。参考（公式）: https://code.claude.com/docs/en/costs"))
    return B, rid


def fmt_none(x):
    return "測れない" if x is None else fmt(x)


def to_md(B):
    out = []
    for b in B:
        k = b[0]
        if k == "h1": out.append("# " + b[1])
        elif k == "h2": out.append("\n## " + b[1])
        elif k in ("p",): out.append("\n" + b[1])
        elif k == "note": out.append("\n> " + b[1])
        elif k == "ul": out.append("\n" + "\n".join("- " + x for x in b[1]))
        elif k == "table":
            esc = lambda c: str(c).replace("|", "\\|").replace("\n", " ")
            out.append("\n| " + " | ".join(b[1]) + " |\n|" + "---|" * len(b[1]))
            out += ["| " + " | ".join(esc(c) for c in row) + " |" for row in b[2]]
    return "\n".join(out) + "\n"


CSS = """body{font:15px/1.7 -apple-system,'Hiragino Sans','Noto Sans JP',sans-serif;max-width:960px;margin:2rem auto;padding:0 16px;color:#1b1f23;background:#fff}
h1{font-size:1.6rem}h2{font-size:1.2rem;border-bottom:2px solid #d0d7de;padding-bottom:.2rem;margin-top:2rem}
table{border-collapse:collapse;width:100%;margin:.8rem 0;font-size:.85rem}th,td{border:1px solid #d0d7de;padding:.35rem .5rem;vertical-align:top;text-align:left}
th{background:#f3f4f6}.note{background:#fff8e1;border-left:4px solid #e0a800;padding:.4rem .8rem;margin:.8rem 0;font-size:.88rem}
@media(max-width:600px){table{display:block;overflow-x:auto}}
@media(prefers-color-scheme:dark){body{background:#0d1117;color:#e6edf3}th{background:#161b22}th,td,h2{border-color:#30363d}.note{background:#2a2410}}"""


def to_html(B):
    inline = lambda s: re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html.escape(s))
    o = ['<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
         '<title>Claude Code 利用ログ診断レポート</title><style>%s</style></head><body>' % CSS]
    for b in B:
        k = b[0]
        if k == "h1": o.append("<h1>%s</h1>" % html.escape(b[1]))
        elif k == "h2": o.append("<h2>%s</h2>" % html.escape(b[1]))
        elif k == "p": o.append("<p>%s</p>" % inline(b[1]))
        elif k == "note": o.append('<div class="note">%s</div>' % inline(b[1]))
        elif k == "ul": o.append("<ul>%s</ul>" % "".join("<li>%s</li>" % inline(x) for x in b[1]))
        elif k == "table":
            o.append("<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (
                "".join("<th>%s</th>" % html.escape(c) for c in b[1]),
                "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % inline(str(c)) for c in row) for row in b[2])))
    o.append("</body></html>")
    return "\n".join(o)


def run(root, out, brief=False, snapshot=None, compare=None):
    L = load(root)
    default_root = os.path.expanduser("~/.claude/projects")
    L["all_calls"] = None
    if os.path.isdir(default_root) and os.path.realpath(root) != os.path.realpath(default_root):
        L["all_calls"] = scan_skill_calls(default_root)       # --root で絞ったとき、全ログでの呼出数も併記するため
    A = analyze(L)
    B, rid = build(A)
    os.makedirs(out, exist_ok=True)
    mp, hp = os.path.join(out, "report.md"), os.path.join(out, "report.html")
    open(mp, "w", encoding="utf-8").write(to_md(B))
    open(hp, "w", encoding="utf-8").write(to_html(B))
    if brief or snapshot or compare:
        import actionbrief   # ローカル詳細版（パス等を含む。共有しない）
        actionbrief.write(L, A, out, snapshot=snapshot, compare=compare)
    return mp, hp, A


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--out", required=True, help="出力先（ログやリポジトリの外を推奨）")
    ap.add_argument("--no-brief", action="store_true", help="ローカル詳細版 action-brief.md を作らない（匿名レポートのみ）")
    ap.add_argument("--snapshot", help="集計のベースラインJSONを保存（0600・パスを含むローカル専用）")
    ap.add_argument("--compare", help="ベースラインJSONとの差分を action-brief.md 末尾に出す")
    a = ap.parse_args()
    if not os.path.isdir(a.root):
        sys.exit("ログディレクトリが見つかりません: 指定を確認してください")
    mp, hp, A = run(a.root, a.out, brief=not a.no_brief, snapshot=a.snapshot, compare=a.compare)
    print("生成: report.md / report.html（%d リクエスト・%d セッション）" % (A["requests"], A["sessions"]))
    if not a.no_brief:
        print("生成: action-brief.md（LOCAL ONLY・共有しない・0600）")


if __name__ == "__main__":
    main()
