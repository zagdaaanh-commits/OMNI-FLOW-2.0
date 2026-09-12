import re

with open("app/static/index.html", encoding="utf-8") as f:
    lines = f.readlines()

for i, l in enumerate(lines[:796]):
    for m in re.finditer(r'on[a-z]+="([^"]+)"', l):
        print(f"Line {i+1}: {m.group(0)}")
