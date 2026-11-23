#!/usr/bin/env python3
"""akanuke の機械検出。.md / .txt / .pptx を読み、AIっぽい型の候補を行番号やスライド番号付きで挙げる。

標準ライブラリだけで動く。検出は候補の提示で、直すかどうかは文脈で判断する。
使い方: python3 check.py [--json] <file> [<file> ...]
"""
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
}

# (パターン名, 正規表現, 説明)。パターン名は references の見出しと揃える。
PHRASE_RULES = [
    ("「〜のは、〜。」の溜め", re.compile(r"[^。\n「」]{1,20}のは、"), "主語を後ろへ回して溜めを作っている"),
    ("「〜ではなく〜」", re.compile(r"ではなく|じゃなくて"), "誰も主張していない誤解を否定していないか"),
    ("薬のような動詞", re.compile(r"効く|効いて|効か[なせ]|効きま|刺さる|刺さり|刺さっ|爆上が|劇的に|激変"), "何が起きるのかを書いていない"),
    ("空疎な語", re.compile(r"シームレス|最適化|最大化|包括的|戦略的|ポテンシャル|可能性を引き出|新たな価値|革新的|次のレベル|本質的|鍵となる|カギとなる|不可欠|伴走者|変革"), "どの資料に貼っても通じる語"),
    ("共感を煽る導入", re.compile(r"悩んでいませんか|という方も多いのでは|ありませんか？|実は、"), "誰にでも当てはまる問いかけ"),
    ("「結論から言うと」", re.compile(r"結論から言うと|結論から言えば|結論から申し上げ"), "宣言自体が前置きになっている"),
    ("数の宣言", re.compile(r"ポイントは[0-9０-９一二三四五六七八九十]+つ|[0-9０-９一二三四五六七八九十]+つのポイント|[0-9０-９]+つのステップで"), "数を先に宣言している"),
    ("「いかがでしたか」型の締め", re.compile(r"いかがでしたか|ぜひ試してみてください|参考になれば幸い"), "定型の締め"),
    ("投稿の型", re.compile(r"保存推奨|知らない人多い|な話。|という件。|皆さんはどう"), "SNSの定型"),
    ("返信が「要約、評価、質問」になる", re.compile(r"素晴らしい(?:視点|取り組み|考察)|参考になりました|どのように(?:され|して)いますか"), "定型の評価や質問"),
    ("教授調・説教調", re.compile(r"ことが(?:重要|大切|大事)です|しましょう|すべきです|すべきだ"), "上の位置から教えている"),
    ("予防線", re.compile(r"と言えるでしょう|かもしれません|一概には言えません|状況によって異なり"), "断定を避ける前置き"),
    ("翻訳調", re.compile(r"することができ|を行う|を行い|において|に対して"), "直訳調の硬さ"),
    ("ダッシュと括弧の多用", re.compile(r"——|――|—"), "ダッシュで文をつないでいる"),
    ("長い免責注釈", re.compile(r"保証するものではありません|正確性・完全性|将来の成果"), "誰も読まない免責文"),
    ("01・02・03の番号と仕切り線", re.compile(r"(?:^|\s|[|｜])0[1-9](?=\s|$|[.．|｜:：])"), "十の位の0付き番号"),
]

DISCLAIMER = next(rx for kind, rx, _ in PHRASE_RULES if kind == "長い免責注釈")
COLON_LABEL = re.compile(r"^\s*(?:[-*#>]+\s*)?(?:(?:結論|ポイント|論点[0-9０-９]*|要点|まとめ|注意|基本方針)\s*[：:]|【[^】]{1,8}】)")
KAGI_SHORT = re.compile(r"「([^」\n]{1,8})」(?!(?:と(?:言|い|呼|書|聞|思)|って|という|とは|の意味|を(?:検索|入力)))")
SENT_END = re.compile(r"([^\s。！？!?]{1,2})[。！？!?]")
TAIGEN_TAIL = re.compile(r"[一-鿿ァ-ヶーA-Za-z0-9０-９）)]$")
BOLD = re.compile(r"\*\*[^*\n]+\*\*")
STRIP_MD = re.compile(r"`[^`]*`")


def finding(kind, loc, text, why, level="警告"):
    return {"pattern": kind, "where": loc, "text": text.strip()[:80], "why": why, "level": level}


def is_emphasis_kagi(line, m):
    """列挙の引用（「A」「B」）、記号入りの例、数字だけの引用は強調とみなさない。"""
    inner = m.group(1)
    if re.search(r"[〜~：:？?！!。/／・…]", inner) or re.fullmatch(r"[0-9０-９A-Za-z]+", inner):
        return False
    before = line[: m.start()].rstrip()
    after = line[m.end():]
    if before.endswith(("」", "『")) or re.match(r"\s*(?:「|、「|や「|と「|など|のよう|のほか|、[^。]{0,3}「)", after):
        return False
    return True


def is_table_rule(line):
    return bool(re.fullmatch(r"\s*\|?[\s:|-]+\|?\s*", line))


