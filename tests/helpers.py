import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "akanuke" / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def example_blocks():
    """examples.md のコードブロックを、直前の見出し行（「直す前:」など）と組にして返す。"""
    text = (ROOT / "skills" / "akanuke" / "references" / "examples.md").read_text(encoding="utf-8")
    out = []
    section = ""
    for m in re.finditer(r"^## ([^\n]+)$|^([^\n]+):\n\n```\n(.*?)```", text, re.S | re.M):
        if m.group(1):
            section = m.group(1)
        else:
            out.append((section, m.group(2), m.group(3)))
    return out
