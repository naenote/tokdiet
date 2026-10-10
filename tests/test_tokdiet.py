import os, re, subprocess, sys, tempfile, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
PROTO = os.path.dirname(HERE)
sys.path.insert(0, PROTO); sys.path.insert(0, HERE)
import tokdiet, leakcheck, synth


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.root = synth.build(os.path.join(cls.tmp, "projects"))
        cls.out = os.path.join(cls.tmp, "out")
        cls.md, cls.html, cls.A = tokdiet.run(cls.root, cls.out)
        cls.text = open(cls.md, encoding="utf-8").read()

    def test_dedup_requestid(self):
        A = self.A
        self.assertEqual(A["requests"], 7)                       # r1(2行)+r2,r2b,r2c,r2d+サブエージェントr3
        self.assertEqual(A["tok"]["out"], 80 + 20 + 7 + 3 * 50 + 1)           # r1は大きい方(80)を1回だけ
        self.assertEqual(A["tok"]["cr"], 1000 + 2000 + 100 + 3 * 1000 + 10)

    def test_subagent_and_limits_and_reread(self):
        A = self.A
        self.assertAlmostEqual(A["side_req_share"], 28.6, places=1)
        self.assertEqual(A["limits"]["events"], 1)               # 上限到達はsyntheticモデル行から取る
        self.assertEqual(A["reread"]["rereads"], 8)   # 本体1 + サブエージェントの同一ファイル2回目1
        self.assertEqual(A["reread"]["reads"], 12)

    def test_anonymized_and_no_content(self):
        for s in ("secretuser", "acme", "secretrepo", "secret_module", "SECRET_PROMPT", "sess-0001", "/home/"):
            for p in (self.md, self.html):
                self.assertNotIn(s, open(p, encoding="utf-8").read(), s)
        self.assertIn("P1", self.text)

    def test_leakcheck_clean_passes(self):
        self.assertEqual(leakcheck.check([self.md, self.html], logs=self.root), [])

    def _inject(self, extra):
        p = os.path.join(self.tmp, "bad.md")
        open(p, "w", encoding="utf-8").write(self.text + "\n" + extra + "\n")
        return leakcheck.check([p], logs=self.root)

    def test_leakcheck_detects(self):
        kinds = lambda hs: {h[1] for h in hs}
        self.assertIn("abs-path", kinds(self._inject("参照: /home/secretuser/repos/x.py")))
        self.assertIn("home", kinds(self._inject("path " + os.path.expanduser("~") + "/a")))
        self.assertIn("email", kinds(self._inject("連絡 someone@example.com")))
        self.assertIn("url", kinds(self._inject("https://example.com/a")))
        self.assertIn("log-substring", kinds(self._inject("引用 " + synth.SECRET_PROMPT[:40])))
        self.assertIn("log-identifier", kinds(self._inject("repo " + synth.SECRET_DIR)))

    def test_leakcheck_cli_exit_code(self):
        bad = os.path.join(self.tmp, "bad2.md"); open(bad, "w").write("see /Users/bob/work/proj\n")
        r = subprocess.run([sys.executable, os.path.join(PROTO, "leakcheck.py"), bad], capture_output=True)
        self.assertEqual(r.returncode, 1)
        r = subprocess.run([sys.executable, os.path.join(PROTO, "leakcheck.py"), self.md, "--logs", self.root], capture_output=True)
        self.assertEqual(r.returncode, 0)

    def test_no_network_code(self):
        src = open(os.path.join(PROTO, "tokdiet.py"), encoding="utf-8").read()
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        for w in ("socket", "urllib", "http.client", "requests", "subprocess", "urlopen"):
            self.assertIsNone(re.search(r"\b(import|from)\s+%s\b|\b%s\." % (w, w), code), w)


if __name__ == "__main__":
    unittest.main()
