"""actionbrief -- ローカル詳細版 action-brief.md（LOCAL ONLY・共有しない）を作る。

tokdiet.py の load()/analyze() の結果を使い、実在のスキル名・エージェント名・ファイルパスと実測の重さを載せる。
ネットワークなし。出力は 0600。シークレット様文字列はマスクする（完全ではない。README参照）。
匿名の管理者転送用レポート(report.md/html)とは別系統で、leakcheck の対象ではない。
"""
import json, os, re, shlex, statistics
from collections import Counter, defaultdict

import tokdiet as T

CAP_PER_CATEGORY = 4
TOP_N = 10
SMALL_OUT_PER_RUN = 3000     # 上位モデルなのに1回あたり出力がこれ未満 → 要検証候補（閾値は仮置き・推定）
BIG_READ_CHARS = T.BIG_RESULT_CHARS // 2

# ---------------------------------------------------------------- シークレットのマスク
_SECRET_PATTERNS = [
    (re.compile(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+"), "[EMAIL]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), "[JWT]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"), "[PRIVATE-KEY]"),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{12,}"), "[API-KEY]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{12,}"), "[TOKEN]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), "[TOKEN]"),
    (re.compile(r"\bAKIA[0-9A-Z]{12,}\b"), "[AWS-KEY]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"), "[API-KEY]"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{12,}"), r"\1 [TOKEN]"),
    (re.compile(r"(?i)((?:api[_\-]?key|apikey|access[_\-]?key|secret|token|passw(?:or)?d|passwd|authorization|auth)\w*\s*[=:]\s*)(['\"]?)[^\s'\"&]{4,}"), r"\1\2[MASKED]"),
    (re.compile(r"(?i)(--(?:token|password|passwd|secret|api-key|key)[= ])\S+"), r"\1[MASKED]"),
    (re.compile(r"://[^/\s:@]+:[^/\s@]+@"), "://[USER]:[MASKED]@"),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "[HEX]"),
    (re.compile(r"(?<![\w/.\-])(?=[A-Za-z0-9+_\-]*\d)(?=[A-Za-z0-9+_\-]*[A-Za-z])[A-Za-z0-9+_\-]{40,}(?![\w/.\-])"), "[LONG-TOKEN]"),
]


def mask(s):
    s = str(s)
    for p, r in _SECRET_PATTERNS:
        s = p.sub(r, s)
    return s


# ---------------------------------------------------------------- 補助
HOME = os.path.expanduser("~")


def disp(path):
    p = str(path)
    if HOME and p.startswith(HOME):
        p = "~" + p[len(HOME):]
    return mask(p)


def tk(chars):          # 文字数→トークン換算（概算: 4文字=1トークン。推定）
    return int(chars / 4)


def q(s):
    return shlex.quote(mask(s))


def code(s):
    return "`" + str(s).replace("`", "'") + "`"




# ---------------------------------------------------------------- 補助（スキル・テキスト解析・再確認コマンド）
def sections(content):
    """見出しごとの文字数 [(見出し, 文字数)]。先頭の見出し前は '(冒頭)'。"""
    res, cur, buf = [], "(冒頭)", 0
    for line in content.splitlines(keepends=True):
        m = re.match(r"#{1,4}\s+(.*)", line)
        if m:
            res.append((cur, buf))
            cur, buf = m.group(1).strip()[:60], 0
        buf += len(line)
    res.append((cur, buf))
    return [(h, n) for h, n in res if n > 0]


HIST_RE = re.compile(r"履歴|経緯|沿革|変更点|changelog|change\s*log|history|背景|background|過去|"
                     r"(?:^|[\s（(])(?:使用例|具体例|出力例|入力例|例|サンプル|examples?)(?:[）)\s:：]|$)", re.I)


NORM_RE = re.compile(r"必須|禁止|手順|ルール|厳守|遵守|\bmust\b|\brequired?\b|\bmandatory\b|\bshall\b|\bnever\b", re.I)
OPENING_CHARS = 200


def section_openings(content):
    """見出し→節冒頭（見出し直後の最初の数行・OPENING_CHARS文字まで）。sections() と同じ見出しの切り方。"""
    res, cur, buf = [], "(冒頭)", []
    for line in content.splitlines():
        m = re.match(r"#{1,4}\s+(.*)", line)
        if m:
            res.append((cur, "\n".join(buf)[:OPENING_CHARS]))
            cur, buf = m.group(1).strip()[:60], []
        elif len(buf) < 4:
            buf.append(line)
    res.append((cur, "\n".join(buf)[:OPENING_CHARS]))
    return res


def analyze_text(content):
    """指示/定義ファイルの節構成。履歴・経緯・例系の見出しと、重複段落を実測で判定する。
    見出し語だけでは判定せず、見出しまたは節冒頭に規範語（必須/禁止/手順/ルール/must/required 等）を含む節は履歴系から除外する。"""
    secs = sections(content)
    total = max(1, len(content))
    opens = {}
    for h, o in section_openings(content):
        opens.setdefault(h, o)
    hist, hist_excl = [], []
    for h, n in secs:
        if HIST_RE.search(h):
            (hist_excl if NORM_RE.search(h + "\n" + opens.get(h, "")) else hist).append((h, n))
    seen, dup = set(), 0
    for para in re.split(r"\n\s*\n", content):
        p = re.sub(r"\s+", " ", para).strip()
        if len(p) >= 80:
            if p in seen:
                dup += len(p)
            seen.add(p)
    heavy = sorted(secs, key=lambda x: -x[1])
    return {"chars": len(content), "sections": heavy, "hist": hist, "hist_excl": hist_excl, "dup_chars": dup,
            "heaviest": heavy[0] if heavy else None, "heaviest_pct": round(100.0 * heavy[0][1] / total, 1) if heavy else 0}


def plan_text_file(a, label):
    """実測条件が成立した提案だけを返す: [(条件, 提案)]。履歴系の節/重複段落が無ければ空（重い節は事実として別表示）。"""
    pl = []
    if a["hist"]:
        pl.append(("見出しに履歴/経緯/背景/例系の節がある: " + "、".join("「%s」%s文字" % (mask(h), T.fmt(n)) for h, n in a["hist"][:4]),
                   "内容を確認して判断（見出し語だけの判定のため、現行の手順に使われているかは未確認）"))
    if a["dup_chars"]:
        pl.append(("80文字以上の同一段落が重複している（重複分 %s 文字）" % T.fmt(a["dup_chars"]), "重複している段落のどちらかを削除する"))
    return pl


def heavy_section(a):
    """事実表示の条件: 最も重い節が3,000文字以上かつ全体の30%以上。"""
    h = a["heaviest"]
    return bool(h and h[1] >= 3000 and a["heaviest_pct"] >= 30)


def front_model(path):
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().split("\n")
    except OSError:
        return None
    if not lines or lines[0].strip() != "---":
        return None
    for ln in lines[1:60]:
        if ln.strip() == "---":
            break
        m = re.match(r"model:\s*['\"]?([\w.\-]+)", ln)
        if m:
            return m.group(1)
    return None


def skill_front_desc_len(path):
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().split("\n")
    except OSError:
        return None
    if not lines or lines[0].strip() != "---":
        return None
    val, cap = "", False
    for ln in lines[1:60]:
        if ln.strip() == "---":
            break
        if cap:
            if ln.startswith((" ", "\t")):
                val += " " + ln.strip(); continue
            cap = False
        m = re.match(r"description:\s*(.*)", ln)
        if m:
            cap = True; val = m.group(1).strip().strip("'\"")
            if val in (">", "|", ">-", "|-"):
                val = ""
    return len(val.strip()) if val else None


def git_root(d):
    cur = d
    for _ in range(12):
        if os.path.isdir(os.path.join(cur, ".git")) or os.path.isfile(os.path.join(cur, ".git")):
            return cur
        nd = os.path.dirname(cur)
        if nd == cur:
            break
        cur = nd
    return None


def skill_origins(names, cwds):
    """name -> {origin, file, file_desc}. ログ内の名前(plugin:skill)とファイルの存在確認で判定できる範囲だけ。"""
    idx = defaultdict(list)

    def scan(base, kind, depth):
        if not os.path.isdir(base):
            return
        base_depth = base.rstrip(os.sep).count(os.sep)
        for dp, dns, fns in os.walk(base):
            if dp.count(os.sep) - base_depth > depth:
                dns[:] = []
                continue
            if "SKILL.md" in fns:
                idx[os.path.basename(dp)].append((kind, os.path.join(dp, "SKILL.md")))
    for c in cwds:
        d = c
        for _ in range(8):
            scan(os.path.join(d, ".claude", "skills"), "project", 2)
            nd = os.path.dirname(d)
            if nd == d or d == HOME:
                break
            d = nd
    for c in cwds:                                    # プロジェクトの自前スラッシュコマンド（.claude/commands/*.md）も project として認識
        d = c
        for _ in range(8):
            cdir = os.path.join(d, ".claude", "commands")
            if os.path.isdir(cdir):
                for dp, dns, fns in os.walk(cdir):
                    for fn in fns:
                        if fn.endswith(".md"):
                            idx[fn[:-3]].append(("project", os.path.join(dp, fn)))
            nd = os.path.dirname(d)
            if nd == d or d == HOME:
                break
            d = nd
    scan(os.path.join(HOME, ".claude", "skills"), "user", 4)
    scan(os.path.join(HOME, ".claude", "plugins"), "plugin", 8)
    order = {"project": 0, "user": 1, "plugin": 2}
    res = {}
    for n in names:
        base = n.split(":")[-1]
        hits = sorted(idx.get(base, []) + (idx.get(n, []) if n != base else []), key=lambda x: order[x[0]])
        if hits:
            kind, path = hits[0]
            if ":" in n and kind in ("user", "project"):
                kind = "配布"                       # <提供元>:<名前> は配布物（ユーザーが編集しても更新で上書きされる）
            if os.sep + "synced" + os.sep in path:
                kind = "配布"                       # 同期（配布）ディレクトリ配下
            res[n] = {"origin": kind, "file": path, "file_desc": skill_front_desc_len(path)}
        elif ":" in n:
            res[n] = {"origin": "plugin", "file": None, "file_desc": None}      # 名前のプレフィックスから判定
        else:
            res[n] = {"origin": "不明", "file": None, "file_desc": None}
    return res


