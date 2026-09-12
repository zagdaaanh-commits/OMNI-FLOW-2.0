with open("models/schemas.py") as f:
    lines = f.readlines()
in_class = False
for i, l in enumerate(lines):
    if "class PublishRequest" in l:
        in_class = True
    if in_class:
        print(l.rstrip())
        if l.startswith("class ") and "PublishRequest" not in l:
            break
