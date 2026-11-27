#!/usr/bin/env python3
"""akanuke の機械検出。.md / .txt / .pptx を読み、AIっぽい型の候補を行番号やスライド番号付きで挙げる。

標準ライブラリだけで動く。テキストは UTF-8（読めなければ CP932）で読む。
検出は候補の提示で、直すかどうかは文脈で判断する。
使い方: python3 check.py [--json] [--script] [--fail-on-warn] [--words <フォルダ>] <file> [<file> ...]
  --script: トークスクリプト・読み原稿として、1文の長さと書き言葉も数える（.md / .txt のみ）
  --fail-on-warn: 「警告」の候補が1件でもあれば終了コード1を返す（CI や Git のフック用）
  --words: 語の一覧のフォルダ。指定しなければ、作業フォルダから上へ .akanuke-words/ を探す
    allow.txt: 指摘しない語（製品名、社内の用語など）。1行に1語
    flag.txt: 必ず指摘する語。「使わない語<TAB>代わりの語」とも書ける
    どちらも # で始まる行と空の行は読み飛ばす
終了コード: 0 = 全ファイルを読めた / 1 = --fail-on-warn で警告が見つかった / 2 = 引数の誤りか、読めないファイルがあった（読めた分の結果は出力する）
"""
import json
import re
import sys
import unicodedata
import zipfile
import posixpath
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
EMU_PER_INCH = 914400

