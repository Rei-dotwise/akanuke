#!/usr/bin/env python3
"""akanuke の前後比較。直す前と直した後を比べ、意味が変わった恐れのある箇所を挙げる。

標準ライブラリだけで動く。.md / .txt を読む（UTF-8、読めなければ CP932）。
数値と単位、「約」などの幅、日付・曜日・時刻、否定、文の立場（依頼・提案・相談・決定・見込み・目標）、
条件（以外・場合・のみ など）の語を、文書全体で数えて比べる。
文の対応までは取らないので、出なかったから意味が同じ、とは言えない。最後は原文と並べて読む。
使い方: python3 diff.py [--json] [--fail-on-risk] [--year 2026] <直す前> <直した後>
終了コード: 0 = 比べられた / 1 = --fail-on-risk で恐れが見つかった / 2 = 読めないファイルがあった
"""
import datetime
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

UNIT = r"(?:%|パーセント|割|万円|億円|千円|円|件|人|名|社|分|時間|日間|日|週間|週|か月|ヶ月|カ月|年間|年|倍|回|字|枚|ページ|個|点|台|本|歳)"
NUM = re.compile(r"(約|およそ|ほぼ|最大|最低|最短|最長)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(" + UNIT + r")?(以上|以下|未満|程度|ほど|前後|強|弱)?")
DATE = re.compile(r"(?:(20[0-9]{2})年)?([0-9]{1,2})月([0-9]{1,2})日(?:\s*[(（]([月火水木金土日])[)）])?")
TIME = re.compile(r"([0-9]{1,2}):([0-9]{2})|([0-9]{1,2})時(?!間)(?:([0-9]{1,2})分|半)?")
WEEKDAYS = "月火水木金土日"

# 名前, 判定の単位, 語の正規表現。
# count: 語の数が変わったら恐れ / group: グループの語が全部消えたか、なかったのに現れたら恐れ / word: 語ごとに、全部消えたか新しく現れたら恐れ
# 恐れにならない変化（数だけ変わった、言い方が入れ替わった）は情報として出す。
GROUPS = [
    ("否定", "count", r"ない|ません|ず(?:に|とも)|ぬ[、。]|不可|禁止"),
    ("依頼", "group", r"ください|下さい|いただけ|いただきたく|(?<!よろしく)お願い|ほしい|欲しい"),
    ("提案", "group", r"提案"),
    ("相談", "group", r"相談"),
    ("決定", "group", r"決定|確定|決ま|決めました|決めた"),
    ("見込み・推量", "group", r"可能性|見込み|見通し|かもしれ|でしょう(?!か)|だろう|と思|はず|予想|想定"),
    ("目標・実績・試算", "group", r"目標|実績|試算|計画|目安"),
    ("条件・範囲", "word", r"以外|場合|なら|限り|除き|除いて|のみ|だけ|以降|以前|ただし"),
    ("義務・必要", "group", r"必要|必須|べき|なければ|なくては"),
    ("時期", "word", r"今日|本日|明日|昨日|先日|今週|来週|先週|今月|来月|先月|今年|来年|昨年|次回|前回|期限|までに"),
]
GROUPS = [(name, unit, re.compile(rx)) for name, unit, rx in GROUPS]
STRIP = re.compile(r"`[^`]*`")


def read(path):
    data = Path(path).read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp932")
    return unicodedata.normalize("NFKC", STRIP.sub("", text))


def features(text):
    nums = Counter()
    approx = Counter()
    for m in NUM.finditer(text):
        pre, n, unit, post = m.group(1), m.group(2).replace(",", ""), m.group(3) or "", m.group(4)
        # 日付・時刻の数字は日付の比較で見る
        tail = text[m.end(): m.end() + 1]
        if unit == "" and tail in ("月", "日", ":", "時") or text[max(0, m.start() - 1): m.start()] == ":":
            continue
        if unit in ("年",) and len(n) == 4:
            continue
        if "." not in n:
            n = n.lstrip("0") or "0"
        # 単位のない小さな整数は、箇条書きの番号と区別できないので数えない
        if not unit and "." not in n and int(n) <= 20:
            continue
        nums[f"{n}{unit}"] += 1
        if pre or post:
            approx[f"{pre or ''}{n}{unit}{post or ''}"] += 1
    dates = Counter()
    weekday = {}
    for m in DATE.finditer(text):
        key = f"{int(m.group(2))}月{int(m.group(3))}日"
        dates[key] += 1
        if m.group(4):
            weekday[key] = (m.group(1), m.group(4))
    times = Counter()
    for m in TIME.finditer(text):
        if m.group(1):
            h, mi = int(m.group(1)), int(m.group(2))
        else:
            h, mi = int(m.group(3)), int(m.group(4) or (30 if m.group(0).endswith("半") else 0))
        times[f"{h}:{mi:02d}"] += 1
    groups = {}
    for name, _, rx in GROUPS:
        groups[name] = Counter(rx.findall(text))
    return {"nums": nums, "approx": approx, "dates": dates, "weekday": weekday, "times": times, "groups": groups}


