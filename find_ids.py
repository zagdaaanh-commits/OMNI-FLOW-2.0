import re

with open('app/static/index.html', encoding='utf-8') as f:
    html = f.read()

ids = re.findall(r'id="([^"]+)"', html)
for id_val in sorted(set(ids)):
    lower = id_val.lower()
    if any(k in lower for k in ['camp', 'stat', 'header', 'count', 'sidebar']):
        print(id_val)
