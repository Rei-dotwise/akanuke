import tempfile
import unittest
from pathlib import Path

from helpers import example_blocks, load

diff = load("diff")


def kinds(src, dst, year=2026):
    return {r["kind"] for r in diff.compare(src, dst, year) if not r.get("info")}


class Diff(unittest.TestCase):
    def test_same_text_has_no_risk(self):
        t = "12月1日（火）10:00から、約30%の削減を目標にご提案します。上記以外の日でも構いません。"
        self.assertEqual(diff.compare(t, t, 2026), [])

    def test_condition_word_swap(self):
        a = "上記以外でご都合のよい日時があれば、お知らせください。"
        b = "いずれも難しい場合は、ご都合のよい日時をお知らせください。"
        self.assertIn("条件・範囲", kinds(a, b))

    def test_weekday(self):
        self.assertIn("曜日", kinds("12月2日（水）", "12月2日（木）"))
        self.assertEqual(kinds("12月2日（水）", "12月2日（水）10:00から"), {"時刻"})

    def test_numbers_and_approx(self):
        self.assertIn("数値の幅", kinds("生産性を約30%高める", "生産性を30%高める"))
        self.assertIn("数値", kinds("生産性を約30%高める", "生産性を約20%高める"))
        self.assertEqual(kinds("01 現状", "1 現状"), set())

    def test_stance(self):
        self.assertIn("見込み・推量", kinds("効率化の可能性をご提案します", "効率化の方法をご提案します"))
        self.assertIn("決定", kinds("対象範囲は未定です", "対象範囲は全社に決定しました"))
        self.assertIn("否定", kinds("機密情報はAIに入力しない", "機密情報はAIに入力する"))
        self.assertEqual(kinds("お気軽にお申し付けください", "お知らせください"), set())
        self.assertIn("依頼", kinds("資料をお送りください。\nよろしくお願いいたします。", "資料をお送りしました。\nよろしくお願いいたします。"))
        self.assertNotIn("時刻", kinds("所要は2時間です。", "所要は2時間です。会議は10時から。") - {"時刻"} | (kinds("所要は2時間です。", "所要は120分です。") & {"時刻"}))

    def test_examples_keep_meaning(self):
        """examples.md の「元の内容だけで直した後」は、直す前と比べて数値・日付・曜日・時刻が変わっていないこと。"""
        blocks = example_blocks()
        pairs = 0
        for i, (section, label, body) in enumerate(blocks):
            if not label.startswith("直す前"):
                continue
            after = next(b for s, l, b in blocks[i + 1:] if l.startswith("元の内容だけで直した後") or l.startswith("直した後"))
            risky = {r["kind"] for r in diff.compare(body, after, 2026) if not r.get("info")}
            with self.subTest(section=section):
                self.assertFalse(risky & {"数値の幅", "日付", "曜日", "時刻"})
            pairs += 1
        self.assertGreaterEqual(pairs, 3)

    def test_cli_exit_code(self):
        import subprocess, sys
        from helpers import SCRIPTS
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d) / "a.txt", Path(d) / "b.txt"
            a.write_text("約30%を目標にします。", encoding="utf-8")
            b.write_text("30%を目標にします。", encoding="utf-8")
            r = subprocess.run([sys.executable, "-I", str(SCRIPTS / "diff.py"), "--fail-on-risk", str(a), str(b)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 1)
            r = subprocess.run([sys.executable, "-I", str(SCRIPTS / "diff.py"), str(a), str(b)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
