"""Мини-клиент Metabase API. Ключ — из /etc/teashop/metabase_api_key, никуда не выводится."""
import json, time, urllib.request, urllib.error
BASE = "https://stats.201-24-120-144.sslip.io/api"
KEY = open("/etc/teashop/metabase_api_key").read().strip()

def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"x-api-key": KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path} -> HTTP {e.code}: {e.read()[:600].decode(errors='replace')}")