EDIT = {"project": "編集可（プロジェクト）", "user": "編集可（ユーザー）", "plugin": "配布（プラグイン）・編集不可（更新で上書き）",
        "配布": "配布・編集不可（更新で上書き）", "不明": "不明（組込み等で編集不可の可能性）"}
EDITABLE = ("project", "user")
SCAN_EXT = {".md", ".yml", ".yaml", ".json", ".sh", ".py", ".js", ".mjs", ".ts", ".toml", ".txt", ".cfg"}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".next", "dist-deploy", "themes", "vendor",
             "third_party", "third-party", "site-packages", "bower_components", "wp-includes", "wp-admin", "Pods", "target", ".tox"}
TABLE_TOP = 20         # スキル表の表示件数（残りは baseline.json）
MIN_SESSIONS = 5       # 最低観測量: 利用量を数えられたセッションがこれ未満なら優先アクションを出さない（仮置き）
DOCS = {"skills": "https://code.claude.com/docs/en/skills", "plugins": "https://code.claude.com/docs/en/plugins/install", "mcp": "https://code.claude.com/docs/en/mcp"}


def is_generic(name):
    """ハイフン/コロンを含まない名前は一般語の可能性が高い（run, loop, simplify 等）。"""
    return "-" not in name and ":" not in name


def make_matcher(names):
    """固有名(ハイフン/コロンを含む)は単語一致、一般語は厳密形式(/名前・`名前`・skills/名前・name: 名前)のみ。text -> 見つかった名前の集合。"""
    esc = lambda xs: "|".join(re.escape(n) for n in sorted(xs, key=len, reverse=True))
    distinct = [n for n in names if not is_generic(n)]
    generic = [n for n in names if is_generic(n)]
    pat_d = re.compile(r"(?<![\w\-:])(" + esc(distinct) + r")(?![\w\-])") if distinct else None
    pat_g = None
    if generic:
        g = esc(generic)
        pat_g = re.compile(r"(?<![\w/.\-])/(?P<a>" + g + r")(?![\w\-/])|`(?P<b>" + g + r")`|skills/(?P<c>" + g + r")(?![\w\-])|(?m:^name:\s*(?P<d>" + g + r")\s*$)")

    def find(text):
        found = set(pat_d.findall(text)) if pat_d else set()
        if pat_g:
            for m in pat_g.finditer(text):
                found.add(m.group("a") or m.group("b") or m.group("c") or m.group("d"))
        return found
    return find


def project_roots(cwds):
    return sorted({r for r in (git_root(c) for c in cwds) if r and r != HOME and r != "/"})


def skill_refs(names, cwds, own):
    """names がプロジェクト内の他ファイルから参照されているか（ファイル数のみ）。サードパーティ配下(SKIP_DIRS)は除外。"""
    roots = project_roots(cwds)
    res = {n: {"files": 0, "ci": 0, "examples": [], "generic": is_generic(n), "rel": []} for n in names}
    if not names or not roots:
        return res, roots
    find = make_matcher(names)
    nfiles = 0
    for root in roots:
        for dp, dns, fns in os.walk(root):
            dns[:] = [x for x in dns if x not in SKIP_DIRS]
            for fn in fns:
                if os.path.splitext(fn)[1] not in SCAN_EXT or nfiles > 30000:
                    continue
                p = os.path.join(dp, fn)
                try:
                    if os.path.getsize(p) > 512 * 1024:
                        continue
                    text = open(p, encoding="utf-8", errors="replace").read()
                except OSError:
                    continue
                nfiles += 1
                for n in find(text):
                    base = n.split(":")[-1]
                    if os.sep + "skills" + os.sep + base + os.sep in p or p == own.get(n):
                        continue                      # 自スキルのディレクトリ内は除外
                    r = res[n]; r["files"] += 1
                    if os.sep + ".github" + os.sep in p or re.search(r"check|lint|test|verify|validate|sync", fn, re.I):
                        r["ci"] += 1
                    if len(r["examples"]) < 3:
                        r["examples"].append(p)
    return res, roots


def workflow_mentions(names, cwds):
    """プロジェクトの .claude/commands・agents・skills の本文に他の名前が言及されているか（呼出関係の手がかり）。
    {名前: [言及しているファイル]}。プラグイン経由の呼出関係はここからは推定できない。"""
    out = defaultdict(list)
    if not names:
        return out
    find = make_matcher(list(names))
    for root in project_roots(cwds) or [c for c in cwds[:1]]:
        for sub in ("commands", "agents", "skills"):
            base = os.path.join(root, ".claude", sub)
            for dp, _, fns in os.walk(base):
                for fn in fns:
                    if not fn.endswith(".md"):
                        continue
                    p = os.path.join(dp, fn)
                    try:
                        text = open(p, encoding="utf-8", errors="replace").read()
                    except OSError:
                        continue
                    me = {fn[:-3], os.path.basename(dp)}
                    for n in find(text):
                        if n.split(":")[-1] in me:
                            continue
                        if len(out[n]) < 5 and p not in out[n]:
                            out[n].append(p)
    return out


def dup_groups(names):
    """正規化した名前(英数のみ)が同じ別名の組（例 wp-media と wp:media = 重複掲載の可能性）。"""
    g = defaultdict(list)
    for n in names:
        g[re.sub(r"[^a-z0-9]", "", n.lower())].append(n)
    return [sorted(v) for v in g.values() if len(v) > 1]


def disable_howto(origin):
    """公式ドキュメント(code.claude.com/docs)で確認できた範囲だけ。確認できないものは『公式未確認・手順不明』。"""
    if origin in ("project", "user"):
        return ("設定の `skillOverrides` に {\"<名前>\": \"off\"} を書く（`/skills` メニューでも可。書込先は .claude/settings.local.json）。戻し方: そのキーを削除、または \"on\"。"
                "または SKILL.md の frontmatter に `disable-model-invocation: true`。出典: " + DOCS["skills"])
    if origin == "plugin":
        return ("`claude plugin disable <plugin>@<marketplace>`（`/plugin` の Installed タブでも可。設定キーは `enabledPlugins`＝user: ~/.claude/settings.json／project: .claude/settings.json／local: .claude/settings.local.json）。"
                "戻し方: `claude plugin enable`。`skillOverrides` はプラグインのスキルには効かない。プラグイン内の個別スキルだけを隠す手順は公式未確認・手順不明。出典: " + DOCS["skills"] + " , " + DOCS["plugins"])
    if origin == "配布":
        return ("`<name>@synced`（claude.ai同期プラグイン）なら `/plugin` の Installed タブで無効化（公式: " + DOCS["plugins"] + "）。"
                "`~/.claude/skills/synced` 配下のスキルや `anthropic-skills:` 名前空間の個別無効化手順は公式未確認・手順不明")
    return ("出所不明のため断定しない。組込みスキルなら `skillOverrides` で \"off\"、または `disableBundledSkills: true`（公式: " + DOCS["skills"] + "）。"
            "どちらの出所か判別できない場合は公式未確認・手順不明")


MCP_HOWTO = ("`/mcp` パネルでサーバーをオフ（接続しないだけで設定は残る。`~/.claude.json` のプロジェクト別 `disabledMcpServers` に記録）。戻し方: `/mcp` で再度オン。"
             "完全に外すなら `claude mcp remove <name>`（リモートはOAuthトークンも削除される）。プラグイン由来のサーバーはプラグインの無効化/アンインストール。出典: " + DOCS["mcp"])


def v_tooluse(name, first_key=None, val=None, prefix=False):
    """tool_use を一意なidで数える再確認コマンド（wireToolInputs の重複表現で2倍に数えない）。"""
    esc = lambda s: re.sub(r"([.\[\]()*+?{}|^$\\])", r"\\\1", s)
    pat = '"id":"toolu_[A-Za-z0-9]+","name":"%s"' % esc(name)
    if first_key:
        pat += ',"input":\\{"%s":"%s' % (first_key, esc(json.dumps(val, ensure_ascii=False)[1:-1])) + ("" if prefix else '"')
    return "grep -rhoE %s ~/.claude/projects --include='*.jsonl' | sort -u | wc -l   # tool_use idの一意数" % shlex.quote(pat)


SCRIPT_RE = re.compile(r"^(?:bash|sh|python3?|node)\s+\S+\.(?:sh|py|js|mjs)\b")


def find_agent_def(atype, cwds):
    if not atype:
        return None
    cands = [os.path.join(HOME, ".claude", "agents", atype + ".md")]
    for c in cwds:
        d = c
        for _ in range(8):
            cands.append(os.path.join(d, ".claude", "agents", atype + ".md"))
            nd = os.path.dirname(d)
            if nd == d:
                break
            d = nd
    for p in cands:
        if os.path.isfile(p):
            try:
                return p, os.path.getsize(p)
            except OSError:
                pass
    return None




