#!/usr/bin/env python3
"""
ElicitationResult hook — fires when user fills form locally.
Scans active request registry to find all pending elicitation request_ids
for this server, writes .done signal for each one.
"""
import json
import os
import sys

from utils.common import ELICIT_SIGNAL_DIR, sanitize_name


def main():
    try:
        event = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    server = sanitize_name(event.get("mcp_server_name", "unknown"))
    action = event.get("action", "")

    os.makedirs(ELICIT_SIGNAL_DIR, exist_ok=True)

    # Find all active requests for this server and signal each one
    signaled = 0
    try:
        for fname in os.listdir(ELICIT_SIGNAL_DIR):
            if not fname.endswith(".active"):
                continue
            fpath = os.path.join(ELICIT_SIGNAL_DIR, fname)
            try:
                with open(fpath) as f:
                    reg = json.load(f)
                if reg.get("server_name") != server:
                    continue
                request_id = reg["request_id"]
                done_file = os.path.join(ELICIT_SIGNAL_DIR, f"{request_id}.done")
                with open(done_file, "w") as f:
                    json.dump({"action": action, "server": server}, f)
                signaled += 1
            except (json.JSONDecodeError, KeyError, OSError):
                continue
    except OSError:
        pass

    sys.exit(0)

if __name__ == "__main__":
    main()
