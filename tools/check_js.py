"""Fetch the running list pages (port 18766) and syntax-check every inline <script> with `node --check`.
Run after changing page HTML/JS: a clash like two `const sb` in one handler stops the whole page script."""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8")
bad = 0
for path in ("/", "/?all=1", "/?s=Micron", "/sources", "/week"):
    page = requests.get("http://127.0.0.1:18766" + path, timeout=90).text
    for i, js in enumerate(re.findall(r"<script>(.*?)</script>", page, re.S)):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(js)
        p = subprocess.run(["node", "--check", f.name], capture_output=True, text=True, encoding="utf-8")
        Path(f.name).unlink()
        if p.returncode:
            bad += 1
            print(f"{path} script {i}: " + (p.stderr.strip().splitlines() or ["?"])[-1])
print("JS OK" if not bad else f"JS ERRORS: {bad}")
