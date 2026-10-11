# Copyright (c) 2026 DevRev Inc. All rights reserved.
"""Create (or reuse) the short-term AgentCore Memory for cost investigator.

Usage: python deploy/create_memory.py

Writes deploy/build/memory.json with id/arn/status.
"""
from __future__ import annotations

import json
import pathlib
import time

import boto3

REGION = "us-west-2"
NAME = "cost_investigator_stm"
# Days until STM events expire (API minimum is 3).
EVENT_EXPIRY_DAYS = 30

ctl = boto3.client("bedrock-agentcore-control", region_name=REGION)
out_path = pathlib.Path(__file__).parent / "build" / "memory.json"
out_path.parent.mkdir(parents=True, exist_ok=True)

existing = [
    m
    for m in ctl.list_memories().get("memories", [])
    if m.get("name") == NAME or m.get("id", "").startswith(f"{NAME}-")
]
if existing:
    mid = existing[0]["id"]
    print(f"Reusing memory {mid}")
else:
    resp = ctl.create_memory(
        name=NAME,
        description="Short-term conversation turns for cost investigator",
        eventExpiryDuration=EVENT_EXPIRY_DAYS,
    )
    mid = resp["memory"]["id"]
    print(f"Created memory {mid}")

while True:
    mem = ctl.get_memory(memoryId=mid)["memory"]
    status = mem.get("status")
    print(f"status={status}")
    if status not in ("CREATING", "UPDATING"):
        break
    time.sleep(5)

out = {
    "id": mem["id"],
    "arn": mem.get("arn"),
    "name": mem.get("name"),
    "status": mem.get("status"),
    "eventExpiryDuration": mem.get("eventExpiryDuration"),
}
print(json.dumps(out, indent=2, default=str))
out_path.write_text(json.dumps(out, indent=2, default=str) + "\n")