# 照合は NFKC で正規化した行に対して行う（全角数字・全角記号も同じ規則で拾うため）。
# mode: each = 1件ずつ挙げる / repeat = 文書全体で3回以上のときだけ挙げる / repeat2 = 2回以上のとき
RULES = [
    ("「〜のは、〜。」の溜め", r"[^。\n「」]{1,20}のは、", "主語を後ろへ回して溜めを作っていないか", "情報", "each"),
    ("「〜ではなく〜」", r"ではなく|じゃなくて", "誰も主張していない誤解を否定していないか", "警告", "each"),
    ("薬のような動詞", r"効く|効いて|効か[なせ]|効きま|刺さる|刺さり|刺さっ|爆上が|劇的[にな]|激変", "何が起きるのかを書いていない", "警告", "each"),
    ("空疎な語", r"シームレス|最適化|最大化|包括的|戦略的|ポテンシャル|可能性を引き出|新たな価値|革新的|次のレベル|本質的|鍵となる|カギとなる|不可欠|伴走者|変革", "どの資料に貼っても通じる語", "警告", "each"),
    ("「近年、〜が急速に」で始める", r"近年、.{0,30}急速に", "誰でも書ける背景説明から入っていないか", "警告", "each"),
    ("「〜につながります」の結び", r"につなが(?:り|る|っ)", "何がどう変わるのかを書いているか", "情報", "each"),
    ("「〜だけでなく〜も」の水増し", r"だけでなく", "本当に両方が主役か", "情報", "each"),
    ("機械的な「一方で」", r"一方で", "長所と短所を対で並べていないか", "情報", "repeat"),
    ("決め台詞で節を締める", r"(?:^[>\s]*|[。!?]\s*)(?:つまり|要するに)、", "直前の言い直しで締めていないか", "情報", "each"),
    ("「結論から言うと」", r"結論から言うと|結論から言えば", "宣言自体が前置きになっている", "警告", "each"),
    ("数の宣言", r"ポイントは[0-9一二三四五六七八九十]+つ|[0-9一二三四五六七八九十]+つのポイント|[0-9]+つのステップで", "数を先に宣言している", "警告", "each"),
    ("AIとのやり取りが残る", r"ご要望を踏まえ|以下のように(?:まとめ|整理し)ました|以下にまとめます|わかりやすく解説します|初心者にもわかりやすく|以下は.{0,20}(?:文案|案|例|ドラフト)です", "依頼への返事が本文に残っている", "警告", "each"),
    ("書き手に向けた締めが残る", r"お手伝いできることがあれば|必要に応じて(?:調整|修正|変更)してください|参考になれば幸い|ご参考になれば", "AIが書き手に向けて書いた締め", "警告", "each"),
    ("穴埋めの記号が残る", r"[〇○]{2}|XX(?:様|日|社)|[\[【][^\]】\n]{0,8}(?:名|日時|日付|会社|部署|担当|金額|URL)[\]】](?!\()", "埋めていない欄が残っていないか", "警告", "each"),
    ("件名が長い、【】を重ねる", r"【[^】\n]*】\s*【", "【】を重ねていないか", "警告", "each"),
    ("毎回の「お気軽に」", r"お気軽に", "定型として毎回付けていないか", "情報", "repeat2"),
    ("「させていただきます」の連続", r"させていただ", "「します」で足りないか", "情報", "repeat"),
    ("「〜となっております」の連続", r"となっております|となっています", "「です」で足りないか", "情報", "repeat"),
    ("二重敬語と重ねすぎ", r"拝見させていただ|おっしゃられ|お伺いさせていただ|ご(?:確認|検討)していただ", "敬語を重ねていないか", "警告", "each"),
    ("共感の定型を重ねる", r"(?:大変|とても)よく(?:分|わ)かります|お気持ち.{0,6}(?:分|わ)かります", "受けの定型を重ねていないか", "情報", "each"),
    ("出典のない総意", r"多くの(?:人|方|企業|営業担当者|ビジネスパーソン|担当者)が|誰もが", "観察した範囲を書けるか", "情報", "each"),
    ("根拠のない評価", r"(?:難易度|期待効果|効果|重要度|優先度|インパクト)\s*:\s*(?:高|中|低|大|小|★)", "評価の根拠を書けるか", "警告", "each"),
    ("矢印でつなぐ因果", r"→[^→\n]+→", "確認した因果だけをつないでいるか", "情報", "each"),
    ("教授調・説教調", r"ことが(?:重要|大切|大事)です|ましょう(?!か)|すべきです|すべきだ", "上の位置から教えている", "警告", "repeat"),
    ("予防線", r"と言えるでしょう|かもしれません|一概には言えません|状況によって異なり", "断定を避ける前置き", "情報", "each"),
    ("翻訳調", r"することができ|することが可能|を行う|を行い|において|に対して", "直訳調の硬さ", "情報", "each"),
    ("ダッシュと括弧の多用", r"——|――|—", "ダッシュで文をつないでいる", "警告", "each"),
    ("長い免責注釈", r"保証するものではありません|正確性・完全性|将来の成果", "定型の免責文", "警告", "each"),
    ("01・02・03の番号と仕切り線", r"(?:^|\s|\|)0[1-9](?=\s|$|[.|:])", "十の位の0付き番号", "警告", "each"),
]
REPEAT2 = {name for name, _, _, _, mode in RULES if mode == "repeat2"}
RULES = [(name, re.compile(rx), why, level, "repeat" if mode == "repeat2" else mode) for name, rx, why, level, mode in RULES]
DISCLAIMER = next(rx for name, rx, *_ in RULES if name == "長い免責注釈")

COLON_LABEL = re.compile(r"^\s*(?:[-*#>]+\s*)?(?:(?:結論|ポイント|論点[0-9]*|要点|まとめ|注意|基本方針)\s*:|【[^】]{1,8}】)")
KAGI_SHORT = re.compile(r"「([^」\n]{1,8})」(?!(?:と(?:言|い|呼|書|聞|思)|って|という|とは|の意味|を(?:検索|入力)))")
SENT_END = re.compile(r"([^\s。!?]{1,2})[。!?]")
TAIGEN_TAIL = re.compile(r"[一-鿿ァ-ヶーA-Za-z0-9]$")
EMOJI_LEAD = re.compile(r"^\s*(?:[-*+#>]+\s*)?[\u2600-\u27bf\U0001F300-\U0001FAFF]")
BOLD = re.compile(r"\*\*[^*\n]+\*\*")
BOLD_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\*\*")
STRIP_MD = re.compile(r"`[^`]*`")
NUMBER_REPORT = re.compile(r"(?:[0-9][0-9,.]*|[一二三四五六七八九十百千]+)[^\s0-9、。]{1,3}$")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")


