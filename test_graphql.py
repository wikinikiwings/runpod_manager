#!/usr/bin/env python3
"""GraphQL diagnostic tests for RunPod API.
Run inside the manager container with: docker compose exec runpod-manager python3 /tmp/test_graphql.py
"""
import urllib.request, urllib.error, json, os, sys

API_KEY = os.environ.get("RUNPOD_API_KEY", "").strip()
if not API_KEY:
    print("FATAL: RUNPOD_API_KEY env var is empty")
    sys.exit(1)

print(f"API key loaded: len={len(API_KEY)}, prefix={API_KEY[:8]}...")
print("=" * 70)

def call(label, payload, headers_extra=None, url="https://api.runpod.io/graphql"):
    print(f"\n[{label}]")
    print(f"  URL: {url}")
    headers = {"Content-Type": "application/json"}
    if headers_extra:
        headers.update(headers_extra)
    ua = headers.get("User-Agent", "default urllib")
    print(f"  UA: {ua}")
    print(f"  Auth: {'Bearer present' if 'Authorization' in headers else 'NO AUTH HEADER'}")
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode()
            print(f"  Status: {resp.status}")
            print(f"  Body: {text[:500]}")
            return text
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        print(f"  HTTP {e.code}")
        print(f"  Body: {body[:500]}")
        return None
    except Exception as e:
        print(f"  Exception: {type(e).__name__}: {e}")
        return None

# Test A: with Manager's User-Agent (the one already proven to work)
print("\n--- TESTS WITH MANAGER USER-AGENT ---")

call("Test A1: myself / Bearer / Manager UA",
     {"query": "query{myself{id}}"},
     {"Authorization": f"Bearer {API_KEY}",
      "User-Agent": "RunPod-Manager/6.0"})

call("Test A2: pods list / Bearer / Manager UA",
     {"query": "query{myself{pods{id name desiredStatus}}}"},
     {"Authorization": f"Bearer {API_KEY}",
      "User-Agent": "RunPod-Manager/6.0"})

# Test B: with browser-like User-Agent (matches what UI would send)
print("\n--- TESTS WITH BROWSER USER-AGENT ---")

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

call("Test B1: myself / Bearer / Browser UA",
     {"query": "query{myself{id}}"},
     {"Authorization": f"Bearer {API_KEY}",
      "User-Agent": BROWSER_UA})

# Test C: also try via try_gql_bearer from the manager module — proves end-to-end
print("\n--- TESTING VIA MANAGER'S OWN FUNCTIONS ---")

sys.path.insert(0, "/app")
try:
    import runpod_manager as rm
    rm._api_key = API_KEY
    print("\n[Test C1: rm.try_gql_bearer()]")
    result = rm.try_gql_bearer()
    if result is None:
        print("  Result: None (function returned None — likely failed)")
    else:
        print(f"  Result: list of {len(result)} pods")
        for p in result[:3]:
            print(f"    - {p.get('name','?')} ({p.get('id','?')}) status={p.get('desiredStatus','?')}")
except Exception as e:
    print(f"  Exception: {type(e).__name__}: {e}")

print("\n" + "=" * 70)
print("Done.")
