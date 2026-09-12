import re

with open("app/static/index.html", encoding="utf-8") as f:
    lines = f.readlines()

for i, l in enumerate(lines[2451:]):
    line_num = 2452 + i
    for m in re.finditer(r'\bon[a-z]+="([^"]+)"', l):
        print(f"Line {line_num}: {m.group(0)}")