WORDS = {"allow": [], "flag": [], "path": None}
ALLOW_MARK = "語"


def load_words(folder=None):
    """語の一覧を読む。folder がなければ作業フォルダから上へ .akanuke-words/ を探す。"""
    if folder is None:
        for d in [Path.cwd(), *Path.cwd().parents]:
            if (d / ".akanuke-words").is_dir():
                folder = d / ".akanuke-words"
                break
        else:
            return {"allow": [], "flag": [], "path": None}
    folder = Path(folder)
    if not folder.is_dir():
        raise FileNotFoundError(str(folder))

    def lines(name):
        f = folder / name
        if not f.is_file():
            return []
        rows = []
        for raw in f.read_text(encoding="utf-8-sig").splitlines():
            if raw.strip() and not raw.lstrip().startswith("#"):
                rows.append([unicodedata.normalize("NFKC", c).strip() for c in raw.split("\t", 1)])
        return rows

    allow = sorted({r[0] for r in lines("allow.txt") if r[0]}, key=len, reverse=True)
    flag = [(r[0], r[1] if len(r) > 1 else "") for r in lines("flag.txt") if r[0]]
    return {"allow": allow, "flag": flag, "path": str(folder)}


def finding(kind, loc, text, why, level="警告"):
    return {"pattern": kind, "where": loc, "text": text.strip()[:80], "why": why, "level": level}


def is_emphasis_kagi(line, m):
    """列挙の引用（「A」「B」）、記号入りの例、数字だけの引用は強調とみなさない。"""
    inner = m.group(1)
    if WORDS["allow"] and set(inner) == {ALLOW_MARK}:
        return False
    if re.search(r"[〜~:?!。/・….]", inner) or re.fullmatch(r"[0-9A-Za-z]+", inner):
        return False
    before = line[: m.start()].rstrip()
    after = line[m.end():]
    if before.endswith(("」", "『")) or re.match(r"\s*(?:「|、「|や「|と「|など|のよう|のほか|、[^。]{0,3}「)", after):
        return False
    return True


def check_lines(items, script=False):
    """items: [(場所, 行)] のリスト。反復の判定は items 全体（文書全体・デッキ全体）で行う。"""
    out = []
    repeats = {}
    taigen = []
    bold_run = []
    emoji_lines = []

    def flush_bold_run():
        if len(bold_run) >= 3:
            for loc, line in bold_run:
                out.append(finding("太字の乱発", loc, line, f"箇条書き{len(bold_run)}項目の先頭がすべて太字"))
        bold_run.clear()

    for loc, raw in items:
        if not raw.strip() or re.fullmatch(r"\s*\|?[\s:|-]+\|?\s*", raw):
            flush_bold_run()
            continue
        shown = STRIP_MD.sub("", raw)
        line = unicodedata.normalize("NFKC", shown)
        for word, alt in WORDS["flag"]:
            if word in line:
                why = f"「{word}」は一覧で使わないと決めた語" + (f"。代わりに「{alt}」" if alt else "")
                out.append(finding("一覧で使わないと決めた語", loc, shown, why))
        # 指摘しない語は、名詞のまま扱えるよう同じ長さの漢字で伏せてから照合する
        for word in WORDS["allow"]:
            line = line.replace(word, ALLOW_MARK * len(word))
        for name, rx, why, level, mode in RULES:
            hits = rx.findall(line)
            if not hits:
                continue
            if mode == "repeat":
                repeats.setdefault(name, (why, level, []))[2].extend([(loc, shown)] * len(hits))
            else:
                out.append(finding(name, loc, shown, why, level))
        if COLON_LABEL.search(line):
            out.append(finding("コロンや【】で区切る見出し", loc, shown, "ラベルで見出しを作っている"))
        for m in KAGI_SHORT.finditer(line):
            if is_emphasis_kagi(line, m):
                out.append(finding("強調のためのカギカッコ", loc, shown, f"「{m.group(1)}」は引用か定義か", "情報"))
        if EMOJI_LEAD.match(raw):
            emoji_lines.append((loc, shown))
        if len(BOLD.findall(raw)) >= 3:
            out.append(finding("太字の乱発", loc, shown, "1行に太字が3か所以上"))
        if BOLD_LIST_ITEM.match(raw):
            bold_run.append((loc, shown))
        else:
            flush_bold_run()
        masked = re.sub(r"「[^」]*」|『[^』]*』", "「」", line)
        for m in SENT_END.finditer(masked):
            # 「12件。」「1200万円。」「三件。」のような数値の報告は体言止めに数えない
            if m.group(0)[-1] == "。" and TAIGEN_TAIL.search(m.group(1)) and not NUMBER_REPORT.search(masked[: m.end() - 1]):
                taigen.append((loc, shown))
    flush_bold_run()

    for name, (why, level, hits) in repeats.items():
        if len(hits) >= (2 if name in REPEAT2 else 3):
            for loc, line in dict.fromkeys(hits):
                out.append(finding(name, loc, line, f"{why}（文書全体で{len(hits)}回）", level))
    if len(emoji_lines) >= 3:
        for loc, line in emoji_lines:
            out.append(finding("全項目にアイコンや絵文字を付ける", loc, line, f"行頭の絵文字が文書全体で{len(emoji_lines)}行"))
    if script:
        out.extend(check_script(items))
    if len(taigen) >= 3:
        for loc, line in dict.fromkeys(taigen):
            out.append(finding("体言止めで決める", loc, line, f"名詞＋「。」の言い切りが文書全体で{len(taigen)}か所"))
    return out


