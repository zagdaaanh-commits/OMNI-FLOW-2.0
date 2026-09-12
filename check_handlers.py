import re

with open('app/static/index.html', encoding='utf-8') as f:
    html = f.read()

# Extract script
script_matches = re.findall(r'<script>(.*?)</script>', html, re.DOTALL)
if not script_matches:
    print("No <script> tag found!")
    exit(1)
script_code = script_matches[0]

# Find all event handlers in HTML
handlers = re.findall(r'on\w+="([^"]+)"', html)
print(f"Total inline event handlers in HTML: {len(handlers)}")

calls = []
for h in handlers:
    parts = [p.strip() for p in h.split(';') if p.strip()]
    for p in parts:
        m = re.match(r'^([a-zA-Z0-9_$]+)\s*\(', p)
        if m:
            calls.append(m.group(1))
        else:
            calls.append(p)

defined_funcs = set(re.findall(r'function\s+([a-zA-Z0-9_$]+)\s*\(', script_code))
defined_funcs.update(re.findall(r'async\s+function\s+([a-zA-Z0-9_$]+)\s*\(', script_code))
defined_funcs.update(re.findall(r'(?:let|const|var)\s+([a-zA-Z0-9_$]+)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>', script_code))
defined_funcs.update(re.findall(r'(?:let|const|var)\s+([a-zA-Z0-9_$]+)\s*=\s*function', script_code))
defined_funcs.update(re.findall(r'window\.([a-zA-Z0-9_$]+)\s*=', script_code))

print(f"Defined functions in script: {len(defined_funcs)}")

missing = []
for c in set(calls):
    if c.startswith('document.') or c.startswith('window.') or c.startswith('console.'):
        continue
    if c not in defined_funcs:
        missing.append(c)

print(f"Missing / unmatched handlers ({len(missing)}): {missing}")
