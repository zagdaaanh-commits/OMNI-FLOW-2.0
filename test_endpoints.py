from app.main import app

routes = []
for route in app.routes:
    if hasattr(route, "methods"):
        for m in route.methods:
            routes.append(f"{m} {route.path}")

print(f"Total routes in app: {len(routes)}")

endpoints_in_html = [
    ("POST", "/content/generate"),
    ("GET", "/campaigns"),
    ("POST", "/campaign/create"),
    ("GET", "/campaign/{campaign_id}/details"),
    ("GET", "/campaign/{campaign_id}"),
    ("POST", "/publish/schedule"),
    ("GET", "/publish/tasks"),
    ("GET", "/analytics/report"),
    ("GET", "/campaign/boost"),
    ("GET", "/settings/apis"),
    ("POST", "/settings/apis"),
    ("POST", "/settings/apis/test"),
    ("POST", "/demo/seed"),
    ("GET", "/auth/me"),
    ("POST", "/auth/register"),
    ("POST", "/auth/login"),
    ("POST", "/auth/logout"),
    ("GET", "/integrations/status"),
    ("POST", "/integrations/connect"),
    ("POST", "/integrations/disconnect")
]

for method, path in endpoints_in_html:
    found = any(r == f"{method} {path}" for r in routes)
    print(f"{method} {path}: {'FOUND' if found else 'MISSING'}")
