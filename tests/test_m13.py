"""M1.3: 意図的モデル設定の除外／規範語節の履歴系除外／重い順上位5（提案なし含む）／leakcheck のプロジェクト単体対応。合成データのみ。"""
import os, sys, tempfile, unittest, unittest.mock, json
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE)); sys.path.insert(0, HERE)
import tokdiet, actionbrief, leakcheck
from synth2 import J, rec, asst


def sub_asst(rid, ts, cwd, model, out):
    r = asst(rid, ts, cwd, [], out=out)
    r["isSidechain"] = True
    r["message"]["model"] = model
    return r


def build(base, rules_text):
    repo = os.path.join(base, "repo")
    for d in (".git", ".claude/agents", ".claude/rules"):
        os.makedirs(os.path.join(repo, d), exist_ok=True)
    for n in ("alpha-agent", "beta-agent"):
        open(os.path.join(repo, ".claude/agents/%s.md" % n), "w").write("---\nname: %s\nmodel: opus\n---\nbody\n" % n)
    open(os.path.join(repo, ".claude/rules/pipe.md"), "w").write(rules_text)
    root = os.path.join(base, "projects"); d = os.path.join(root, "-repo-proj")
    os.makedirs(os.path.join(d, "sess-A", "subagents"))
    open(os.path.join(d, "sess-A.jsonl"), "w").write(J(asst("m1", "2026-01-01T00:00:00Z", repo, [], out=5)) + "\n")
    for i, (typ, model, out) in enumerate([("alpha-agent", "claude-opus-4-1", 100), ("beta-agent", "claude-opus-4-1", 100),
                                           ("general-purpose", "claude-opus-4-1", 100)]):
        ts = "2026-01-01T00:0%d:00Z" % (i + 1)
        open(os.path.join(d, "sess-A", "subagents", "agent-%d.jsonl" % i), "w").write(J(sub_asst("s%d" % i, ts, repo, model, out)) + "\n")
        open(os.path.join(d, "sess-A", "subagents", "agent-%d.meta.json" % i), "w").write(J({"agentType": typ}))
    return root, repo


RULES = ("# Rules\n\nモデル振り分け: alpha-agent は opus で回す（意図的）。\n\n"
         "beta-agent はこの文書では\n名前だけ登場する。\n\n別段落で sonnet について述べる。\n")


class Intent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.root, cls.repo = build(cls.tmp, RULES)
        cls.L = tokdiet.load(cls.root); cls.A = tokdiet.analyze(cls.L)
        cls.C = actionbrief.collect(cls.L, cls.A)

    def test_find_intent_same_paragraph_only(self):
        r = actionbrief.find_intent(["alpha-agent", "beta-agent"], [("p.md", RULES, False)])
        self.assertEqual(r, {"alpha-agent": ("p.md", 3)})              # beta は段落にモデル名が無い
        r = actionbrief.find_intent(["x-agent"], [("d.md", "---\nname: x-agent\nmodel: opus\n---\nbody\n", True)])
        self.assertEqual(r, {})                                       # 定義の frontmatter は対象外
        r = actionbrief.find_intent(["x-agent"], [("d.md", "---\nname: y\n---\nx-agent は Sonnet 固定\n", True)])
        self.assertEqual(r, {"x-agent": ("d.md", 4)})                  # 本文は対象（frontmatter を含めた実行番号）

    def test_collect_and_no_downgrade_proposal(self):
        ag = self.C["agents"]
        self.assertEqual(ag["alpha-agent"]["intent"][1], 3)
        self.assertTrue(ag["alpha-agent"]["intent"][0].endswith("pipe.md"))
        self.assertIsNone(ag["beta-agent"]["intent"])
        targets = {i["target"] for i in actionbrief.items(self.C) if i["cat"] == "agent"}
        self.assertNotIn("サブエージェント種別 alpha-agent", targets)
        self.assertIn("サブエージェント種別 beta-agent", targets)

    def test_render_marks_intentional(self):
        with unittest.mock.patch.object(actionbrief, "MIN_SESSIONS", 1):
            out = actionbrief.render(self.L, self.A, self.C, actionbrief.items(self.C))
        self.assertRegex(out, r"- alpha-agent[^\n]*意図的設定の記載あり（[^）]*pipe\.md:3）")
        cand = out.split("要検証候補")[1].split("意図的設定の記載あり")[0]
        self.assertIn("beta-agent", cand); self.assertNotIn("alpha-agent", cand)


