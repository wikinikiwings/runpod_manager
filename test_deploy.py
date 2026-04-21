#!/usr/bin/env python3
"""Test the DeployOnDemand GraphQL mutation directly.
This is the final diagnostic step before integrating into runpod_manager.

If this script successfully creates a pod, it will print the new pod's ID
so you can immediately delete it to avoid charges.

Run inside the manager container with:
  docker compose cp test_deploy.py runpod-manager:/tmp/test_deploy.py
  docker compose exec runpod-manager python3 /tmp/test_deploy.py
"""
import urllib.request, urllib.error, json, os, sys

API_KEY = os.environ.get("RUNPOD_API_KEY", "").strip()
if not API_KEY:
    print("FATAL: RUNPOD_API_KEY env var is empty")
    sys.exit(1)

print(f"API key: len={len(API_KEY)}, prefix={API_KEY[:8]}...")
print("=" * 70)

# The exact mutation captured from the RunPod UI's Network tab
MUTATION = """mutation DeployOnDemand($input: PodFindAndDeployOnDemandInput) {
  podFindAndDeployOnDemand(input: $input) {
    id
    imageName
    env
    machineId
    machine { podHostId }
  }
}"""

# Variables matching what UI sent, with name changed to avoid collision
VARIABLES = {
    "input": {
        "cloudType": "SECURE",
        "containerDiskInGb": 20,
        "dataCenterId": "EU-RO-1",
        "globalNetwork": False,
        "gpuCount": 1,
        "gpuTypeId": "NVIDIA RTX PRO 4500 Blackwell",
        "minMemoryInGb": 62,
        "minVcpuCount": 28,
        "name": "test_diag_graphql",
        "networkVolumeId": "0czgom7b1j",
        "ports": "8188/http,8888/http,8686/http,8189/http",
        "startJupyter": True,
        "startSsh": True,
        "templateId": "i3j2sm66q8",
        "volumeInGb": 0,
        "volumeKey": None,
    }
}

print("\nMutation variables:")
print(json.dumps(VARIABLES, indent=2))
print()

payload = {
    "operationName": "DeployOnDemand",
    "query": MUTATION,
    "variables": VARIABLES,
}

# Note: include the operation= query param to match UI's URL exactly,
# in case RunPod's router uses it for anything
url = "https://api.runpod.io/graphql?operation=DeployOnDemand"

headers = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {API_KEY}",
    "User-Agent": "RunPod-Manager/6.0",
}

print(f"POST {url}")
print(f"Headers: Content-Type, Authorization (Bearer), User-Agent: RunPod-Manager/6.0")
print()
print("Sending request...")
print()

req = urllib.request.Request(
    url, data=json.dumps(payload).encode(), headers=headers, method="POST"
)

try:
    with urllib.request.urlopen(req, timeout=60) as resp:
        text = resp.read().decode()
        print(f"HTTP {resp.status}")
        print(f"Response body:")
        try:
            data = json.loads(text)
            print(json.dumps(data, indent=2))
            # Check for GraphQL-level errors
            if "errors" in data:
                print()
                print("⚠ GraphQL returned errors (HTTP was 200, but mutation failed):")
                for err in data["errors"]:
                    print(f"  - {err.get('message', err)}")
            elif data.get("data", {}).get("podFindAndDeployOnDemand"):
                pod = data["data"]["podFindAndDeployOnDemand"]
                pid = pod.get("id")
                print()
                print(f"✓ POD CREATED SUCCESSFULLY")
                print(f"  ID: {pid}")
                print(f"  Image: {pod.get('imageName')}")
                print(f"  Machine: {pod.get('machineId')}")
                print()
                print(f"⚠ DELETE IT NOW to avoid charges:")
                print(f"  docker compose exec runpod-manager runpodctl pod delete {pid}")
        except json.JSONDecodeError:
            print(text)
except urllib.error.HTTPError as e:
    body = e.read().decode() if e.fp else ""
    print(f"HTTP {e.code}")
    print(f"Response body: {body}")
except Exception as e:
    print(f"Exception: {type(e).__name__}: {e}")

print()
print("=" * 70)
print("Done.")
