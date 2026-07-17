"""
recover_r2_sidecars.py — find lasso_v2_* images already on R2 and write
their public URLs into AGENT_LIBRARY_PATH/*.json sidecar files.

Run once on the Railway console with the app venv:
    /app/.venv/bin/python3 scripts/recover_r2_sidecars.py

No image regeneration. Lists R2, writes 3 JSON files, re-runs post-captions.
"""
import json
import os
import subprocess
import sys

# Make sure agent module resolves from /app
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import config, media_host

TARGETS = [
    "lasso_v2_built_by_gym_owners.png",
    "lasso_v2_speed_to_lead_concept.png",
    "lasso_v2_follow_up_problem.png",
]

LIB_PATH = config.LIBRARY_PATH
BASE_URL  = config.S3_PUBLIC_BASE_URL.rstrip("/")

missing = []
for var in ("AGENT_S3_ENDPOINT", "AGENT_S3_BUCKET",
            "AGENT_S3_ACCESS_KEY_ID", "AGENT_S3_SECRET_ACCESS_KEY",
            "AGENT_S3_PUBLIC_BASE_URL"):
    if not os.environ.get(var):
        missing.append(var)
if missing:
    print("MISSING env vars:", ", ".join(missing))
    sys.exit(1)

print(f"Library path : {LIB_PATH}")
print(f"R2 base URL  : {BASE_URL}")
print(f"Bucket       : {config.S3_BUCKET}")

import boto3
from botocore.config import Config as _BotoCfg

s3 = boto3.client(
    "s3",
    endpoint_url=config.S3_ENDPOINT,
    aws_access_key_id=os.environ[config.S3_ACCESS_KEY_ID_ENV],
    aws_secret_access_key=os.environ[config.S3_SECRET_ACCESS_KEY_ENV],
    config=_BotoCfg(signature_version="s3v4"),
)

print("\nScanning R2 for lasso_v2_* images ...")
found = {}
paginator = s3.get_paginator("list_objects_v2")
for page in paginator.paginate(Bucket=config.S3_BUCKET, Prefix="echo/"):
    for obj in page.get("Contents", []):
        key = obj["Key"]
        fname = key.split("/")[-1]
        if fname in TARGETS:
            url = f"{BASE_URL}/{key}"
            found[fname] = url
            print(f"  FOUND  {fname}")
            print(f"         {url}")

if not found:
    print("\nNo lasso_v2_* images found in R2 under echo/ prefix.")
    print("Images were never uploaded. Run regen-library to generate them.")
    sys.exit(1)

os.makedirs(LIB_PATH, exist_ok=True)
print(f"\nWriting sidecars to {LIB_PATH} ...")
for fname, url in found.items():
    stem = fname.rsplit(".", 1)[0]
    sidecar_path = os.path.join(LIB_PATH, stem + ".json")
    with open(sidecar_path, "w", encoding="utf-8") as fh:
        json.dump({"public_url": url, "recovered": True}, fh, indent=2)
    print(f"  wrote  {sidecar_path}")

not_found = [t for t in TARGETS if t not in found]
if not_found:
    print("\nWARN: missing on R2 (regen-library needed for these):")
    for f in not_found:
        print(f"  {f}")

print("\nRunning post-captions to update cards with image URLs ...")
venv_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       ".venv", "bin", "python3")
py = venv_py if os.path.exists(venv_py) else sys.executable
result = subprocess.run([py, "-m", "agent", "post-captions"],
                       cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.exit(result.returncode)
