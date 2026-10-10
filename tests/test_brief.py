import os, stat, sys, tempfile, unittest, unittest.mock, json
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'skills', 'tokdiet', 'scripts')); sys.path.insert(0, HERE)
import tokdiet, actionbrief, synth


class B(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.root = synth.build(os.path.join(cls.tmp, "projects"))
        cls.out = os.path.join(cls.tmp, "out")
        cls.snap = os.path.join(cls.tmp, "base.json")
        with unittest.mock.patch.object(actionbrief, "MIN_SESSIONS", 1):      # 合成ログは1セッションなので閾値を下げて本文経路を検査
            tokdiet.run(cls.root, cls.out, brief=True, snapshot=cls.snap)
        out_d = os.path.join(cls.tmp, "out_default")
        tokdiet.run(cls.root, out_d, brief=True)                                # 既定の閾値（5）ではデータ不足になる
        cls.text_default = open(os.path.join(out_d, "action-brief.md"), encoding="utf-8").read()
        cls.A_ = tokdiet.analyze(tokdiet.load(cls.root))
        cls.path = os.path.join(cls.out, "action-brief.md")
        cls.text = open(cls.path, encoding="utf-8").read()

    def test_mask_secrets(self):
        for s in (synth.SECRET_KEY, synth.SECRET_GH, synth.SECRET_EMAIL):
            self.assertNotIn(s, self.text)
        self.assertIn("MASKED", actionbrief.mask("password=hunter2hunter2"))
        self.assertIn("[JWT]", actionbrief.mask("eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0NTY.SflKxwRJSMeKKF2QT4"))

    def test_mode_0600_and_local_only_header(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.snap).st_mode), 0o600)
        self.assertIn("LOCAL ONLY", self.text.split("\n", 3)[2])
        self.assertIn("各項目の根拠を再確認してから", self.text)

    def test_per_file_and_per_name(self):
        self.assertIn(synth.INSTR_PATH, self.text)               # 指示ファイルのパス
        self.assertIn("Big Section", self.text)                   # 見出し別
        self.assertRegex(self.text, r"\| unused-skill \| [^|]+\| [^|]+\| 10 \| — \| 0回")   # 0回スキル
        self.assertRegex(self.text, r"\| used-skill \| [^|]+\| [^|]+\| 4 \| — \| 1 \|")
        self.assertIn("my-agent", self.text)                      # meta.json の agentType

    def test_no_partial_secret_in_verify_cmd(self):
        self.assertNotIn("sk-ant", self.text)
        self.assertNotIn("ghp_", self.text)

    # ---- M1
    def test_A_skill_calls_not_doubled(self):
        import subprocess
        # tool_use が wireToolInputs に二重表現されても、一意なtool_use idで1回と数える
        L = tokdiet.load(self.root)
        self.assertEqual(sum(1 for t in L["tools"].values() if t.get("skill_name") == "used-skill"), 1)
        raw = subprocess.run("grep -rhoF '\"skill\":\"used-skill\"' %s --include='*.jsonl' | wc -l" % self.root, shell=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(raw, "2")                                   # 素朴なgrepは2倍に数える（誤差の出所）
        cmd = actionbrief.v_tooluse("Skill", "skill", "used-skill").replace("~/.claude/projects", self.root).split("   #")[0]
        self.assertEqual(subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip(), "1")   # 提示コマンドは一意数
        self.assertIn("sort -u", self.text)

    def test_B_C_listing_parse(self):
        d = tokdiet.parse_skill_listing("- a: xxx\n- plug:b: yy\n- c: z\n", {"a", "plug:b", "c"})
        self.assertEqual(d, {"a": 3, "plug:b": 2, "c": 1})          # 名前に':'を含む行で前の説明を巻き込まない
        L = tokdiet.load(self.root)
        C = actionbrief.collect(L, self.A_)
        self.assertEqual(C["skills"]["parsed"], 4)
        self.assertEqual(C["skills"]["desc"]["big-skill"], 500)
        self.assertEqual(C["skills"]["logged_mode"], 4)

    def test_D_origin_and_refs(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, ".git")); os.makedirs(os.path.join(d, ".github", "workflows"))
        os.makedirs(os.path.join(d, ".claude", "skills", "foo"))
        open(os.path.join(d, ".claude", "skills", "foo", "SKILL.md"), "w").write("---\nname: foo\ndescription: hello world\n---\nbody foo\n")
        open(os.path.join(d, ".github", "workflows", "ci.yml"), "w").write("run: ./check bar-skill\n")
        o = actionbrief.skill_origins(["foo", "plug:x", "zzz-unknown"], [d])
        self.assertEqual(o["foo"]["origin"], "project"); self.assertEqual(o["foo"]["file_desc"], len("hello world"))
        self.assertEqual(o["plug:x"]["origin"], "plugin"); self.assertEqual(o["zzz-unknown"]["origin"], "不明")
        r, roots = actionbrief.skill_refs(["foo", "bar-skill"], [d], {"foo": o["foo"]["file"]})
        self.assertEqual(r["foo"]["files"], 0)                       # 自スキルのディレクトリ内は除外
        self.assertEqual(r["bar-skill"]["files"], 1); self.assertEqual(r["bar-skill"]["ci"], 1)

    def test_E_reread_attributed_to_agent_type(self):
        self.assertRegex(self.text, r"guide\.md（現在は存在しない） \| 参照資料 \| 2 \| 2 \| 0 \| 1 \| 0 \| my-agent\[範囲あり0／全文2\]")

    def test_F_no_generic_templates(self):
        for s in ("要点を写", "CLAUDE.md かメモ", "更新履歴・経緯・長い例は削る"):
            self.assertNotIn(s, self.text)
        a = actionbrief.analyze_text("# T\nbody\n## 変更履歴\n" + "x" * 100 + "\n## 手順\nok\n")
        self.assertTrue(any("履歴" in c for c, _ in actionbrief.plan_text_file(a, "f")))
        a2 = actionbrief.analyze_text("# T\nbody\n## 手順\n" + "y" * 100 + "\n## 注意\nok\n")
        pl = actionbrief.plan_text_file(a2, "f")
        self.assertEqual(pl, [])                                   # 履歴系も重複も無ければ提案は出さない（事実表示のみ）
        self.assertFalse(actionbrief.heavy_section(a2))
        para = "同じ段落の本文。" * 15
        a3 = actionbrief.analyze_text("# T\n\n" + para + "\n\n" + para + "\n")
        self.assertGreater(a3["dup_chars"], 0)

    def test_M11_range_breakdown_and_suppression(self):
        L = tokdiet.load(self.root); C = actionbrief.collect(L, self.A_)
        f = {x["path"]: x for x in C["files"]}
        self.assertEqual(f[synth.FULL_PATH]["readers"]["my-agent"], {"ranged": 0, "full": 4, "big": 0, "re": 3, "re_full": 3})
        self.assertEqual(f[synth.RANGED_PATH]["readers"]["ranged-agent"]["ranged"], 4)
        self.assertEqual(f[synth.RANGED_PATH]["full"], 0)
        # 既に範囲指定読みが中心 -> 節指定読みの提案を出さない
        fake = (os.path.join(self.tmp, "my-agent.md"), 100)
        with unittest.mock.patch.object(actionbrief, "find_agent_def", return_value=fake):
            C = actionbrief.collect(L, self.A_)
            for f_ in C["files"]:
                f_["gone"] = False                                   # 合成パスは実在しないので存在確認を無効化
            I = actionbrief.items(C)
        by = {i["target"]: i for i in I}
        self.assertIn(actionbrief.disp(synth.FULL_PATH), by)             # 全文読み4回(>=3) -> 提案あり
        self.assertIn("節だけ", by[actionbrief.disp(synth.FULL_PATH)]["plan"][0][1])
        self.assertNotIn(actionbrief.disp(synth.RANGED_PATH), by)         # 範囲指定のみ -> 項目自体を出さない
        self.assertIn("範囲あり4／全文0", self.text)

    def test_M11_generic_skill_refs_strict(self):
        d = tempfile.mkdtemp(); os.makedirs(os.path.join(d, ".git"))
        open(os.path.join(d, "a.md"), "w").write("please run the tests and loop over items. Use /simplify then `schedule` and my-skill.\n")
        r, _ = actionbrief.skill_refs(["run", "loop", "simplify", "schedule", "my-skill"], [d], {})
        self.assertEqual((r["run"]["files"], r["run"]["generic"]), (0, True))      # 裸の語は数えない
        self.assertEqual(r["loop"]["files"], 0)
        self.assertEqual(r["simplify"]["files"], 1)                               # /simplify
        self.assertEqual(r["schedule"]["files"], 1)                               # `schedule`
        self.assertEqual((r["my-skill"]["files"], r["my-skill"]["generic"]), (1, False))

    def test_M11_distributed_origin(self):
        home = tempfile.mkdtemp()
        for n in ("docs", "mine"):
            os.makedirs(os.path.join(home, ".claude", "skills", n))
            open(os.path.join(home, ".claude", "skills", n, "SKILL.md"), "w").write("---\nname: x\ndescription: d\n---\n")
        with unittest.mock.patch.object(actionbrief, "HOME", home):
            o = actionbrief.skill_origins(["anthropic-skills:docs", "mine", "zz"], [])
        self.assertEqual(o["anthropic-skills:docs"]["origin"], "配布")
        self.assertEqual(o["mine"]["origin"], "user")
        self.assertEqual(o["zz"]["origin"], "不明")

    def test_M11_model_diff_and_launcher_and_na(self):
        d = os.path.join(self.tmp, "agdef.md")
        open(d, "w").write("---\nname: my-agent\nmodel: opus\n---\nbody\n")
        L = tokdiet.load(self.root)
        with unittest.mock.patch.object(actionbrief, "find_agent_def", return_value=(d, 40)):
            C = actionbrief.collect(L, self.A_)
            txt = actionbrief.render(L, self.A_, C, actionbrief.items(C))
        self.assertIn("定義 `model: opus`／実使用 claude-sonnet-5×1", txt)
        self.assertNotIn("意図した使い分け", txt)
        self.assertRegex(self.text, r"\| my-agent \| 1 \| /publish-all×1 \|")      # 種別×起動元
        self.assertIn("モデル差分", self.text); self.assertIn("該当なし（定義に model 指定があり", self.text)
        self.assertIn("該当なし（5 回以上", self.text)                               # Bash 閾値未満
        self.assertIn("該当なし（結果の平均が", self.text)                           # MCP

    def test_M12_insufficient_data(self):
        self.assertIn("データ不足: 判断に足りない", self.text_default)
        self.assertNotIn("優先アクション上位", self.text_default)
        self.assertIn("分母: ログファイル3件中、利用量を数えられたファイル3件", self.text_default)   # 分母（数えなかった分も明示）
        self.assertIn("優先アクション上位", self.text)               # 閾値を満たせば出る

    def test_sections(self):
        sec = dict(actionbrief.sections(synth.INSTR_BODY))
        self.assertGreater(sec["Big Section"], 3000)

    def test_no_percent_effect_claims(self):
        self.assertNotIn("削減見込み%", self.text)

    def test_brief_not_in_anonymous_report(self):
        for fn in ("report.md", "report.html"):
            self.assertNotIn("secretuser", open(os.path.join(self.out, fn), encoding="utf-8").read())

    def test_compare(self):
        out2 = os.path.join(self.tmp, "out2")
        tokdiet.run(self.root, out2, brief=True, compare=self.snap)
        self.assertIn("前後比較", open(os.path.join(out2, "action-brief.md"), encoding="utf-8").read())