SPOKEN_STIFF = re.compile(r"であり、|および|ならびに|すなわち|(?<![上下平同高均対初中])等(?:の|を|が|で|、|。)|貴社")


CUSTOMER_LINE = re.compile(r"^(?:[-*>]\s*)?(?:お客様|お客さま|顧客|先方|相手|担当者)\s*[:「]")


def check_script(items):
    """トークスクリプト用。声に出すと長い文と、話し言葉にしない書き言葉を挙げる。"""
    out = []
    for loc, raw in items:
        line = unicodedata.normalize("NFKC", STRIP_MD.sub("", raw)).strip()
        if not line:
            continue
        for sent in re.split(r"(?<=[。!?])", line):
            body = re.sub(r"[（(][^）)]*[）)]", "", sent).strip()
            if len(body) > 60:
                out.append(finding("1文が長い", loc, sent, f"1文{len(body)}字。声に出して息が続くか", "情報"))
        if CUSTOMER_LINE.match(line):
            out.append(finding("相手が都合よく納得する", loc, raw, "相手がそう答えるとは限らない。問いと分岐にできるか", "情報"))
        for m in SPOKEN_STIFF.finditer(line):
            out.append(finding("書き言葉のまま", loc, raw, f"「{m.group(0).rstrip('、。のをがで')}」は話すときに言うか", "情報"))
    return out


def read_text(path, script=False):
    data = path.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp932")
    items = []
    in_code = None
    for i, line in enumerate(text.splitlines()):
        m = FENCE.match(line)
        if m:
            fence = m.group(1)
            if in_code is None:
                in_code = fence
            elif fence[0] == in_code[0] and len(fence) >= len(in_code) and not m.group(2).strip():
                in_code = None
            items.append((f"L{i + 1}", ""))
            continue
        items.append((f"L{i + 1}", "" if in_code else line))
    return check_lines(items, script)


class NotADeck(Exception):
    pass


def slide_order(z):
    """表示順のスライドXMLパスを返す。presentation.xml が読めなければファイル名順にする。"""
    try:
        pres = ET.fromstring(z.read("ppt/presentation.xml"))
        rels = ET.fromstring(z.read("ppt/_rels/presentation.xml.rels"))
        target = {r.get("Id"): r.get("Target") for r in rels.findall("rel:Relationship", NS)}
        order = []
        for sid in pres.findall("p:sldIdLst/p:sldId", NS):
            t = target.get(sid.get(f"{{{NS['r']}}}id"))
            if t:
                order.append(posixpath.normpath(posixpath.join("ppt", t.lstrip("/").removeprefix("ppt/"))))
        if order:
            return order
    except (KeyError, ET.ParseError):
        pass
    names = [n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
    if not names and "ppt/presentation.xml" not in z.namelist():
        raise NotADeck("pptx の構成ではありません")
    return sorted(names, key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))


