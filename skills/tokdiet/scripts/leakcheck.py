#!/usr/bin/env python3
"""leakcheck -- tokdiet のレポートに入力ログ由来の情報が漏れていないかを機械検査する。
検出が1件でもあれば終了コード1。

検査:
  A パターン: ホームディレクトリ・ユーザー名・絶対パス・相対パス様・Windowsパス・メール・URL(許可リスト外)・UUID・長い16進/トークン様
  B 識別子:   (--logs 指定時) ログ中の cwd・プロジェクトディレクトリ名・セッションIDがレポートに現れないか
  C 長い文字列: (--logs 指定時) レポート各行の20文字連続部分のうち、ツール自身のソース(テンプレ文言)に無く、
              ログ生テキストに現れるもの
使い方: leakcheck.py report.md [report.html ...] [--logs ~/.claude/projects または ~/.claude/projects/<proj>]
"""
import argparse, getpass, json, os, re, sys

ALLOWED_URL = re.compile(r"^https://code\.claude\.com/docs/en/costs$")
HERE = os.path.dirname(os.path.abspath(__file__))
WIN = 20


def pattern_checks(text):
    hits = []
    text = text.replace("https://code.claude.com/docs/en/costs", "")
    home = os.path.expanduser("~")
    if home and home != "/" and home in text:
        hits.append(("home", home))
    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    if len(user) >= 4 and re.search(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(user), text):
        hits.append(("username", user))
    pats = [
        ("abs-path", r"(?<![\w:/.])/(?:home|Users|root|tmp|var|etc|mnt|opt|usr|srv|workspace|Volumes)/[^\s\"'<>)]+"),
        ("tilde-path", r"(?<![\w])~/[^\s\"'<>)]+"),
        ("win-path", r"\b[A-Za-z]:\\[^\s\"'<>]+"),
        ("rel-path", r"(?<![\w:/.])(?:[A-Za-z0-9_.\-]+/){2,}[A-Za-z0-9_.\-]+"),
        ("email", r"[\w.+\-]+@[\w\-]+\.[\w.\-]+"),
        ("uuid", r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
        ("hex-id", r"\b[0-9a-f]{24,}\b"),
        ("token", r"\b(?:sk|ghp|gho|xox[bap]|AKIA)[-_A-Za-z0-9]{12,}"),
        ("jsonl", r"\.jsonl\b"),
    ]
    for name, p in pats:
        for m in re.finditer(p, text):
            s = m.group(0)
            if name == "rel-path" and not re.search(r"[A-Za-z]", s):
                continue   # 日付 2026/10/10 等
            if name == "rel-path" and re.fullmatch(r"[\w.\-]+/[\w.\-]+/[\w.\-]+", s) and len(s) < 12:
                continue
            hits.append((name, s[:60]))
    for m in re.finditer(r"https?://[^\s\"'<>)]+", text):
        u = m.group(0).rstrip(".,;")
        if not ALLOWED_URL.match(u):
            hits.append(("url", u[:60]))
    # 許可URLが rel-path に誤検出されるのを除く
    hits = [(n, s) for n, s in hits if not (n in ("rel-path",) and "code.claude.com" in s)]
    return hits


def corpus(root):
    ident, raws = set(), []
    for dp, _, fns in os.walk(root):
        for fn in fns:
            if not fn.endswith(".jsonl"):
                continue
            p = os.path.join(dp, fn)
            rel = os.path.relpath(p, root).split(os.sep)
            # projectsルート(<proj>/<session>.jsonl)でもプロジェクト単体(<session>.jsonl, <session>/subagents/..)でも動く:
            # ファイル名より上のディレクトリ名と、直下のjsonlは語幹(セッションID)を識別子にする。
            ident.update(d for d in rel[:-1] if d != "subagents")
            if len(rel) <= 2:
                ident.add(os.path.splitext(rel[-1])[0])
            if len(rel) == 1:
                ident.add(os.path.basename(os.path.abspath(root)))      # プロジェクト単体を渡した場合のプロジェクト名
            raw = open(p, encoding="utf-8", errors="replace").read()
            raws.append(raw)
            for line in raw.splitlines():
                if '"cwd"' in line:
                    try:
                        c = json.loads(line).get("cwd")
                        if c:
                            ident.add(c)
                    except Exception:
                        pass
    return {i for i in ident if len(i) >= 6}, raws


def identifier_checks(text, ident):
    return [("log-identifier", i[:12] + "…") for i in ident if i in text]


def template_fragments():
    """tokdiet.py の文字列リテラルを書式指定子で割った断片（ツール自身の定型文）。"""
    import ast
    frags = set()
    p = os.path.join(HERE, "tokdiet.py")
    if os.path.exists(p):
        for n in ast.walk(ast.parse(open(p, encoding="utf-8").read())):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                for part in re.split(r"%[-0-9.]*[sdf]|%%|\n", n.value.replace("**", "")):
                    if len(part) >= 3:
                        frags.add(part)
    return sorted(frags, key=len, reverse=True)


def long_string_checks(text, raws, frags):
    """定型文断片とモデル名を伏せ、残った動的部分の20文字窓がログ生テキストに現れるかを見る。"""
    big = "\n".join(raws)
    hits = []
    for line in text.splitlines():
        line = re.sub(r"claude-[a-z0-9.\-]+", "\0", line)
        for f in frags:
            if f in line:
                line = line.replace(f, "\0" * len(f))
        for run in line.split("\0"):
            for i in range(0, max(0, len(run) - WIN + 1)):
                w = run[i:i + WIN]
                if re.search(r"[A-Za-z぀-ヿ一-鿿]", w) and w in big:
                    hits.append(("log-substring", w[:30] + "…"))
                    if len(hits) >= 20:
                        return hits
    return hits


def check(files, logs=None):
    hits = []
    ident = raws = None
    frags = template_fragments()
    if logs:
        ident, raws = corpus(logs)
    for f in files:
        text = open(f, encoding="utf-8", errors="replace").read()
        h = pattern_checks(text)
        if f.endswith(".html"):   # タグと実体参照を外した可視テキストでもC検査する
            import html as _h
            text = _h.unescape(re.sub(r"<[^>]+>", "", text))
        text = text.replace("**", "")
        if logs:
            h += identifier_checks(text, ident)
            h += long_string_checks(text, raws, frags)
        hits += [(os.path.basename(f),) + x for x in h]
    return hits


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--logs", help="入力ログのルート（B/C検査を有効化）")
    a = ap.parse_args()
    hits = check(a.files, a.logs)
    for f, kind, s in hits:
        print("LEAK %s: %s: %s" % (f, kind, s))
    print("leakcheck: %d 件検出 (%s)" % (len(hits), "A+B+C" if a.logs else "Aのみ"))
    sys.exit(1 if hits else 0)


if __name__ == "__main__":
    main()