MODEL_WORD_RE = re.compile(r"(?<![A-Za-z])(?:opus|sonnet|haiku)(?![A-Za-z])", re.I)


def strip_frontmatter(text):
    """先頭の --- ... --- を空行に置換（行番号を保つ）。"""
    lines = text.split("\n")
    if lines and lines[0].strip() == "---":
        for i in range(1, min(len(lines), 60)):
            if lines[i].strip() == "---":
                return "\n".join([""] * (i + 1) + lines[i + 1:])
    return text


def find_intent(agent_names, sources):
    """指示ファイル本文で、エージェント名とモデル名(opus/sonnet/haiku)が同一段落(空行区切り)または同一行にある箇所。
    sources: [(path, text, is_agent_def)]。戻り値 {エージェント名: (path, 行番号)}。エージェント定義はfrontmatterを除いた本文のみ見る。"""
    names = [n for n in agent_names if n and n != "(種別不明)"]
    res = {}
    if not names:
        return res
    pats = {n: re.compile(r"(?<![\w\-:])" + re.escape(n) + r"(?![\w\-])") for n in names}
    for path, text, is_def in sources:
        if is_def:
            text = strip_frontmatter(text)
        lines = text.split("\n")
        i = 0
        while i < len(lines):
            if not lines[i].strip():
                i += 1; continue
            j = i
            while j < len(lines) and lines[j].strip():
                j += 1
            para = lines[i:j]                          # 段落 = 空行で区切られた連続行（i..j-1）
            if any(MODEL_WORD_RE.search(l) for l in para):
                for n, pat in pats.items():
                    if n in res:
                        continue
                    for k, l in enumerate(para):
                        if pat.search(l):
                            res[n] = (path, i + k + 1)
                            break
            i = j
    return res