UNIT_EMU = {"in": 914400, "cm": 360000, "mm": 36000, "pt": 12700, "pc": 152400, "pi": 152400}


def emu(value):
    """座標を EMU に直す。「2in」のような単位付きの表記も読む。読めなければ None。"""
    m = re.fullmatch(r"\s*(-?[0-9.]+)\s*([a-z]*)\s*", value or "0")
    if not m or (m.group(2) and m.group(2) not in UNIT_EMU):
        return None
    try:
        return int(float(m.group(1)) * UNIT_EMU.get(m.group(2), 1))
    except ValueError:
        return None


def box(el):
    """(x, y, 幅, 高さ) を返す。90度・270度回転している図形は幅と高さを入れ替える。"""
    xfrm = el.find(".//a:xfrm", NS)
    if xfrm is None or xfrm.find("a:off", NS) is None or xfrm.find("a:ext", NS) is None:
        return None
    off, ext = xfrm.find("a:off", NS), xfrm.find("a:ext", NS)
    vals = [emu(off.get("x")), emu(off.get("y")), emu(ext.get("cx")), emu(ext.get("cy"))]
    if None in vals:
        return None
    x, y, cx, cy = vals
    rot = (int(xfrm.get("rot", "0") or 0) // 60000) % 180 if (xfrm.get("rot") or "0").lstrip("-").isdigit() else 0
    if 45 <= rot < 135:
        x, y, cx, cy = x + (cx - cy) // 2, y + (cy - cx) // 2, cy, cx
    return x, y, cx, cy


def read_pptx(path):
    out = []
    deck_items = []
    with zipfile.ZipFile(path) as z:
        for no, name in enumerate(slide_order(z), start=1):
            loc = f"スライド{no}"
            root = ET.fromstring(z.read(name))
            paras = []
            for p in root.iter(f"{{{NS['a']}}}p"):
                text = "".join(t.text or "" for t in p.iter(f"{{{NS['a']}}}t")).strip()
                if not text:
                    continue
                paras.append(text)
                deck_items.append((loc, text))
                sizes = [int(r.get("sz")) for r in p.iter(f"{{{NS['a']}}}rPr") if r.get("sz")]
                if sizes and max(sizes) <= 1100 and len(text) >= 80 and not DISCLAIMER.search(text):
                    out.append(finding("長い免責注釈", loc, text, "小さい文字の長文注釈"))
            deck_items.append((loc, ""))

            for text, n in Counter(t for t in paras if len(t) <= 15).items():
                if n >= 3:
                    out.append(finding("同じラベルを繰り返す", loc, text, f"同じラベルが{n}回"))

            text_boxes, thin_bars, colors = [], [], set()
            for sp in root.iter(f"{{{NS['p']}}}sp"):
                b = box(sp)
                has_text = any((t.text or "").strip() for t in sp.iter(f"{{{NS['a']}}}t"))
                if b and has_text:
                    text_boxes.append(b)
                elif b and b[2] and b[3] and b[2] / b[3] <= 0.12 and b[2] <= 120000:
                    thin_bars.append(b)
                sppr = sp.find("p:spPr", NS)
                if sppr is not None:
                    for c in sppr.findall("a:solidFill/*", NS):
                        tag = c.tag.rsplit("}", 1)[1]
                        if tag in ("srgbClr", "schemeClr"):
                            # テーマ色の明度・彩度の調整（lumMod など）も別の指定として数える
                            mods = ",".join(f"{m.tag.rsplit('}', 1)[1]}={m.get('val')}" for m in c)
                            colors.add(f"{tag}:{c.get('val')}:{mods}")

            # 細い縦長図形のうち、右隣（0.3インチ以内）に縦方向で重なる文字がある本数を数える
            beside_text = 0
            for x, y, cx, cy in thin_bars:
                for tx, ty, tcx, tcy in text_boxes:
                    if 0 <= tx - (x + cx) <= 0.3 * EMU_PER_INCH and ty < y + cy and y < ty + tcy:
                        beside_text += 1
                        break
            if beside_text >= 2:
                out.append(finding("左の縦線（アクセントバー）", loc, f"文字の左に細い縦長図形 {beside_text}個", "見出しや箱の左に縦線を付けていないか"))
            elif len(thin_bars) >= 2:
                out.append(finding("図形の確認", loc, f"細い縦長図形 {len(thin_bars)}個（うち文字の左 {beside_text}個）", "位置と役割を画像で確認する", "情報"))

            h_lines = 0
            for el in list(root.iter(f"{{{NS['p']}}}cxnSp")) + list(root.iter(f"{{{NS['p']}}}sp")):
                b = box(el)
                if b and b[2] and b[3] <= b[2] * 0.05 and not any((t.text or "").strip() for t in el.iter(f"{{{NS['a']}}}t")):
                    h_lines += 1
            if h_lines >= 2:
                out.append(finding("仕切り線で区切って関係をぼかす", loc, f"水平の区切り線 {h_lines}本", "上下の要素の関係を線でごまかしていないか", "情報"))
            if len(colors) >= 4:
                out.append(finding("意味のない色分け", loc, f"図形の塗りの指定 {len(colors)}種類（直接指定、テーマ色、明度違いを区別）", "色ごとの意味を凡例で説明できるか", "情報"))
            chars = sum(len(t) for t in paras)
            if chars >= 450 or len(paras) >= 25:
                out.append(finding("1枚に全部盛る", loc, f"{len(paras)}段落・{chars}字", "主張を一文で言えるか"))
    return check_lines(deck_items) + out


def main(argv):
    flags = ("--json", "--script", "--fail-on-warn")
    as_json, script, fail = (x in argv for x in flags)
    files, words_dir = [], None
    it = iter(argv)
    for a in it:
        if a == "--words":
            words_dir = next(it, None)
            if not words_dir:
                print(__doc__.strip(), file=sys.stderr)
                return 2
        elif a not in flags:
            files.append(a)
    if not files:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        WORDS.update(load_words(words_dir))
    except (OSError, UnicodeDecodeError) as e:
        print(f"語の一覧を読めませんでした（{type(e).__name__}: {e}）", file=sys.stderr)
        return 2
    results, errors = {}, {}
    for f in files:
        path = Path(f)
        try:
            results[f] = read_pptx(path) if path.suffix.lower() == ".pptx" else read_text(path, script)
        except FileNotFoundError:
            errors[f] = "ファイルがありません"
        except NotADeck as e:
            errors[f] = f"読めませんでした（{e}）"
        except (UnicodeDecodeError, zipfile.BadZipFile, ET.ParseError, KeyError, ValueError, OSError) as e:
            errors[f] = f"読めませんでした（{type(e).__name__}）"
    warned = fail and any(x["level"] == "警告" for items in results.values() for x in items)
    code = 2 if errors else 1 if warned else 0
    if as_json:
        out = {"results": results, "errors": errors}
        if WORDS["path"]:
            out["words"] = {"path": WORDS["path"], "allow": len(WORDS["allow"]), "flag": len(WORDS["flag"])}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return code
    if WORDS["path"]:
        print(f"語の一覧: {WORDS['path']}（指摘しない語 {len(WORDS['allow'])}、必ず指摘する語 {len(WORDS['flag'])}）")
    for f, items in results.items():
        print(f"=== {f}: {len(items)}件")
        for kind, n in Counter(x["pattern"] for x in items).most_common():
            print(f"  {kind}: {n}")
        seen = set()
        for x in items:
            key = (x["pattern"], x["where"], x["text"], x["why"])
            if key in seen:
                continue
            seen.add(key)
            print(f"[{x['level']}] {x['where']} {x['pattern']} … {x['why']}\n    {x['text']}")
    for f, msg in errors.items():
        print(f"=== {f}: {msg}", file=sys.stderr)
    print("\n検出は候補。意味があって置いた型は残してよい。見た目（色・縦線・改行位置）は画像で目視確認する。")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