def check_lines(lines, label_fn):
    out = []
    taigen = []
    preachy = []
    for i, raw in enumerate(lines):
        if not raw.strip() or is_table_rule(raw):
            continue
        line = STRIP_MD.sub("", raw)
        loc = label_fn(i)
        for kind, rx, why in PHRASE_RULES:
            m = rx.search(line)
            if not m:
                continue
            if kind == "教授調・説教調":
                preachy.append((loc, line))
                continue
            level = "情報" if kind in ("翻訳調", "予防線") else "警告"
            out.append(finding(kind, loc, line, why, level))
        if COLON_LABEL.search(line):
            out.append(finding("コロンや【】で区切る見出し", loc, line, "ラベルで見出しを作っている"))
        for m in KAGI_SHORT.finditer(line):
            if not is_emphasis_kagi(line, m):
                continue
            out.append(finding("強調のためのカギカッコ", loc, line, f"「{m.group(1)}」は引用か定義か", "情報"))
        if len(BOLD.findall(raw)) >= 3:
            out.append(finding("太字の乱発", loc, line, "1行に太字が3か所以上"))
        for m in SENT_END.finditer(re.sub(r"「[^」]*」|『[^』]*』", "「」", line)):
            if m.group(0)[-1] == "。" and TAIGEN_TAIL.search(m.group(1)):
                taigen.append((loc, line))
    if len(preachy) >= 3:
        for loc, line in preachy:
            out.append(finding("教授調・説教調", loc, line, f"「〜しましょう」型が文書全体で{len(preachy)}回"))
    if len(taigen) >= 3:
        for loc, line in taigen:
            out.append(finding("体言止めで決める", loc, line, f"名詞＋「。」の言い切りが文書全体で{len(taigen)}か所"))
    return out


def read_text(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    in_code = False
    kept = []
    for line in lines:
        if line.strip().startswith("```"):
            in_code = not in_code
            kept.append("")
            continue
        kept.append("" if in_code else line)
    return check_lines(kept, lambda i: f"L{i + 1}")


def emu_box(sp):
    xfrm = sp.find(".//a:xfrm", NS)
    if xfrm is None:
        return None
    ext = xfrm.find("a:ext", NS)
    if ext is None:
        return None
    return int(ext.get("cx", 0)), int(ext.get("cy", 0))


def read_pptx(path):
    out = []
    with zipfile.ZipFile(path) as z:
        names = sorted(
            (n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
            key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)),
        )
        for name in names:
            no = int(re.search(r"slide(\d+)\.xml", name).group(1))
            loc = f"スライド{no}"
            root = ET.fromstring(z.read(name))
            paras = []
            small_long = []
            for p in root.iter(f"{{{NS['a']}}}p"):
                text = "".join(t.text or "" for t in p.iter(f"{{{NS['a']}}}t")).strip()
                if not text:
                    continue
                paras.append(text)
                sizes = [int(r.get("sz")) for r in p.iter(f"{{{NS['a']}}}rPr") if r.get("sz")]
                if sizes and max(sizes) <= 1100 and len(text) >= 80:
                    small_long.append(text)
            out += check_lines(paras, lambda i, loc=loc: loc)

            counts = Counter(t for t in paras if len(t) <= 15)
            for text, n in counts.items():
                if n >= 3:
                    out.append(finding("同じラベルを繰り返す", loc, text, f"同じラベルが{n}回"))
            for text in small_long:
                if DISCLAIMER.search(text):
                    continue  # 文言ルールで既に挙げた注釈は二重に数えない
                out.append(finding("長い免責注釈", loc, text, "小さい文字の長文注釈"))

            bars = lines = 0
            colors = set()
            for sp in root.iter(f"{{{NS['p']}}}sp"):
                box = emu_box(sp)
                has_text = any((t.text or "").strip() for t in sp.iter(f"{{{NS['a']}}}t"))
                if box and not has_text:
                    cx, cy = box
                    if cy and cx and cx / cy <= 0.12 and cx <= 120000:
                        bars += 1
                    elif cx and cy / max(cx, 1) <= 0.05:
                        lines += 1
                sppr = sp.find("p:spPr", NS)
                if sppr is not None:
                    for c in sppr.findall("a:solidFill/a:srgbClr", NS):
                        colors.add(c.get("val"))
            lines += sum(1 for _ in root.iter(f"{{{NS['p']}}}cxnSp"))
            if bars >= 2:
                out.append(finding("左の縦線（アクセントバー）", loc, f"細い縦長の図形 {bars}個", "見出しや箱の左に縦線を付けていないか"))
            if lines >= 2:
                out.append(finding("仕切り線で区切って関係をぼかす", loc, f"区切り線 {lines}本", "上下の要素の関係を線でごまかしていないか", "情報"))
            if len(colors) >= 4:
                out.append(finding("意味のない色分け", loc, f"塗り色 {len(colors)}種類", "色ごとの意味を凡例で説明できるか", "情報"))
            chars = sum(len(t) for t in paras)
            if chars >= 450 or len(paras) >= 25:
                out.append(finding("1枚に全部盛る", loc, f"{len(paras)}段落・{chars}字", "主張を一文で言えるか"))
    return out


def main(argv):
    as_json = "--json" in argv
    files = [a for a in argv if a != "--json"]
    if not files:
        print(__doc__.strip(), file=sys.stderr)
        return 1
    results = {}
    for f in files:
        path = Path(f)
        if not path.exists():
            print(f"ファイルがありません: {f}", file=sys.stderr)
            return 1
        results[f] = read_pptx(path) if path.suffix.lower() == ".pptx" else read_text(path)
    if as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    for f, items in results.items():
        print(f"=== {f}: {len(items)}件")
        by_kind = Counter(x["pattern"] for x in items)
        for kind, n in by_kind.most_common():
            print(f"  {kind}: {n}")
        seen = set()
        for x in items:
            key = (x["pattern"], x["where"], x["text"], x["why"])
            if key in seen:
                continue
            seen.add(key)
            print(f"[{x['level']}] {x['where']} {x['pattern']} … {x['why']}\n    {x['text']}")
    print("\n検出は候補。意味があって置いた型は残してよい。見た目（色・縦線・改行位置）は画像で目視確認する。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
