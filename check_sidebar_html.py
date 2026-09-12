with open("app/static/index.html", encoding="utf-8") as f:
    lines = f.readlines()

for i, l in enumerate(lines):
    if "sidebarCampaignsList" in l:
        print("".join(lines[max(0, i-5):min(len(lines), i+15)]))
