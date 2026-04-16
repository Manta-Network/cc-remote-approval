#!/usr/bin/env python3
"""Minimal MCP server with elicitation for testing."""
import json
import sys


def main():
    """JSON-RPC stdio MCP server with one tool that triggers elicitation."""
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        rid = req.get("id")
        method = req.get("method", "")

        if method == "initialize":
            _respond(rid, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "test-elicit", "version": "0.1.0"},
            })
        elif method == "notifications/initialized":
            pass  # no response needed
        elif method == "tools/list":
            _respond(rid, {"tools": [{
                "name": "ask_deploy_config",
                "description": "Ask user for deployment configuration",
                "inputSchema": {"type": "object", "properties": {}},
            }]})
        elif method == "tools/call":
            tool = req.get("params", {}).get("name", "")
            if tool == "ask_deploy_config":
                # Request elicitation
                elicit_id = f"elicit-{rid}"
                _request(elicit_id, "elicitation/create", {
                    "message": "请配置部署参数",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {
                            "environment": {
                                "type": "string",
                                "title": "环境",
                                "enum": ["production", "staging", "development"],
                            },
                            "notify_team": {
                                "type": "boolean",
                                "title": "通知团队",
                                "default": False,
                            },
                        },
                        "required": ["environment"],
                    },
                })
                # Read elicitation response
                while True:
                    resp_line = sys.stdin.readline()
                    if not resp_line:
                        return
                    try:
                        resp = json.loads(resp_line)
                    except json.JSONDecodeError:
                        continue
                    if resp.get("id") == elicit_id:
                        elicit_result = resp.get("result", {})
                        action = elicit_result.get("action", "decline")
                        content = elicit_result.get("content", {})
                        if action == "accept":
                            _respond(rid, {"content": [{"type": "text",
                                "text": f"Deploy config: {json.dumps(content)}"}]})
                        else:
                            _respond(rid, {"content": [{"type": "text",
                                "text": "User cancelled deployment config."}]})
                        break
        elif method == "ping":
            _respond(rid, {})
        else:
            _respond(rid, None, error={"code": -32601, "message": f"Unknown method: {method}"})


def _respond(rid, result, error=None):
    msg = {"jsonrpc": "2.0", "id": rid}
    if error:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _request(rid, method, params):
    msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