if __name__ == "__main__":
    unittest.main()


class M12(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import synth2
        cls.tmp = tempfile.mkdtemp()
        cls.root, cls.repo, cls.out_path, cls.ref_path = synth2.build(cls.tmp)
        cls.L = tokdiet.load(cls.root)
        cls.A = tokdiet.analyze(cls.L)
        cls.C = actionbrief.collect(cls.L, cls.A)
        cls.s = cls.C["skills"]

    def test_listing_main_sessions_only_and_latest(self):
        self.assertNotIn("sub-only-skill", self.s["desc"])               # サブエージェントの一覧は除外
        self.assertEqual(self.s["logged_mode"] in (5, 4), True); self.assertNotEqual(self.s["logged_mode"], 99)
        self.assertEqual(self.s["latest_logged"], 4)                      # 最新セッションの値を併記
        self.assertEqual(self.s["cur"], {"new-article", "wp-media", "wp:media", "shared"})

    def test_stale_names_excluded_from_plans(self):
        I = actionbrief.items(self.C)
        txt = " ".join(c + p for i in I for c, p in i["plan"])
        self.assertNotIn("old-skill", txt)   # 現在の一覧にない名前は提案から外す
        with unittest.mock.patch.object(actionbrief, "MIN_SESSIONS", 1):
            out = actionbrief.render(self.L, self.A, self.C, I)
        self.assertRegex(out, r"old-skill [^\n]*現在の一覧にない")

    def test_third_party_excluded_and_workflow_relation(self):
        r = self.s["refs"]
        self.assertEqual(r["old-skill"]["files"], 0)                      # themes/ 配下の言及は数えない
        self.assertTrue(any(p.endswith("new-article.md") for p in r["wp-media"]["rel"]))   # コマンド本文からの言及
        I = actionbrief.items(self.C)
        txt = " ".join(c for i in I for c, p in i["plan"])
        self.assertNotIn("wp-media", txt.split("退避注意")[0] if "退避注意" in txt else txt)

    def test_commands_as_project_and_dups(self):
        self.assertEqual(self.s["origins"]["new-article"]["origin"], "project")
        self.assertEqual(self.s["origins"]["new-article"]["file"].endswith("new-article.md"), True)
        self.assertIn(["wp-media", "wp:media"], [sorted(g) for g in actionbrief.dup_groups(list(self.s["desc"]))])
        self.assertTrue(any("wp-media" in g and "wp:media" in g for g in self.s["dups"]))

    def test_mcp_depends_on_instructions(self):
        self.assertEqual(self.C["mcp_depends"], ["claude_ai_google_drive"])   # CLAUDE.md に記述 -> 提案から除外
        self.assertEqual(self.C["mcp_unused"], ["other_srv"])
        I = actionbrief.items(self.C)
        mcp_item = [i for i in I if i["cat"] == "mcpdef"][0]
        self.assertNotIn("google_drive", " ".join(c for c, _ in mcp_item["plan"]))
        self.assertIn("/mcp", mcp_item["plan"][0][1])                         # 公式手順（/mcp パネル）

    def test_edited_artifact_vs_reference(self):
        f = {x["path"]: x for x in self.C["files"]}
        self.assertTrue(f[self.out_path]["edited"]); self.assertFalse(f[self.ref_path]["edited"])
        I = {i["target"]: i for i in actionbrief.build_items(self.C) if i["cat"] == "file"}
        self.assertTrue(I[actionbrief.disp(self.ref_path)]["risk"].startswith("クイックウィン"))   # 参照資料
        self.assertTrue(I[actionbrief.disp(self.out_path)]["risk"].startswith("要検証"))           # 成果物（Write された形跡）

    def test_all_calls_and_table_top(self):
        self.C["skills"]["all_calls"] = {"new-article": 1}
        with unittest.mock.patch.object(actionbrief, "MIN_SESSIONS", 1), unittest.mock.patch.object(actionbrief, "TABLE_TOP", 2):
            out = actionbrief.render(self.L, self.A, self.C, actionbrief.items(self.C))
        self.assertIn("0回（全ログ1回）", out)
        self.assertIn("残り", out); self.assertIn("baseline.json", out)

    def test_official_howto_recorded(self):
        self.assertIn("skillOverrides", actionbrief.disable_howto("project"))
        self.assertIn("claude plugin disable", actionbrief.disable_howto("plugin"))
        self.assertIn("公式未確認・手順不明", actionbrief.disable_howto("plugin"))   # プラグイン内の個別スキル
        self.assertIn("公式未確認・手順不明", actionbrief.disable_howto("配布"))
        self.assertIn("code.claude.com/docs", actionbrief.disable_howto("project"))