# ---------------------------------------------------------------- 集計
def collect(L, A):
    tools, results, reqs, sess, D = L["tools"], L["results"], L["reqs"], L["sess"], L["detail"]
    C = {}
    C["nsess"] = len({(r["proj"], r["sid"]) for r in reqs.values()})
    cwds = sorted(D["cwds"])
    # --- 分母（観測量）
    usage_sessions = len({(r["proj"], r["sid"]) for r in reqs.values()})
    sub_logs = sum(1 for p in L["usage_files"] if os.sep + "subagents" + os.sep in p)
    C["denom"] = {"files": L["files"], "usage_files": len(L["usage_files"]), "sub_logs": sub_logs, "sessions": usage_sessions,
                  "requests": A["requests"], "period": A["period"], "insufficient": usage_sessions < MIN_SESSIONS}
    # --- スキル: 件数の定義 = メインセッションの全 skill_listing の名前の和集合（サブエージェントのログは除く）
    desc, nsess_listing = {}, 0
    per_sess_counts = []
    for s in sess.values():
        if s["skills"]:
            nsess_listing += 1
            per_sess_counts.append(len(s["skills"]))
            for n, v in s["skills"].items():
                desc[n] = max(desc.get(n, 0), v)
    latest = max((s for s in sess.values() if s["skills"] and s.get("skills_ts")), key=lambda x: x["skills_ts"], default=None)
    cur_set = set(latest["skills"]) if latest else set(desc)
    logged = [s["skills_logged"] for s in sess.values() if s["skills_logged"]]
    mode_logged = Counter(logged).most_common(1)[0][0] if logged else None
    calls = Counter(t["skill_name"] for t in tools.values() if t.get("skill_name"))
    slash = D["slash"]
    all_calls = L.get("all_calls")
    zero = [n for n in desc if calls.get(n, 0) + slash.get(n, 0) == 0]
    origins = skill_origins(list(desc), cwds)
    refs, roots = skill_refs(zero, cwds, {n: o["file"] for n, o in origins.items() if o["file"]})
    rel = workflow_mentions(zero, cwds)
    for n in zero:
        refs[n]["rel"] = rel.get(n, [])
    C["skills"] = {"desc": desc, "calls": calls, "slash": slash, "origins": origins, "refs": refs, "ref_roots": len(roots),
                   "listing_chars": sum(s["skill_listing"] for s in sess.values()),   # セッション合算（トークン換算の重さ用）
                   "sessions": nsess_listing, "parsed": len(desc), "logged_mode": mode_logged,
                   "latest_logged": latest["skills_logged"] if latest else None, "latest_parsed": len(cur_set) if latest else None,
                   "cur": cur_set, "has_latest": latest is not None, "all_calls": all_calls,
                   "dups": [g for g in dup_groups(sorted(cur_set)) ],
                   "per_session_median": int(statistics.median(per_sess_counts)) if per_sess_counts else None}
    # --- 指示ファイル
    inst = []
    for p, e in D["instr"].items():
        a = analyze_text(e["content"])
        inst.append({"path": p, "chars": len(e["content"]), "sessions": len(e["sessions"]), "an": a, "sections": a["sections"],
                     "gone": not os.path.exists(p)})
    C["instr"] = sorted(inst, key=lambda x: -x["chars"] * x["sessions"])
    # --- ファイル（読み手×範囲指定の有無）。範囲指定なし = Read に offset も limit も無い全文読み（サイズ不問）。
    last, dirty = set(), set()
    ps = defaultdict(lambda: defaultdict(lambda: {"ranged": 0, "full": 0, "big": 0, "re": 0, "re_full": 0}))
    fs = defaultdict(lambda: {"reads": 0, "re": 0, "re_chars": 0, "full": 0, "big": 0, "big_chars": 0})
    for tid, t in tools.items():
        if t.get("editkey"):
            dirty.add(t["editkey"])
        elif t["name"] == "Read":
            k, ch, p = t["pathkey"], results.get(tid, 0), t["path"]
            who = t.get("agent") or "(メイン会話)"
            st, f = ps[p][who], fs[p]
            f["reads"] += 1
            full = not t["ranged"]
            st["full" if full else "ranged"] += 1
            if full:
                f["full"] += 1
                if ch >= BIG_READ_CHARS:
                    f["big"] += 1; f["big_chars"] += ch; st["big"] += 1
            if k in last and k not in dirty:
                f["re"] += 1; f["re_chars"] += ch; st["re"] += 1
                if full:
                    st["re_full"] += 1
            last.add(k); dirty.discard(k)
    edited = {t["epath"] for t in tools.values() if t.get("epath")}      # ログ内で Write/Edit された形跡のあるパス
    files = []
    for p, f in fs.items():
        if f["re"] or f["big"]:
            files.append(dict(f, path=p, readers=ps[p], weight=max(f["re_chars"], f["big_chars"]),
                              edited=p in edited, gone=not os.path.exists(p)))
    C["files"] = sorted(files, key=lambda x: -x["weight"])
    # --- Bash
    bc, bchars = Counter(), Counter()
    for tid, t in tools.items():
        if t["name"] == "Bash" and t.get("cmd"):
            bc[t["cmd"]] += 1; bchars[t["cmd"]] += results.get(tid, 0)
    C["bash"] = [(c, n, bchars[c]) for c, n in bc.most_common() if n >= 3]
    # --- MCP
    mc = defaultdict(lambda: {"n": 0, "chars": 0})
    used_srv = set()
    for tid, t in tools.items():
        if t["name"].startswith("mcp__"):
            mc[t["name"]]["n"] += 1; mc[t["name"]]["chars"] += results.get(tid, 0)
            if t["name"].count("__") >= 2:
                used_srv.add(T.norm(t["name"].split("__")[1]))
    C["mcp"] = sorted(mc.items(), key=lambda x: -x[1]["chars"])
    ann = set().union(*[s["mcp_announced"] for s in sess.values()]) if sess else set()
    unused = sorted(a for a in ann if a and not any(a in u or u in a for u in used_srv))
    corpus = " ".join(e["content"] for e in D["instr"].values())              # ログに載った指示ファイル
    for r_ in project_roots(cwds):                                             # 加えてプロジェクトの CLAUDE.md / .claude/rules
        for p_ in [os.path.join(r_, "CLAUDE.md")] + [os.path.join(dp, f_) for dp, _, fs_ in os.walk(os.path.join(r_, ".claude", "rules")) for f_ in fs_]:
            try:
                corpus += " " + open(p_, encoding="utf-8", errors="replace").read()
            except OSError:
                pass
    ncorp = re.sub(r"[^a-z0-9]+", "_", corpus.lower())
    C["mcp_depends"] = [a for a in unused if len(re.sub(r"^(claude_ai_|plugin_[a-z0-9]+_)", "", a)) >= 4 and re.sub(r"^(claude_ai_|plugin_[a-z0-9]+_)", "", a) in ncorp]
    C["mcp_unused"] = [a for a in unused if a not in C["mcp_depends"]]
    C["mcp_block_chars"] = sum(s["mcp_block_chars"] for s in sess.values())
    # --- エージェント種別
    ag = defaultdict(lambda: {"files": set(), "req": 0, "inp": 0, "out": 0, "cr": 0, "cw": 0, "models": Counter()})
    for r in reqs.values():
        if r["side"]:
            a = ag[r["agent"] or "(種別不明)"]
            if r["afile"]:
                a["files"].add(r["afile"])
            a["req"] += 1
            for k in ("inp", "out", "cr", "cw"):
                a[k] += r[k]
            a["models"][T.safe_model(r["model"])] += 1
    agents = []
    for name, a in ag.items():
        runs = max(1, len(a["files"]))
        billed = a["inp"] + a["out"] + a["cw"]
        d = find_agent_def(name, D["cwds"])
        an = None
        if d:
            try:
                an = analyze_text(open(d[0], encoding="utf-8", errors="replace").read())
            except OSError:
                an = None
        agents.append({"type": name, "runs": len(a["files"]), "req": a["req"], "billed": billed,
                       "ctx_out": a["inp"] + a["cr"] + a["cw"] + a["out"], "avg_billed": billed // runs,
                       "avg_out": a["out"] // runs, "models": a["models"].most_common(4), "def": d, "an": an})
    # 意図的モデル設定の記載（指示ファイル: ログに載った分＋プロジェクトの CLAUDE.md / .claude/rules / .claude/agents 本文）
    srcs, seen_p = [], set()

    def add_src(p_, is_def):
        if p_ in seen_p:
            return
        try:
            srcs.append((p_, open(p_, encoding="utf-8", errors="replace").read(), is_def)); seen_p.add(p_)
        except OSError:
            pass
    for p_, e in D["instr"].items():
        srcs.append((p_, e["content"], os.sep + "agents" + os.sep in p_)); seen_p.add(p_)
    for r_ in project_roots(cwds):
        add_src(os.path.join(r_, "CLAUDE.md"), False)
        for dp, _, fs_ in os.walk(os.path.join(r_, ".claude", "rules")):
            for f_ in sorted(fs_):
                add_src(os.path.join(dp, f_), False)
        for dp, _, fs_ in os.walk(os.path.join(r_, ".claude", "agents")):
            for f_ in sorted(fs_):
                if f_.endswith(".md"):
                    add_src(os.path.join(dp, f_), True)
    for a in agents:
        if a["def"]:
            add_src(a["def"][0], True)
    intent = find_intent([a["type"] for a in agents], srcs)
    for a in agents:
        a["intent"] = intent.get(a["type"])
    # 起動元（親セッションの最初のスラッシュコマンド、無ければ最初に呼ばれたSkill）
    first_skill = {}
    for t in tools.values():
        if t["name"] == "Skill" and not t["side"] and t.get("skill_name"):
            first_skill.setdefault((t["proj"], t["sid"]), t["skill_name"])
    launch, seen_files = defaultdict(Counter), set()
    for r in reqs.values():
        if r["side"] and r["afile"] and r["afile"] not in seen_files:
            seen_files.add(r["afile"])
            key = (r["proj"], r["sid"])
            lc = D["first_cmd"].get(key)
            lbl = "/" + lc if lc else ("skill:" + first_skill[key] if key in first_skill else "(起動元不明)")
            launch[r["agent"] or "(種別不明)"][lbl] += 1
    C["launch"] = launch
    # モデル差分: 定義 frontmatter の model と実使用モデル
    for a in agents:
        a["fm_model"] = front_model(a["def"][0]) if a["def"] else None
        a["models_all"] = ag[a["type"]]["models"]
    callers = workflow_mentions([a["type"] for a in agents if a["type"] != "(種別不明)"], cwds)
    for a in agents:
        a["callers"] = callers.get(a["type"], [])
    C["agents"] = {x["type"]: x for x in agents}
    C["agent_list"] = sorted(agents, key=lambda x: -x["billed"])
    return C


# ---------------------------------------------------------------- 項目生成
def agent_def_of(C, who):
    a = C["agents"].get(who)
    return a["def"] if a and a["def"] else None


BASH_MIN = 5            # 反復Bashの閾値（この回数以上を表示・提案の対象にする。仮置き）
MCP_AVG_MIN = 5000      # MCP結果の平均文字数の閾値（仮置き）


def reader_line(st):
    return "範囲あり%d／全文%d" % (st["ranged"], st["full"])


def build_items(C):
    """提案の有無に関わらず全項目を作る（テスト・検査用）。"""
    I = []
    s = C["skills"]
    if s["desc"]:
        pl = []
        o = s["origins"]
        cur = s["cur"]
        total_calls = lambda n: s["calls"].get(n, 0) + s["slash"].get(n, 0) + ((s["all_calls"] or {}).get(n, 0) if s["all_calls"] else 0)
        edit_long = [(n, c) for n, c in sorted(s["desc"].items(), key=lambda x: -x[1]) if c >= 500 and o[n]["origin"] in EDITABLE and n in cur][:5]
        if edit_long:
            pl.append(("編集可（project/user）のスキルで一覧上の説明が500文字以上: " + "、".join("%s(%s字)" % (mask(n), T.fmt(c)) for n, c in edit_long),
                       "SKILL.md の description の発動条件に関係しない記述を短くすることを検討（発動条件を弱めると発動しなくなる恐れがあるため、短縮後に発動を確認）"))
        zero = [n for n in s["desc"] if n in s["refs"] and n in cur and total_calls(n) == 0 and not o[n].get("gone")]
        safe = lambda n: s["refs"][n]["files"] == 0 and not s["refs"][n]["rel"] and not s["refs"][n]["generic"]
        cand_e = [n for n in zero if o[n]["origin"] in EDITABLE and safe(n)]
        cand_d = [n for n in zero if o[n]["origin"] in ("plugin", "配布") and safe(n)]
        warn = [n for n in zero if s["refs"][n]["files"] > 0 or s["refs"][n]["rel"]]
        undecided = [n for n in zero if s["refs"][n]["generic"] and s["refs"][n]["files"] == 0 and not s["refs"][n]["rel"]]
        if cand_e:
            pl.append(("現在の一覧にあり、ログ内で0回・編集可（project/user）で、他ファイルの参照も .claude 内のワークフロー言及も0件のスキルが %d 件: %s（説明 計%s 字）" % (
                len(cand_e), "、".join(mask(n) for n in cand_e[:12]) + (" ほか" if len(cand_e) > 12 else ""), T.fmt(sum(s["desc"][n] for n in cand_e))),
                "無効化の候補（参照0件は安全の証明ではない。プラグイン経由の呼出は推定できない）。手順: " + disable_howto("project")))
        for org in ("plugin", "配布"):
            cd = [n for n in cand_d if o[n]["origin"] == org]
            if cd:
                pl.append(("現在の一覧にあり、ログ内で0回・%s由来（編集不可）で、参照・ワークフロー言及が0件のスキルが %d 件: %s（説明 計%s 字）" % (
                    "プラグイン" if org == "plugin" else "配布", len(cd), "、".join(mask(n) for n in cd[:12]) + (" ほか" if len(cd) > 12 else ""), T.fmt(sum(s["desc"][n] for n in cd))),
                    "無効化の候補。手順: " + disable_howto(org)))
        if warn:
            pl.append(("ログ内で0回だが、他ファイルの参照または .claude/commands・agents・skills の本文での言及があるスキルが %d 件（うちCI/検査系参照 %d 件・ワークフロー言及 %d 件）" % (
                len(warn), sum(1 for n in warn if s["refs"][n]["ci"]), sum(1 for n in warn if s["refs"][n]["rel"])),
                "『退避注意』: ワークフローから呼ばれる可能性がある。参照元（本文の表・参照元の例）を確認してから判断（プラグイン経由の呼出は推定できない）"))
        stale = [n for n in s["desc"] if s["has_latest"] and n not in cur]
        I.append(dict(cat="skills", weight=tk(s["listing_chars"]),
            target="スキル説明の常駐一覧（解析できた %d 件・現在の一覧 %s 件）" % (len(s["desc"]), len(cur)),
            heavy="一覧は全セッション合算 %s 文字（約%s トークン換算・%d セッション）。ログ内0回は %d 件（うち一般語のため参照判定不能 %d 件、現在の一覧にない過去の名前 %d 件は提案から除外）" % (
                T.fmt(s["listing_chars"]), T.fmt(tk(s["listing_chars"])), s["sessions"],
                len([n for n in s["desc"] if s["calls"].get(n, 0) + s["slash"].get(n, 0) == 0]), len(undecided), len(stale)),
            verify=v_tooluse("Skill", "skill", "<スキル名>") + "   # <スキル名>を置き換える\n"
                   "grep -rho '\"skillCount\":[0-9]*' ~/.claude/projects --include='*.jsonl' | sort | uniq -c | sort -rn | head -3",
            risk="クイックウィン（品質影響が出にくいと考えられる・変更後に確認）", plan=pl,
            check="無効化/短縮後に `python3 tokdiet.py --out <dir> --compare <baseline.json>` を実行し、スキル一覧の文字数と各セッション最初の文脈が減ったか確認。必要なスキルが発動しなくなっていないか、いつもの作業を1〜2件流して確認。",
            label="実測（文字数・呼出回数・参照数）／推定（トークン換算・無効化の安全性）"))
    for f in C["instr"][:CAP_PER_CATEGORY]:
        pl = [] if f["gone"] else plan_text_file(f["an"], "このファイル")
        I.append(dict(cat="instr", weight=tk(f["chars"]) * f["sessions"], target=disp(f["path"]) + ("（現在は存在しない）" if f["gone"] else ""),
            heavy="%s 文字（約%s トークン換算）×%d セッションで常駐。節別: %s" % (T.fmt(f["chars"]), T.fmt(tk(f["chars"])), f["sessions"],
                "、".join("「%s」%s" % (mask(h), T.fmt(n)) for h, n in f["sections"][:3])),
            verify="wc -c %s; grep -n '^#' %s | head -40" % (q(f["path"]), q(f["path"])),
            risk="クイックウィン（品質影響が出にくいと考えられる・指示の削除は挙動が変わりうるので変更後に確認）", plan=pl,
            check="編集後に再実行して `--compare` で当該ファイルの文字数と最初の文脈が減ったか確認。主要な作業を1〜2件流し、従来の指示が守られているか確認。",
            label="実測（文字数・節別文字数・重複段落）／推定（トークン換算）"))
    for f in C["files"][:CAP_PER_CATEGORY * 2]:
        p, rd = f["path"], f["readers"]
        pl = []
        for who, st in ([] if f["gone"] else sorted(rd.items(), key=lambda x: -x[1]["full"])):
            d = agent_def_of(C, who)
            if who == "(メイン会話)" or st["full"] < 3 or st["full"] <= st["ranged"]:
                continue
            if d:
                pl.append(("%s が範囲指定なしの全文読みを %d 回（範囲指定あり %d 回）。このうち再読込 %d 回・20,000字以上の結果 %d 回" % (mask(who), st["full"], st["ranged"], st["re_full"], st["big"]),
                           "候補: %s で、このファイルは必要な節だけ（offset/limit か Grep で該当節を特定して）読むよう書く" % disp(d[0])))
            else:
                pl.append(("%s が範囲指定なしの全文読みを %d 回（範囲指定あり %d 回）。定義ファイルは見つからず" % (mask(who), st["full"], st["ranged"]),
                           "定義の所在が不明のため、どこを直すかは特定できない"))
            if len(pl) >= 2:
                break
        top_re = max(rd.items(), key=lambda x: x[1]["re"])
        note = ""
        if top_re[1]["re"] and top_re[0] != "(メイン会話)" and top_re[1]["ranged"] >= top_re[1]["full"] and top_re[1]["ranged"] > 0:
            note = "。再読込の主な読み手 %s は既に範囲指定読みが中心（%s）のため、節指定読みの提案は出さない" % (mask(top_re[0]), reader_line(top_re[1]))
        I.append(dict(cat="file", weight=tk(f["weight"]), target=disp(p) + ("（現在は存在しない）" if f["gone"] else ""),
            heavy="読込 計%d 回（範囲指定なしの全文読み %d 回、うち結果20,000字以上 %d 回）、編集を挟まない再読込 %d 回（結果 %s 文字・約%s トークン換算）。読み手別: %s%s"
                  % (f["reads"], f["full"], f["big"], f["re"], T.fmt(f["re_chars"]), T.fmt(tk(f["re_chars"])),
                     "、".join("%s[%s]" % (mask(w), reader_line(st)) for w, st in sorted(rd.items(), key=lambda x: -(x[1]["full"] + x[1]["ranged"]))[:4]), note),
            verify=v_tooluse("Read", "file_path", p) + "   # 読込の一意数\nwc -c %s" % q(p),
            risk=("要検証（ログ内で Write/Edit された形跡のある成果物＝作業対象。全文読みは品質に直結しうる）" if f["edited"]
                  else "クイックウィン（編集された形跡のない参照資料・品質影響が出にくいと考えられる）"), plan=pl,
            check=("品質同等の最小手順: 変更前後で同じ入力3件を流し、成果物の必須項目の欠落・誤りを並べて比較（合格基準は先に決める）。さらに " if f["edited"] else "")
                  + "変更後に同種の作業を流し、`--compare` でこのファイルの全文読み回数と読み手別内訳が減ったか確認。",
            label="実測（回数・文字数・読み手・範囲指定の有無）／推定（無駄かどうか・文脈次第）"))
    for cmd, n, ch in C["bash"]:
        if n < BASH_MIN or SCRIPT_RE.match(cmd) or len([i for i in I if i["cat"] == "bash"]) >= CAP_PER_CATEGORY:
            continue
        c80 = mask(cmd)[:80]
        I.append(dict(cat="bash", weight=tk(ch), target="Bash: " + code(c80),
            heavy="同一コマンド（30文字以上）が期間内に %d 回、結果 計%s 文字（約%s トークン換算）" % (n, T.fmt(ch), T.fmt(tk(ch))),
            verify=(v_tooluse("Bash", "command", cmd[:40], prefix=True) if mask(cmd) == cmd else
                    "# シークレット様文字列を含むため再確認コマンドは省略（コマンド先頭の非機密部分で自分で検索してください）"),
            risk="要検証（決定論化・手順変更＝品質確認が要る）",
            plan=[("同一コマンドが %d 回（%d 回以上）で、スクリプト呼出ではない（複数行/インライン）" % (n, BASH_MIN),
                   "`scripts/` のシェルスクリプトにまとめ、Claude にはスクリプトの実行と結果の要約だけをさせる")],
            check="品質同等の最小手順: 変更前後で同じ入力3件（直近に実際にこのコマンドを使った作業）を流し、(1) 結果の数値・判定が一致するか (2) 失敗時の挙動が同じか を並べて比較。合格基準は事前に書く。",
            label="実測（回数・結果サイズ）／効果は根拠なし"))
    for name, v in C["mcp"]:
        avg = v["chars"] // max(1, v["n"])
        if avg < MCP_AVG_MIN or len([i for i in I if i["cat"] == "mcp"]) >= CAP_PER_CATEGORY:
            continue
        I.append(dict(cat="mcp", weight=tk(v["chars"]), target="MCPツール " + mask(name),
            heavy="呼出 %d 回、結果 計%s 文字（約%s トークン換算）、平均 %s 文字" % (v["n"], T.fmt(v["chars"]), T.fmt(tk(v["chars"])), T.fmt(avg)),
            verify=v_tooluse(name), risk="クイックウィン（品質影響が出にくいと考えられる）",
            plan=[("1回あたりの結果が平均 %s 文字（%s 文字以上）" % (T.fmt(avg), T.fmt(MCP_AVG_MIN)), "取得件数やフィールドを絞る引数がそのツールにあれば、呼出側の指示に足す（引数の有無はログから測れない）")],
            check="再実行後の `--compare` で当該ツールの結果サイズが減ったか確認。", label="実測（呼出数・結果サイズ）"))
    for a in C["agent_list"]:
        if len([i for i in I if i["cat"] == "agent"]) >= CAP_PER_CATEGORY:
            break
        if not (any(T.family(m) == "opus" for m, _ in a["models"]) and a["avg_out"] < SMALL_OUT_PER_RUN) or not a["def"] or a.get("intent"):
            continue                      # 定義も変更手段も無い（組込み等）種別は情報のみ・提案なし
        defs = ("定義ファイル %s（%s バイト）" % (disp(a["def"][0]), T.fmt(a["def"][1]))) if a["def"] else "定義ファイル: 見つからず"
        I.append(dict(cat="agent", weight=a["billed"], target="サブエージェント種別 " + mask(a["type"]),
            heavy="起動 %d 回、合計 %s トークン（キャッシュ読取を除く）、1回あたり平均 %s、出力平均 %s。使用モデル: %s。%s"
                  % (a["runs"], T.fmt(a["billed"]), T.fmt(a["avg_billed"]), T.fmt(a["avg_out"]), "、".join("%s×%d" % (m, n) for m, n in a["models"]), defs),
            verify="grep -lF %s ~/.claude/projects/*/*/subagents/*.meta.json 2>/dev/null | wc -l   # 起動数" % q('"agentType":"' + a["type"] + '"'),
            risk="要検証（モデル段の引き下げ＝品質確認が要る）",
            plan=[("上位モデル（Opus系）を使用し、1回あたりの出力平均が %s トークン（%d 未満）" % (T.fmt(a["avg_out"]), SMALL_OUT_PER_RUN), "下位モデルへの引き下げを検討（要検証・下の品質手順で測る）")],
            check="品質同等の最小手順: 直近の同種別の実入力を3件選び（`grep -lF '\"agentType\":\"%s\"' ~/.claude/projects/*/*/subagents/*.meta.json | head -3` の隣の .jsonl 先頭の user 行）、(1) 現行設定の出力を保存 (2) 変更後に同じ3件を流す (3) 必須項目の欠落・誤りを並べて比較。合格基準（例: 3件中3件で必須項目が揃う）を先に決める。" % mask(a["type"]),
            label="実測（起動数・トークン・モデル）／推定（モデル引き下げの適否）／効果は根拠なし"))
    for a in sorted((x for x in C["agent_list"] if x["def"] and x["an"]), key=lambda x: -x["def"][1] * x["runs"])[:CAP_PER_CATEGORY]:
        pl = plan_text_file(a["an"], "この定義")
        I.append(dict(cat="agentdef", weight=tk(a["def"][1]) * a["runs"], target=disp(a["def"][0]),
            heavy="エージェント定義 %s バイト（約%s トークン換算）×起動 %d 回（起動のたびに文脈へ載る前提の推定）" % (T.fmt(a["def"][1]), T.fmt(tk(a["def"][1])), a["runs"]),
            verify="wc -c %s; grep -n '^#' %s | head -40" % (q(a["def"][0]), q(a["def"][0])),
            risk="クイックウィン（品質影響が出にくいと考えられる・手順の削除は挙動が変わりうるので変更後に確認）", plan=pl,
            check="編集後、同じ種別を1〜2回流して出力形式が変わらないか確認。`--compare` でこの種別の1回あたり平均トークンが下がったか確認（利用量の差に注意）。",
            label="実測（バイト数・節別文字数・起動数）／推定（トークン換算・載り方）"))
    if C["mcp_unused"]:
        I.append(dict(cat="mcpdef", weight=tk(C["mcp_block_chars"]),
            target="MCPサーバー説明（通知された定義ブロック）",
            heavy="通知文 計%s 文字（約%s トークン換算・全セッション合算）。ログ上で呼出が見当たらず、指示ファイルにも記述が見つからないサーバー名: %s%s" % (
                T.fmt(C["mcp_block_chars"]), T.fmt(tk(C["mcp_block_chars"])), "、".join(mask(x) for x in C["mcp_unused"][:8]),
                ("。指示ファイルに記述があり運用で依存している可能性（提案から除外）: " + "、".join(mask(x) for x in C["mcp_depends"][:8])) if C["mcp_depends"] else ""),
            verify="grep -rho '\"type\":\"mcp_instructions_delta\"' ~/.claude/projects --include='*.jsonl' | wc -l",
            risk="クイックウィン（品質影響が出にくいと考えられる）",
            plan=[("通知されたMCPサーバーのうち %d 件に、ログ上で呼出が見当たらない（名称の近似突合）" % len(C["mcp_unused"]), "無効化の候補（呼出が無いことは不要の証明ではない。個別ツール定義のサイズはログから測れない）。手順: " + MCP_HOWTO)],
            check="再実行後の `--compare` で最初の文脈が減ったか確認。", label="実測（文字数）／推定（名称突合・トークン換算）"))
    return I


HEAVY_TOP = 5


def heavy_ranking(C, I):
    """実測の重い順（提案なし含む）上位 HEAVY_TOP 件。サブエージェント種別・MCPツール・ファイル・指示ファイルを共通尺度
    （トークン換算・キャッシュ読取除く）で並べ、提案が立たない理由を付す。I は build_items の全項目。"""
    withplan = {i["target"] for i in I if i.get("plan")}
    rows = []
    for a in C["agent_list"]:
        t = "サブエージェント種別 " + mask(a["type"])
        up = any(T.family(m) == "opus" for m, _ in a["models"])
        if t in withplan or (a["def"] and disp(a["def"][0]) in withplan):
            why = None
        elif a.get("intent"):
            why = "意図的設定の記載あり（%s:%d）のため、モデル引き下げ提案を出さない" % (disp(a["intent"][0]), a["intent"][1])
        elif not a["def"]:
            why = "定義ファイルが見つからない（組込み/プラグイン由来の可能性・変更手段なし）"
        elif not up:
            why = "該当する実測条件が不成立（上位モデル×出力小に当たらない: 使用モデル %s）" % "、".join(m for m, _ in a["models"])
        elif a["avg_out"] >= SMALL_OUT_PER_RUN:
            why = "該当する実測条件が不成立（出力平均 %s トークン/回が閾値 %d 以上）" % (T.fmt(a["avg_out"]), SMALL_OUT_PER_RUN)
        else:
            why = "定義ファイルの履歴系の節・重複段落など、成立した実測条件が無い"
        rows.append(dict(kind="サブエージェント", target=t, weight=a["billed"], why=why,
                         detail="起動 %d 回・1回あたり平均 %s" % (a["runs"], T.fmt(a["avg_billed"]))))
    for name, v in C["mcp"]:
        t = "MCPツール " + mask(name)
        avg = v["chars"] // max(1, v["n"])
        why = None if t in withplan else "該当する実測条件が不成立（結果の平均 %s 文字が閾値 %s 文字未満）" % (T.fmt(avg), T.fmt(MCP_AVG_MIN)) if avg < MCP_AVG_MIN else "上位件数の上限により提案対象外"
        rows.append(dict(kind="MCP", target=t, weight=tk(v["chars"]), why=why, detail="呼出 %d 回・結果 %s 文字" % (v["n"], T.fmt(v["chars"]))))
    for f in C["files"]:
        t = disp(f["path"]) + ("（現在は存在しない）" if f["gone"] else "")
        if t in withplan:
            why = None
        elif f["gone"]:
            why = "ファイルが現在存在しない"
        else:
            why = "該当する実測条件が不成立（範囲指定なしの全文読みが3回以上かつ範囲指定読みより多い読み手が、定義ファイルのあるエージェントにいない）"
        rows.append(dict(kind="ファイル", target=t, weight=tk(f["weight"]), why=why,
                         detail="読込 %d 回・再読込 %d 回・大読込 %d 回" % (f["reads"], f["re"], f["big"])))
    for f in C["instr"]:
        t = disp(f["path"]) + ("（現在は存在しない）" if f["gone"] else "")
        an = f["an"]
        if t in withplan:
            why = None
        elif f["gone"]:
            why = "ファイルが現在存在しない"
        elif an["hist_excl"] and not an["hist"] and not an["dup_chars"]:
            why = "規範語を含むため削減提案から除外（節: %s）" % "、".join("「%s」" % mask(h) for h, _ in an["hist_excl"][:3])
        elif not an["hist"] and not an["dup_chars"]:
            why = "該当する実測条件が不成立（履歴系の節・80文字以上の重複段落なし）" + ("。最も重い節が突出（事実として表示）" if heavy_section(an) else "")
        else:
            why = "上位件数の上限により提案対象外"
        rows.append(dict(kind="指示ファイル", target=t, weight=tk(f["chars"]) * f["sessions"], why=why,
                         detail="%s 文字×%d セッション" % (T.fmt(f["chars"]), f["sessions"])))
    return sorted(rows, key=lambda r: -r["weight"])[:HEAVY_TOP]


def items(C):
    return [i for i in build_items(C) if i.get("plan")]


def top_items(I):
    seen, out = Counter(), []
    for it in sorted(I, key=lambda x: -x["weight"]):
        if seen[it["cat"]] >= CAP_PER_CATEGORY:
            continue
        seen[it["cat"]] += 1
        out.append(it)
    return out[:TOP_N]
def metrics(L, A, C):
    m = {"requests": A["requests"], "sessions": A["sessions"], "cache_rate": A["cache_rate"],
         "first_ctx_median": A["resident"]["first_ctx_median"], "skill_listing_chars": C["skills"]["listing_chars"]}
    for n_, c_ in C["skills"]["desc"].items():
        m["skill_desc:" + mask(n_)] = c_
    for f in C["instr"]:
        m["instr_chars:" + disp(f["path"])] = f["chars"]
    for f in C["files"]:
        m["reread_chars:" + disp(f["path"])] = f["re_chars"]
        m["full_read_count:" + disp(f["path"])] = f["full"]
    for a in C["agent_list"]:
        m["agent_billed:" + mask(a["type"])] = a["billed"]
    for n, v in C["mcp"]:
        m["mcp_result_chars:" + mask(n)] = v["chars"]
    for c, n, ch in C["bash"]:
        m["bash_count:" + mask(c)[:80]] = n
    return m


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o600)


def compare_section(cur, base):
    rows = []
    for k in sorted(set(cur) | set(base)):
        a, b = base.get(k, 0), cur.get(k, 0)
        if a != b:
            rows.append((abs(b - a), k, a, b))
    rows.sort(reverse=True)
    out = ["## 前後比較（ベースライン → 今回）", "",
           "注意: 利用量（期間・セッション数）が違えば合計値は単純比較できません。ベースライン: %s リクエスト／%s セッション、今回: %s リクエスト／%s セッション。"
           "効果の判断は、同種の作業量に揃えたうえで、セッションあたりの値（最初の文脈など）で見てください。" % (
               T.fmt(base.get("requests", 0)), T.fmt(base.get("sessions", 0)), T.fmt(cur["requests"]), T.fmt(cur["sessions"])), "",
           "| 指標 | ベースライン | 今回 | 差 |", "|---|---|---|---|"]
    for _, k, a, b in rows[:25]:
        out.append("| %s | %s | %s | %+g |" % (k.replace("|", "\\|"), a, b, round(b - a, 1)))
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- 出力


# ---------------------------------------------------------------- 出力
def render(L, A, C, I, cmp_text=""):
    o = []
    w = o.append
    dn = dict(C["denom"], insufficient=C["denom"]["sessions"] < MIN_SESSIONS)
    denom_txt = "分母: ログファイル%s件中、利用量を数えられたファイル%s件（うちサブエージェントログ%s件）／セッション%s／リクエスト%s／期間 %s〜%s" % (
        T.fmt(dn["files"]), T.fmt(dn["usage_files"]), T.fmt(dn["sub_logs"]), T.fmt(dn["sessions"]), T.fmt(dn["requests"]), dn["period"][0], dn["period"][1])

    def sec(title):
        w("\n## " + title)
        w("*（%s）*\n" % denom_txt)
    w("# action-brief（ローカル詳細版）\n")
    w("> **LOCAL ONLY・共有しない。** このファイルにはファイルパス・コマンド断片・スキル/エージェント名が含まれます。管理者や外部へ送るのは、匿名の `report.md` / `report.html` のほうです。（ファイル権限 0600）\n")
    w("## Claudeへの指示（このファイルを読ませるときの冒頭文）\n")
    w("以下は私の環境の実測。各項目の根拠を再確認してから、変更案を出し、変更前後で測れるようにして。")
    w("（再確認 = 各項目の「根拠の再確認コマンド」を実行。変更案は私の承認後に適用。効果の%は根拠がないので書かないこと。変更案は『成立した実測条件』つきの候補で、条件が成立していない一般論は載せていません。）\n")
    w("**%s。** 同一requestIdは畳み済み。重さは「トークン換算（4文字=1トークン・推定）」を基準にした目安で、項目ごとに定義が違うため厳密な比較ではありません。\n" % denom_txt)
    top = top_items(I)
    if dn["insufficient"]:
        w("## データ不足: 判断に足りない\n")
        w("利用量を数えられたセッションが %d 件で、最低観測量（%d 件。**閾値は仮置き**）に達していません。優先アクションは出しません。以下の表は事実の表示のみで、"
          "『ログ内0回』『参照0件』などは観測が少ないため無効化・削除の根拠に使わないでください（例: ログファイルが多くても、assistantの利用量記録を含むのが1セッションだけの場合がある）。\n" % (dn["sessions"], MIN_SESSIONS))
    else:
        sec("優先アクション上位%d（実測の重さ順・実測条件が成立した提案があるもののみ）" % len(top))
        w("同じ種類は最大%d件まで。順位は削減見込みではなく実測の重さ。\n" % CAP_PER_CATEGORY)
        for i, it in enumerate(top, 1):
            w("### %d. %s\n" % (i, it["target"]))
            w("- 実測の重さ: %s" % it["heavy"])
            w("- 根拠の再確認コマンド:\n  ```\n  " + it["verify"].replace("\n", "\n  ") + "\n  ```")
            w("- リスク区分: %s" % it["risk"])
            w("- 変更案（候補）:")
            for cond, prop in it["plan"]:
                w("  - 【成立した実測条件】%s\n    → %s" % (cond, prop))
            w("- 変更後の確認方法: %s" % it["check"])
            w("- 根拠ラベル: %s\n" % it["label"])
    # 実測の重い順（提案なし含む）
    sec("実測の重い順 上位%d（提案が立たないものを含む）" % HEAVY_TOP)
    w("サブエージェント種別・MCPツール・ファイル・指示ファイルを共通尺度（トークン換算・キャッシュ読取除く）で並べる。優先アクション一覧とは別枠で、"
      "提案が立たない項目も載せ、その理由を付す。尺度は項目ごとに定義が異なる（種別=課金対象トークン、他=文字数÷4の概算×セッション数等）ため厳密な比較ではない。\n")
    hv = heavy_ranking(C, build_items(C))
    if hv:
        w("| # | 種類 | 対象 | 重さ(トークン換算) | 実測 | 提案 |\n|---|---|---|---|---|---|")
        for n_, r in enumerate(hv, 1):
            w("| %d | %s | %s | %s | %s | %s |" % (n_, r["kind"], r["target"].replace("|", "\\|"), T.fmt(r["weight"]), r["detail"],
               "優先アクション参照" if r["why"] is None else "提案なし: " + r["why"]))
    else:
        w("- 該当なし")
    # スキル
    s = C["skills"]
    sec("スキル（説明文の長さ順・出所・ログ内の呼出回数）")
    if s["desc"]:
        w("件数の定義: **解析できた %d 件** = メインセッションの全 skill_listing（サブエージェントのログは除く）の名前の和集合。ログ記録 skillCount の最頻値は %s、**最新セッションの値は %s（解析 %s 件）**、セッションごとの解析件数の中央値は %s。" % (
            s["parsed"], s["logged_mode"] if s["logged_mode"] is not None else "不明", s["latest_logged"] if s["latest_logged"] is not None else "不明",
            s["latest_parsed"] if s["latest_parsed"] is not None else "不明", s["per_session_median"] if s["per_session_median"] is not None else "不明"))
        w("最頻値が少数セッションに偏る場合は信頼できません（セッション数 %d）。" % s["sessions"])
        w("『呼出』は Skill ツール＋`<command-name>` 入力の一意な合算で、**読み込んだログの期間内**の回数（`--root` で絞った場合は全ログでの回数を括弧で併記）。他の発動経路は測れず過小評価の可能性。"
          "出所はファイルの存在確認と名前で判定できた範囲のみ（`.claude/commands` の自前ファイルは project）。`<提供元>:<名前>`・synced 配下は『配布・編集不可』、判定できないものは『不明』。"
          "参照は %d 個のgitルート内のファイル数（自スキル内・サードパーティ配下 themes/vendor/node_modules 等は除外）。固有名は単語一致、一般語は厳密形式のみ。"
          "**参照0件・ログ内0回は安全の証明ではない**: .claude の commands/agents/skills 本文での言及（ワークフローから呼ばれる可能性）も拾うが、プラグイン経由の呼出は推定できない。\n" % s["ref_roots"])
        if s["dups"]:
            w("重複掲載の可能性（正規化すると同名）: " + "／".join("、".join(mask(x) for x in g) for g in s["dups"]) + "\n")
        w("| スキル | 出所 | 編集可否 | 一覧上の説明(字) | 実ファイル説明(字) | 呼出 | 退避判定（0回のみ） |\n|---|---|---|---|---|---|---|")
        rows = sorted(s["desc"].items(), key=lambda x: -x[1])
        for n, c in rows[:TABLE_TOP]:
            k = s["calls"].get(n, 0) + s["slash"].get(n, 0)
            o_ = s["origins"][n]
            ac = (s["all_calls"] or {}).get(n, 0) if s["all_calls"] else 0
            cell = str(k) if k else ("0回（全ログ%d回）" % ac if ac else "0回")
            stale = s["has_latest"] and n not in s["cur"]
            if k or ac:
                jd = ""
            elif dn["insufficient"]:
                jd = ""
            else:
                r = s["refs"].get(n)
                if stale:
                    jd = "現在の一覧にない（過去の名前）"
                elif not r:
                    jd = ""
                elif r["files"] == 0 and not r["rel"]:
                    jd = "一般語のため判定不能" if r["generic"] else "退避候補（参照0件）"
                else:
                    bits = []
                    if r["files"]:
                        bits.append("%s参照%d件%s" % ("厳密形式の" if r["generic"] else "", r["files"], "・CI/検査系" if r["ci"] else ""))
                    if r["rel"]:
                        bits.append("ワークフロー言及%d件" % len(r["rel"]))
                    jd = "退避注意（" + "、".join(bits) + "）"
            w("| %s | %s | %s | %s | %s | %s | %s |" % (mask(n), o_["origin"], EDIT[o_["origin"]], T.fmt(c),
               T.fmt(o_["file_desc"]) if o_["file_desc"] is not None else "—", cell, jd))
        if len(rows) > TABLE_TOP:
            w("\n（上位%d件のみ。残り%d件は `--snapshot` の baseline.json（`skill_desc:` で始まる行）に含まれます。）" % (TABLE_TOP, len(rows) - TABLE_TOP))
        warn = [(n, s["refs"][n]) for n in s["desc"] if n in s["refs"] and (s["refs"][n]["files"] > 0 or s["refs"][n]["rel"]) and not (s["has_latest"] and n not in s["cur"])]
        if warn and not dn["insufficient"]:
            w("\n参照元・言及元の例（退避注意の確認用・最大3件）:")
            for n, r in warn[:20]:
                w("- %s: %s" % (mask(n), "、".join(disp(p) for p in (r["examples"] + r["rel"])[:4])))
        if any(o["origin"] != "project" and o["origin"] != "user" for o in s["origins"].values()):
            w("\n無効化手順（公式ドキュメントで確認できた範囲のみ）:")
            for org in ("project", "plugin", "配布", "不明"):
                if any(o["origin"] == org for o in s["origins"].values()) or org == "project":
                    w("- 出所 %s: %s" % (org, disable_howto(org)))
    else:
        w("スキル一覧がログに見つかりませんでした（測れない）。")
    # エージェント（事実表示）
    sec("サブエージェント（種別ごと・重い順）")
    if not C["agent_list"]:
        w("ログなし（サブエージェントのログがありません）。")
    if C["agent_list"]:
        w("| 種別 | 起動 | 合計トークン(キャッシュ読取除く) | 1回あたり平均 | 出力平均/回 | モデル | 定義ファイル |\n|---|---|---|---|---|---|---|")
        for a in C["agent_list"][:20]:
            w("| %s | %d | %s | %s | %s | %s | %s |" % (mask(a["type"]), a["runs"], T.fmt(a["billed"]), T.fmt(a["avg_billed"]), T.fmt(a["avg_out"]),
                "、".join("%s×%d" % x for x in a["models"]),
                ("%s (%s B)" % (disp(a["def"][0]), T.fmt(a["def"][1]))) if a["def"] else "見つからず"))
        w("\n種別は各サブエージェントログの隣の `.meta.json` の agentType から取得。取れないものは「(種別不明)」。定義ファイルは cwd 以下と `~/.claude/agents/` から探すため、プラグイン/組込みは「見つからず」になります。")
        # モデル差分（差分があるものだけ）
        w("\n### モデル差分（定義の frontmatter `model` と実使用モデルが食い違う種別のみ）\n")
        diff = []
        for a in C["agent_list"]:
            fam = T.family(a["fm_model"]) if a.get("fm_model") else None
            if not fam:
                continue
            other = [(m, n) for m, n in a["models_all"].most_common() if T.family(m) != fam]
            if other:
                diff.append("- %s: 定義 `model: %s`／実使用 %s" % (mask(a["type"]), mask(a["fm_model"]), "、".join("%s×%d" % x for x in a["models_all"].most_common())))
        w("\n".join(diff) if diff else "- 該当なし（定義に model 指定があり、実使用が食い違う種別はありません）")
        # 起動元
        w("\n### 種別×起動元（親セッションの最初のスラッシュコマンド。無ければ最初に呼んだSkill。取れなければ『起動元不明』）\n")
        w("| 種別 | 起動 | 起動元の内訳（回数） |\n|---|---|---|")
        for a in C["agent_list"][:12]:
            lc = C["launch"].get(a["type"])
            if lc:
                w("| %s | %d | %s |" % (mask(a["type"]), sum(lc.values()), "、".join("%s×%d" % (mask(k), v) for k, v in lc.most_common(5))))
        # 定義の節別サイズ
        w("\n### エージェント定義の節別サイズ（起動のたびに載る量が多い順・事実のみ）\n")
        defs = sorted((x for x in C["agent_list"] if x["def"] and x["an"]), key=lambda x: -x["def"][1] * x["runs"])[:5]
        for a in defs:
            w("- %s（%s B×起動%d）: %s" % (disp(a["def"][0]), T.fmt(a["def"][1]), a["runs"], "、".join("「%s」%s" % (mask(h), T.fmt(n)) for h, n in a["an"]["sections"][:4])))
        if not defs:
            w("- 該当なし")
        sm_all = [a for a in C["agent_list"] if any(T.family(m) == "opus" for m, _ in a["models"]) and a["avg_out"] < SMALL_OUT_PER_RUN]
        sm = [a for a in sm_all if not a.get("intent")]
        w("\n### モデル×種別: 上位モデルなのに1回あたり出力が小さい種別（要検証候補・閾値 %d トークン/回は仮置き）\n" % SMALL_OUT_PER_RUN)
        w("\n".join("- %s（出力平均 %s/回、モデル %s）" % (mask(a["type"]), T.fmt(a["avg_out"]), "、".join(m for m, _ in a["models"])) for a in sm) or "- 該当なし")
        intl = [a for a in sm_all if a.get("intent")]
        if intl:
            w("\n意図的設定の記載あり（指示ファイルで種別名とモデル名が同一段落/同一行にあるため、モデル引き下げ提案は出さず要検証候補から外す。表には残す）:")
            for a in intl:
                w("- %s（出力平均 %s/回、モデル %s）: 意図的設定の記載あり（%s:%d）" % (mask(a["type"]), T.fmt(a["avg_out"]), "、".join(m for m, _ in a["models"]), disp(a["intent"][0]), a["intent"][1]))
        nodef = [a for a in sm if not a["def"]]
        if nodef:
            w("\n情報のみ・提案なし（定義ファイルも変更手段も見つからない種別。組込み/プラグイン由来の可能性）: " + "、".join(mask(a["type"]) for a in nodef))
        w("\n品質同等の最小手順: 同じ実入力3件を、変更前/後で流して出力を並べ、必須項目の欠落・誤りを比較（合格基準は先に決める）。")
        called = [a for a in C["agent_list"] if a.get("callers")]
        if called:
            w("\n呼出関係の手がかり（プロジェクトの .claude/commands・agents・skills の本文で名前が言及されている種別。プラグイン経由は推定できない）:")
            for a in called[:10]:
                w("- %s ← %s" % (mask(a["type"]), "、".join(disp(p) for p in a["callers"][:3])))
    # ファイル
    sec("ファイル（読み手別・範囲指定の有無）")
    w("定義: **範囲指定なし = Read に offset も limit も無い全文読み**（結果サイズ不問）。うち結果が20,000文字以上のものを『大読込』として別掲。再読込 = 同一セッションで、前回の読込以降に編集を挟まずに同じファイルを再度読んだ回数。読み手別の [範囲あり／全文] は、そのファイルへの読込回数（再読込に限らない）。\n")
    if not C["files"]:
        w("- 該当なし（再読込・大読込のあるファイルはありません）")
    else:
        w("『種別』= ログ内で Write/Edit された形跡があれば成果物（作業対象）、無ければ参照資料。\n")
        w("| ファイル | 種別 | 読込計 | 全文読み | 大読込 | 再読込 | 再読込の結果(文字) | 読み手別 [範囲あり／全文] |\n|---|---|---|---|---|---|---|---|")
    for f in C["files"][:20]:
        w("| %s | %s | %d | %d | %d | %d | %s | %s |" % (disp(f["path"]) + ("（現在は存在しない）" if f["gone"] else ""), "成果物" if f["edited"] else "参照資料", f["reads"], f["full"], f["big"], f["re"], T.fmt(f["re_chars"]),
            "、".join("%s[%s]" % (mask(a), reader_line(st)) for a, st in sorted(f["readers"].items(), key=lambda x: -(x[1]["full"] + x[1]["ranged"]))[:4])))
    # 指示ファイル
    sec("指示ファイル（CLAUDE.md 等）")
    shown = [f for f in C["instr"] if f["an"]["hist"] or f["an"]["hist_excl"] or f["an"]["dup_chars"] or heavy_section(f["an"])]
    if shown:
        w("表示条件: 履歴/経緯系の見出し・重複段落がある、または最も重い節が3,000文字以上かつ全体の30%以上。"
          "履歴系の判定は見出し語のみのため『内容を確認して判断』。見出しまたは節冒頭に規範語（必須/禁止/手順/ルール/must/required 等）を含む節は履歴系から除外する。\n")
        for f in shown[:10]:
            tags = [x for x in (("履歴系の節あり" if f["an"]["hist"] else ""), ("重複段落あり" if f["an"]["dup_chars"] else ""), ("重い節あり" if heavy_section(f["an"]) else "")) if x]
            w("- %s: %s 文字×%d セッション（%s）。節別: %s" % (disp(f["path"]) + ("（現在は存在しない）" if f["gone"] else ""), T.fmt(f["chars"]), f["sessions"], "・".join(tags),
                "、".join("「%s」%s" % (mask(h), T.fmt(n)) for h, n in f["sections"][:5])))
            if f["an"]["hist_excl"]:
                w("  - 規範語を含むため削減提案から除外: " + "、".join("「%s」%s文字" % (mask(h), T.fmt(n)) for h, n in f["an"]["hist_excl"][:4]))
    else:
        w("- 該当なし（履歴/経緯系の見出し・重複段落・突出して重い節のいずれも見つかりませんでした）")
    # Bash
    sec("Bash（同一コマンドの反復・%d 回以上・80文字で切断・シークレットはマスク）" % BASH_MIN)
    brows = [(c, n, ch) for c, n, ch in C["bash"] if n >= BASH_MIN]
    if brows:
        w("| コマンド | 回数 | 結果(文字) | 形態 |\n|---|---|---|---|")
        for c, n, ch in brows[:15]:
            w("| `%s` | %d | %s | %s |" % (mask(c)[:80].replace("|", "\\|").replace("`", "'").replace("\n", " "), n, T.fmt(ch),
               "単一スクリプト呼出（スクリプト化の余地なし）" if SCRIPT_RE.match(c) else "スクリプト呼出ではない（決定論化の候補）"))
    else:
        w("- 該当なし（%d 回以上反復された30文字以上の同一コマンドはありません）" % BASH_MIN)
    # MCP
    sec("MCP")
    heavy_mcp = [(n, v) for n, v in C["mcp"] if v["chars"] // max(1, v["n"]) >= MCP_AVG_MIN]
    if heavy_mcp or C["mcp_unused"] or C["mcp_depends"]:
        if heavy_mcp:
            w("結果の平均が %s 文字以上のツール:\n" % T.fmt(MCP_AVG_MIN))
            w("| ツール | 呼出 | 結果(文字) | 平均 |\n|---|---|---|---|")
            for n, v in heavy_mcp[:20]:
                w("| %s | %d | %s | %s |" % (mask(n), v["n"], T.fmt(v["chars"]), T.fmt(v["chars"] // max(1, v["n"]))))
        if C["mcp_unused"]:
            w("\nログ上で呼出が見当たらず、指示ファイルにも記述が見つからない通知済みサーバー: %s" % "、".join(mask(x) for x in C["mcp_unused"][:15]))
        if C["mcp_depends"]:
            w("\nログ上で呼出が見当たらないが、指示ファイル（CLAUDE.md 等）に記述があり運用で依存している可能性があるサーバー（提案から除外）: %s" % "、".join(mask(x) for x in C["mcp_depends"][:15]))
        if C["mcp_unused"]:
            w("\n無効化手順（公式で確認できた範囲）: " + MCP_HOWTO)
    else:
        w("- 該当なし（結果の平均が %s 文字以上のツールも、呼出が見当たらないサーバーもありません）" % T.fmt(MCP_AVG_MIN))
    w("\n## 測れなかった項目")
    w("- 品質（出力の良し悪し）、個別スキル/MCP定義の正確なトークン数、プランの残り枠、スキルのSkillツール/スラッシュコマンド以外での発動、メイン会話での再読込の理由、他端末・Web版の利用、削減効果（%・円）。")
    w("- 出所は存在確認と名前で判定できた範囲のみ（組込みと配布の区別は不可）。プラグイン経由の呼出関係は推定できない。")
    w("\n## 再実行して前後を比較する")
    w("`python3 tokdiet.py --out <dir> --snapshot <dir>/baseline.json`（変更前・パスを含むため共有しない）→ 変更後に `--compare <dir>/baseline.json`（末尾「前後比較」に出る。利用量が違うときはセッションあたりの指標で見る）。\n")
    if cmp_text:
        w(cmp_text)
    return "\n".join(o)


def write(L, A, out, snapshot=None, compare=None):
    if not A["requests"]:
        return None
    C = collect(L, A)
    I = items(C)
    cur = metrics(L, A, C)
    cmp_text = ""
    if compare:
        with open(compare, encoding="utf-8") as fh:
            cmp_text = compare_section(cur, json.load(fh))
    text = mask(render(L, A, C, I, cmp_text))   # 最終段でも全体にマスクをかける
    path = os.path.join(out, "action-brief.md")
    write_private(path, text)
    if snapshot:
        write_private(snapshot, json.dumps(cur, ensure_ascii=False, indent=1))
    return path
