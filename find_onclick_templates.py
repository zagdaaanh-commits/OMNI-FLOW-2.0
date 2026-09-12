import re

with open('app/static/index.html', encoding='utf-8') as f:
    html = f.read()

for m in re.finditer(r'onclick="([^"]*\$\{[^"]*)"', html):
    print("MATCH:", m.group(0))
