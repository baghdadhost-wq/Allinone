#!/usr/bin/env python3
"""Permission prompts for the Allinone GUI.

Claude Code (-p mode) calls this MCP tool (`--permission-prompt-tool`) whenever a
tool needs approval. The request is forwarded to the GUI, which shows Allow/Deny
buttons, and the answer goes back to Claude Code. Only started for GUI runs.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp_kit import serve, tool  # noqa: E402

URL = os.environ.get("ALLINONE_APPROVAL_URL", "")
TOKEN = os.environ.get("ALLINONE_APPROVAL_TOKEN", "")
RUN_ID = os.environ.get("ALLINONE_RUN_ID", "")


@tool(description="Internal: answers Claude Code permission prompts through the Allinone GUI. Never call it yourself.")
def approve(tool_name: str = "", input: dict = None, **extra) -> str:
    tool_input = input if isinstance(input, dict) else {}
    if not URL:
        return json.dumps({"behavior": "deny", "message": "No Allinone GUI is attached to approve this."})
    body = json.dumps({"run_id": RUN_ID, "tool_name": tool_name, "input": tool_input,
                       "tool_use_id": extra.get("tool_use_id", "")}).encode("utf-8")
    req = urllib.request.Request(URL, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Allinone-Token", TOKEN)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=3600) as resp:
            decision = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return json.dumps({"behavior": "deny", "message": f"Approval failed: {exc}"})
    if decision.get("behavior") == "allow":
        return json.dumps({"behavior": "allow", "updatedInput": decision.get("updatedInput") or tool_input})
    return json.dumps({"behavior": "deny", "message": decision.get("message") or "The user denied this action."})


if __name__ == "__main__":
    serve("allinone_approval", "1.0.0")
