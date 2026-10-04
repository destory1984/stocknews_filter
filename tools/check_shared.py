"""Compare the pieces this repo shares with saveticker_filter and report any drift.

The target table lives in both alerters: `targets_db.py` is the same file in both repos, and the page
scripts `COPY_IMG` (copy as image) and `TG_FILTER` (period / hide-maintained filter) are the same text
inside stock_alert.py and news_alert.py. `targets.py` differs between the repos, but the function
`latest_per_broker` (one line per broker in the by-name table) must have the same code in both;
its docstring and comments may differ. Change one side only and the two tables stop matching.

    python tools/check_shared.py [path to the saveticker_filter checkout]   # default: ../saveticker
"""
import ast
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
here = Path(__file__).resolve().parent.parent
other = Path(sys.argv[1]) if len(sys.argv) > 1 else here.parent / "saveticker"
if not (other / "news_alert.py").exists():
    sys.exit(f"saveticker_filter 를 찾지 못함: {other}")


def const(text: str, name: str) -> str:
    m = re.search(rf'^{name} = """(.*?)"""', text, re.S | re.M)
    return m.group(1) if m else ""


def func(text: str, name: str) -> str:
    """The function's code without its docstring and comments, so only a change in behaviour counts."""
    for node in ast.parse(text).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            if ast.get_docstring(node):
                node.body = node.body[1:]
            return ast.unparse(node)
    return ""


mine, theirs = (here / "stock_alert.py").read_text(encoding="utf-8"), (other / "news_alert.py").read_text(encoding="utf-8")
pairs = [("targets_db.py", (here / "targets_db.py").read_text(encoding="utf-8"), (other / "targets_db.py").read_text(encoding="utf-8"))]
pairs += [(name, const(mine, name), const(theirs, name)) for name in ("COPY_IMG", "TG_FILTER")]
pairs += [(f"targets.{name}", func((here / "targets.py").read_text(encoding="utf-8"), name),
           func((other / "targets.py").read_text(encoding="utf-8"), name)) for name in ("latest_per_broker",)]
bad = 0
for name, a, b in pairs:
    if not a or not b:
        bad += 1
        print(f"{name}: 한쪽에 없음")
    elif a != b:
        bad += 1
        la, lb = a.splitlines(), b.splitlines()
        i = next((k for k, (x, y) in enumerate(zip(la, lb)) if x != y), min(len(la), len(lb)))
        print(f"{name}: 다름 ({i + 1}번째 줄부터)\n  여기: {la[i][:100] if i < len(la) else '(끝)'}\n  저기: {lb[i][:100] if i < len(lb) else '(끝)'}")
    else:
        print(f"{name}: 같음")
sys.exit(1 if bad else 0)