def counter_diff(a, b):
    """a にあって b で減ったもの、b で増えたもの。"""
    gone = {k: a[k] - b.get(k, 0) for k in a if a[k] > b.get(k, 0)}
    added = {k: b[k] - a.get(k, 0) for k in b if b[k] > a.get(k, 0)}
    return gone, added


def fmt(d):
    return "、".join(f"{k}（{v}）" if v > 1 else k for k, v in d.items())


def compare(src, dst, year=None):
    a, b = features(src), features(dst)
    risks = []

    def add(kind, gone, added, note):
        if gone or added:
            risks.append({"kind": kind, "gone": gone, "added": added, "note": note})

    add("数値", *counter_diff(a["nums"], b["nums"]), "数値や単位が消えたか、元にない数値が入った")
    add("数値の幅", *counter_diff(a["approx"], b["approx"]), "「約」「以上」「程度」などの幅が変わった")
    add("日付", *counter_diff(a["dates"], b["dates"]), "日付が消えたか、元にない日付が入った")
    add("時刻", *counter_diff(a["times"], b["times"]), "時刻が消えたか、元にない時刻が入った")
    for name, unit, _ in GROUPS:
        ca, cb = a["groups"][name], b["groups"][name]
        gone, added = counter_diff(ca, cb)
        if not gone and not added:
            continue
        ta, tb = sum(ca.values()), sum(cb.values())
        if unit == "count":
            risky = ta != tb
        elif unit == "group":
            risky = (ta == 0) != (tb == 0)
        else:
            gone = {k: v for k, v in gone.items() if cb.get(k, 0) == 0} or gone
            added = {k: v for k, v in added.items() if ca.get(k, 0) == 0} or added
            risky = any(cb.get(k, 0) == 0 for k in gone) or any(ca.get(k, 0) == 0 for k in added)
        if not risky:
            note = "言い方や回数だけ変わった。意味が同じか確かめる"
        elif unit == "word":
            note = "元にあった語が消えたか、元にない語が入った。条件や時期が変わっていないか"
        else:
            note = f"語の数 {ta} → {tb}。文の立場が変わっていないか"
        r = {"kind": name, "gone": gone, "added": added, "note": note}
        if not risky:
            r["info"] = True
        risks.append(r)

    for key, (y, wd) in b["weekday"].items():
        yy = int(y) if y else year
        if not yy:
            continue
        m, d = map(int, re.findall(r"[0-9]+", key))
        try:
            real = WEEKDAYS[datetime.date(yy, m, d).weekday()]
        except ValueError:
            risks.append({"kind": "曜日", "gone": {}, "added": {f"{key}（{wd}）": 1}, "note": f"{yy}年に存在しない日付"})
            continue
        if real != wd:
            risks.append({"kind": "曜日", "gone": {}, "added": {f"{key}（{wd}）": 1}, "note": f"{yy}年の{key}は{real}曜日"})
    for key, (_, wd) in a["weekday"].items():
        if key in b["weekday"] and b["weekday"][key][1] != wd:
            risks.append({"kind": "曜日", "gone": {f"{key}（{wd}）": 1}, "added": {f"{key}（{b['weekday'][key][1]}）": 1}, "note": "曜日が変わった"})
    return risks


def main(argv):
    as_json = "--json" in argv
    fail = "--fail-on-risk" in argv
    year = None
    args = []
    it = iter(argv)
    for x in it:
        if x == "--year":
            value = next(it, "")
            if not value.isdigit():
                print(__doc__.strip(), file=sys.stderr)
                return 2
            year = int(value)
        elif x not in ("--json", "--fail-on-risk"):
            args.append(x)
    if len(args) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        src, dst = read(args[0]), read(args[1])
    except (OSError, UnicodeDecodeError) as e:
        print(f"読めませんでした（{type(e).__name__}）", file=sys.stderr)
        return 2
    risks = compare(src, dst, year)
    main_risks = [r for r in risks if not r.get("info")]
    if as_json:
        print(json.dumps({"risks": risks}, ensure_ascii=False, indent=2))
    else:
        print(f"=== {args[0]} → {args[1]}: 意味が変わる恐れ {len(main_risks)}件、言い方の入れ替え {len(risks) - len(main_risks)}件")
        for r in risks:
            label = "情報" if r.get("info") else "恐れ"
            print(f"[{label}] {r['kind']} … {r['note']}")
            if r["gone"]:
                print(f"    消えた: {fmt(r['gone'])}")
            if r["added"]:
                print(f"    増えた: {fmt(r['added'])}")
        print("\n文書全体で数えた比較。出なかった箇所も、原文と1行ずつ並べて確かめる。")
    return 1 if fail and main_risks else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