class Normative(unittest.TestCase):
    def test_heading_word_alone_fires(self):
        a = actionbrief.analyze_text("# T\n## 変更履歴\n" + "x" * 50 + "\n")
        self.assertEqual([h for h, _ in a["hist"]], ["変更履歴"]); self.assertEqual(a["hist_excl"], [])
        pl = actionbrief.plan_text_file(a, "f")
        self.assertIn("内容を確認して判断", pl[0][1])
        self.assertNotIn("削除", pl[0][1])

    def test_normative_opening_excluded(self):
        txt = ("# T\n## 過去の学びの参照（必須）\nS0開始時に読む。\n" + "x" * 50 + "\n"
               "## 背景\nこのルールに従うこと。\n## 経緯\nmust read before start\n## 履歴\n昔の話。\n昔の話2。\n昔の話3。\n昔の話4。\n昔の話5(手順は後ろの行なので対象外)\n")
        a = actionbrief.analyze_text(txt)
        self.assertEqual([h for h, _ in a["hist_excl"]], ["過去の学びの参照（必須）", "背景", "経緯"])
        self.assertEqual([h for h, _ in a["hist"]], ["履歴"])
        self.assertEqual(len(actionbrief.plan_text_file(a, "f")), 1)
        only = actionbrief.analyze_text("# T\n## 過去の学び（禁止事項）\n" + "x" * 50 + "\n")
        self.assertEqual(actionbrief.plan_text_file(only, "f"), [])      # 提案なし

    def test_render_states_exclusion(self):
        tmp = tempfile.mkdtemp()
        root, repo = build(tmp, "# R\n## 過去の学び（必須）\n" + "y" * 100 + "\n")
        L = tokdiet.load(root); A = tokdiet.analyze(L); C = actionbrief.collect(L, A)
        C["instr"] = [{"path": os.path.join(repo, "CLAUDE.md"), "chars": 120, "sessions": 2, "gone": False,
                       "an": actionbrief.analyze_text("# R\n## 過去の学び（必須）\n" + "y" * 100 + "\n"), "sections": []}]
        C["instr"][0]["sections"] = C["instr"][0]["an"]["sections"]
        with unittest.mock.patch.object(actionbrief, "MIN_SESSIONS", 1):
            out = actionbrief.render(L, A, C, actionbrief.items(C))
        self.assertIn("規範語を含むため削減提案から除外: 「過去の学び（必須）」", out)


class Heavy(unittest.TestCase):
    def test_ranking_includes_no_proposal_with_reasons(self):
        tmp = tempfile.mkdtemp()
        root, repo = build(tmp, RULES)
        L = tokdiet.load(root); A = tokdiet.analyze(L); C = actionbrief.collect(L, A)
        hv = actionbrief.heavy_ranking(C, actionbrief.build_items(C))
        by = {r["target"]: r for r in hv}
        gp = by["サブエージェント種別 general-purpose"]
        self.assertIn("定義ファイルが見つからない", gp["why"])                         # 定義なし・提案なしでも載る
        self.assertIn("意図的設定の記載あり", by["サブエージェント種別 alpha-agent"]["why"])
        self.assertIsNone(by["サブエージェント種別 beta-agent"]["why"])               # 提案あり
        ws = [r["weight"] for r in hv]
        self.assertEqual(ws, sorted(ws, reverse=True))
        self.assertLessEqual(len(hv), actionbrief.HEAVY_TOP)
        with unittest.mock.patch.object(actionbrief, "MIN_SESSIONS", 1):
            out = actionbrief.render(L, A, C, actionbrief.items(C))
        self.assertIn("実測の重い順 上位5", out)
        self.assertRegex(out, r"general-purpose \|[^\n]*提案なし: 定義ファイルが見つからない")

    def test_top5_cap_and_mixed_kinds(self):
        C = {"agent_list": [dict(type="a%d" % i, runs=1, billed=1000 - i, avg_billed=1, avg_out=1, models=[("claude-sonnet-5", 1)], **{"def": None}, intent=None) for i in range(4)],
             "mcp": [("mcp__s__t%d" % i, {"n": 1, "chars": 4000 * (i + 1)}) for i in range(4)],
             "files": [], "instr": []}
        hv = actionbrief.heavy_ranking(C, [])
        self.assertEqual(len(hv), 5)
        self.assertEqual({r["kind"] for r in hv}, {"サブエージェント", "MCP"})
        self.assertTrue(all(r["why"] for r in hv))


class LeakProjectDir(unittest.TestCase):
    def test_corpus_accepts_root_and_single_project(self):
        tmp = tempfile.mkdtemp()
        root, _ = build(tmp, RULES)
        proj = os.path.join(root, "-repo-proj")
        for r in (root, proj):
            ident, raws = leakcheck.corpus(r)                  # IndexError にならない
            self.assertTrue(raws)
            self.assertIn("-repo-proj", ident)
        ident, _ = leakcheck.corpus(proj)
        self.assertIn("sess-A", ident)
        self.assertNotIn("subagents", ident)

    def test_direct_jsonl_dir(self):
        d = os.path.join(tempfile.mkdtemp(), "-proj-dirname")
        os.makedirs(d)
        open(os.path.join(d, "session-1234.jsonl"), "w").write(J({"cwd": "/work/example-cwd"}) + "\n")
        ident, raws = leakcheck.corpus(d)
        self.assertEqual(ident, {"-proj-dirname", "session-1234", "/work/example-cwd"})
        text = os.path.join(os.path.dirname(d), "r.md")
        open(text, "w").write("参照 -proj-dirname を含む\n")
        self.assertIn("log-identifier", {h[1] for h in leakcheck.check([text], logs=d)})


if __name__ == "__main__":
    unittest.main()
