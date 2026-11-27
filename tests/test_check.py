import io
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path

from helpers import FIXTURES, ROOT, SCRIPTS, example_blocks, load

check = load("check")

# 既知のファイルに対する件数。規則を変えて件数が動いたら、意図した変化かを確かめてから更新する。
EXPECTED = {
    "smelly.md": 14,
    "natural.md": 0,
    "deck.pptx": 19,
    "nums.md": 0,
    "nums-plain.md": 0,
    "before.md": 4,
    "context.md": 5,
    "cp932.txt": 1,
    "geometry.pptx": 3,
    "headings.md": 3,
    "headings.pptx": 3,
    "one-line.txt": 1,
    "order.pptx": 1,
    "three-lines.txt": 3,
    "w.md": 0,
    "counts.pptx": 0,
    "fences.md": 1,
    "neon.md": 5,
    "neon2.md": 3,
    "rules.md": 2,
    "shapes.pptx": 2,
    "taigen.md": 1,
    "u0.pptx": 1,
    "units.pptx": 1,
    "bad_mail.txt": 14,
    "ok_mail.txt": 0,
}


def run(name, script=False):
    path = FIXTURES / name
    return check.read_pptx(path) if path.suffix == ".pptx" else check.read_text(path, script)


def run_text(text, script=False):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.txt"
        p.write_text(text, encoding="utf-8")
        return check.read_text(p, script)


class Counts(unittest.TestCase):
    def test_expected_counts(self):
        for name, n in EXPECTED.items():
            with self.subTest(name=name):
                self.assertEqual(len(run(name)), n)

    def test_unreadable_files(self):
        out = subprocess.run([sys.executable, "-I", str(SCRIPTS / "check.py"), str(FIXTURES / "bad.pptx"), str(FIXTURES / "non-deck.pptx")],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 2)
        self.assertIn("BadZipFile", out.stderr)
        self.assertIn("pptx の構成ではありません", out.stderr)


class Patterns(unittest.TestCase):
    def test_slide_order_uses_presentation(self):
        self.assertEqual([x["where"] for x in run("order.pptx")], ["スライド2"])

    def test_mail_patterns(self):
        kinds = Counter(x["pattern"] for x in run("bad_mail.txt"))
        for k in ["件名が長い、【】を重ねる", "穴埋めの記号が残る", "二重敬語と重ねすぎ", "書き手に向けた締めが残る",
                  "「させていただきます」の連続", "「〜となっております」の連続", "毎回の「お気軽に」", "全項目にアイコンや絵文字を付ける"]:
            self.assertIn(k, kinds)

    def test_standard_business_phrases_are_kept(self):
        text = "お世話になっております。\nご確認のほど、よろしくお願いいたします。\n承知しました。明日お送りします。\n料金は月額5万円となっております。\n"
        self.assertEqual(run_text(text), [])

    def test_links_and_checkboxes_are_not_placeholders(self):
        self.assertEqual(run_text("詳細は[会社案内](https://example.com)にあります。\n- [ ] 送付済み\n"), [])

    def test_script_mode(self):
        text = "多くの営業組織では、議事録作成や提案資料の準備といった付帯業務に多くの時間を要しており、本来注力すべき顧客との対話に十分な時間を割けていません。\nお客様：「まさにその通りです」\n貴社の課題を伺います。\n"
        kinds = Counter(x["pattern"] for x in run_text(text, script=True))
        self.assertEqual(kinds["1文が長い"], 1)
        self.assertEqual(kinds["相手が都合よく納得する"], 1)
        self.assertEqual(kinds["書き言葉のまま"], 1)
        self.assertNotIn("1文が長い", Counter(x["pattern"] for x in run_text(text)))

    def test_number_reports_are_not_taigen(self):
        self.assertEqual(run_text("受注は12件。\n売上は1200万円。\n解約は三件。\n"), [])


class Docs(unittest.TestCase):
    def test_examples_after_blocks_are_clean(self):
        """examples.md の「直した後」と「聞けた場合」の例は、検出が0件であること（スライドの免責注釈は意図して残す）。"""
        checked = 0
        for section, label, body in example_blocks():
            if label.startswith("直す前"):
                continue
            script = section == "トークスクリプト"
            found = [x for x in run_text(body, script) if x["pattern"] != "長い免責注釈"]
            with self.subTest(section=section, label=label):
                self.assertEqual(found, [])
            checked += 1
        self.assertGreaterEqual(checked, 5)

    def test_readme_diff_sample_matches(self):
        diff = load("diff")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        sample = readme.split("$ python3 skills/akanuke/scripts/diff.py --year 2026 before.txt after.txt\n")[1].split("```")[0].splitlines()
        buf = io.StringIO()
        with redirect_stdout(buf):
            diff.main(["--year", "2026", str(FIXTURES / "diff_before.txt"), str(FIXTURES / "diff_after.txt")])
        actual = [line.replace(str(FIXTURES / "diff_before.txt"), "before.txt").replace(str(FIXTURES / "diff_after.txt"), "after.txt")
                  for line in buf.getvalue().splitlines()]
        self.assertEqual(sample, actual[: len(sample)])

    def test_pocket_fits_custom_instructions(self):
        import re
        text = (ROOT / "pocket" / "akanuke-pocket.md").read_text(encoding="utf-8")
        body = re.search(r"```\n(.*?)```", text, re.S).group(1)
        self.assertLessEqual(len(body), 1500)

    def test_readme_sample_output_matches(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        sample = readme.split("$ python3 skills/akanuke/scripts/check.py deck.pptx\n")[1].split("（以下")[0].splitlines()
        buf = io.StringIO()
        with redirect_stdout(buf):
            check.main([str(FIXTURES / "deck.pptx")])
        actual = [line.replace(str(FIXTURES / "deck.pptx"), "deck.pptx") for line in buf.getvalue().splitlines()]
        self.assertEqual(sample, actual[: len(sample)])


if __name__ == "__main__":
    unittest.main()
