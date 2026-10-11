import json, os, subprocess, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "tokdiet", "scripts"))
import tokdiet

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "skills", "tokdiet", "scripts", "tokdiet.py")


def req(model):
    return dict(model=model, inp=1_000_000, out=1_000_000, cr=1_000_000, cw=1_000_000, cw1h=0)


class PricesTest(unittest.TestCase):
    def tearDown(self):
        tokdiet.PRICES.clear()

    def write(self, obj):
        p = os.path.join(tempfile.mkdtemp(), "prices.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        return p

    def test_no_prices_means_unpriced(self):
        self.assertEqual(tokdiet.PRICES, {})
        self.assertIsNone(tokdiet.cost_of(req("claude-opus-5-5")))

    def test_priced_by_model_id_not_family(self):
        tokdiet.load_prices(self.write({"claude-sonnet-5-5": {"input": 2, "output": 10, "cache_read": 0.2}}))
        self.assertAlmostEqual(tokdiet.cost_of(req("claude-sonnet-5-5")), 2 + 10 + 0.2 + 2.5)
        self.assertIsNone(tokdiet.cost_of(req("claude-sonnet-4-6")))     # 同じ系列でも世代違いは単価不明
        self.assertIsNone(tokdiet.cost_of(req("claude-haiku-5-5")))

    def test_cache_read_defaults_to_tenth_and_date_suffix(self):
        tokdiet.load_prices(self.write({"claude-opus-4-6": {"input": 5, "output": 25}}))
        self.assertAlmostEqual(tokdiet.cost_of(req("claude-opus-4-6-20260101")), 5 + 25 + 0.5 + 6.25)

    def test_cache_read_override(self):
        tokdiet.load_prices(self.write({"claude-opus-5-5": {"input": 4, "output": 20, "cache_read": 0.2}}))
        self.assertAlmostEqual(tokdiet.cost_of(req("claude-opus-5-5")), 4 + 20 + 0.2 + 5)

    def test_invalid_file_exits(self):
        for bad in ({"m": {"input": "3", "output": 1}}, {"m": {"input": 1}}, {}, {"m": {"input": -1, "output": 1}},
                    {"m": {"input": 1, "output": 1, "cache_read": "x"}}):
            with self.assertRaises(SystemExit):
                tokdiet.load_prices(self.write(bad))

    def test_no_bundled_price_table(self):
        src = open(SCRIPT, encoding="utf-8").read()
        self.assertIn("PRICES = {}", src)


if __name__ == "__main__":
    unittest.main()
