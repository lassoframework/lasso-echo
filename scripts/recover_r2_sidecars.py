"""
recover_r2_sidecars.py — find lasso_v2_* images already on R2 and write
their public URLs into /data/content_library/*.json sidecar files.

Run once on the Railway console:
    python3 scripts/recover_r2_sidecars.py

No image regeneration. Reads R2 (list objects), writes 3 JSON files,
then re-runs post-captions automatically.
"""
import json
import os
import subprocess
import sys

try:
    import boto3
    from botocore.config import Config
except ImportError:
    print("boto3 not installed; trying pip install ...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "boto3"])
    import boto3
    from botocore.config import Config

ENDPOINT   = os.environ.get("AGENT_S3_ENDPOINT", "")
BUCKET     = os.environ.get("AGENT_S3_BUCKET", "")
ACCESS_KEY = os.environ.get("AGENT_S3_ACCESS_KEY_ID", "")
SECRET_KEY = os.environ.get("AGENT_S3_SECRET_ACCESS_KEY", "")
BASE_URL   = os.environ.get("AGENT_S3_PUBLIC_BASE_URL", "").rstrip("/")
LIB_PATH   = os.environ.get("AGENT_LIBRARY_PATH", "content_library")

TARGETS = [
    "lasso_v2_built_by_gym_owners.png",
    "lasso_v2_speed_to_lead_concept.png",
    "lasso_v2_follow_up_problem.png",
]

missing = [v for v in ["AGENT_S3_ENDPOINT", "AGENT_S3_BUCKET",
                       "AGENT_S3_ACCESS_KEY_ID", "AGENT_S3_SECRET_ACCESS_KEY",
                       "AGENT_S3_PUBLIC_BASE_URL"] if not os.environ.get(v)]
if missing:
    print("MISSING env vars:", ", ".join(missing))
    print("Cannot list R2 without S3 credentials.")
    sys.exit(1)

print(f"Connecting to R2: {ENDPOINT} bucket={BUCKET}")
s3 = boto3.client(
    "s3",
    endpoint_url=ENDPOINT,
    aws_access_key_id=ACCESS_KEY,
    aws_secret_access_key=SECRET_KEY,
    config=Config(signature_version="s3v4"),
)

found = {}
paginator = s3.get_paginator("list_objects_v2")
for page in paginator.paginate(Bucket=BUCKET, Prefix="echo/"):
    for obj in page.get("Contents", []):
        key = obj["Key"]
        fname = key.split("/")[-1]
        if fname in TARGETS:
            url = f"{BASE_URL}/{key}"
            found[fname] = url
            print(f"  found: {fname}")
            print(f"         {url}")

if not found:
    print("\nNo lasso_v2_* images found in R2 under echo/ prefix.")
    print("The images may not have been uploaded yet. Run regen-library first.")
    sys.exit(1)

os.makedirs(LIB_PATH, exist_ok=True)
for fname, url in found.items():
    stem = fname.rsplit(".", 1)[0]
    sidecar_path = os.path.join(LIB_PATH, stem + ".json")
    sidecar = {"public_url": url, "recovered": True}
    with open(sidecar_path, "w", encoding="utf-8") as fh:
        json.dump(sidecar, fh, indent=2)
    print(f"  wrote: {sidecar_path}")

not_found = [t for t in TARGETS if t not in found]
if not_found:
    print("\nWARN: these images were NOT found on R2 (regen-library needed for them):")
    for f in not_found:
        print(f"  {f}")

print(f"\nSidecars written. Running post-captions to update cards ...")
result = subprocess.run(
    [sys.executable, "-m", "agent", "post-captions"],
    cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)
sys.exit(result.returncode)
