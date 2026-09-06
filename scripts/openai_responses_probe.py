"""Text-only proof that a hosted OpenAI model can read Voiceprint through the public MCP endpoint.

Usage (PowerShell):
  $env:OPENAI_API_KEY = "..."            # never committed
  scripts\\dev.ps1 tunnel                  # prints https://<name>.trycloudflare.com and the bearer token
  .venv\\Scripts\\python.exe scripts\\openai_responses_probe.py https://<name>.trycloudflare.com/mcp

Sends one Responses API request with a remote MCP tool and prints the mcp_list_tools / mcp_call items and the answer.
"""
import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path


def token(explicit):
    if explicit:
        return explicit
    if os.environ.get("VOICEPRINT_MCP_TOKEN"):
        return os.environ["VOICEPRINT_MCP_TOKEN"]
    path = Path(__file__).resolve().parents[1] / "data" / "mcp-token.txt"
    return path.read_text(encoding="utf-8").strip() if path.exists() else None


def mcp_tool(server_url, bearer, allowed=("list_sessions", "get_transcript", "get_current_speaker")):
    tool = {"type": "mcp", "server_label": "voiceprint", "server_url": server_url, "allowed_tools": list(allowed), "require_approval": "never",
            "server_description": "Live speaker-attributed transcript of a group conversation. Labels are similarity-based, not calibrated."}
    if bearer:
        tool["authorization"] = bearer
    return tool


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("server_url", help="public https URL ending in /mcp")
    parser.add_argument("--token", help="MCP bearer token (default: VOICEPRINT_MCP_TOKEN or data/mcp-token.txt)")
    parser.add_argument("--model", default="gpt-5")
    parser.add_argument("--prompt", default="Use the voiceprint tools: list sessions, then fetch the transcript of the newest session. "
                        "Reply in two short lines: who spoke last, and which lines (if any) were overlap or low confidence.")
    args = parser.parse_args()
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        sys.exit("Set OPENAI_API_KEY in the environment first.")
    body = {"model": args.model, "tools": [mcp_tool(args.server_url, token(args.token))], "input": args.prompt}
    request = urllib.request.Request("https://api.openai.com/v1/responses", json.dumps(body).encode(),
                                     {"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        sys.exit(f"HTTP {error.code}: {error.read().decode('utf-8', 'replace')[:2000]}")
    for item in result.get("output", []):
        kind = item.get("type")
        if kind == "mcp_list_tools":
            print("mcp_list_tools:", [t["name"] for t in item.get("tools", [])])
        elif kind == "mcp_call":
            print(f"mcp_call {item.get('name')}({item.get('arguments')}) -> {'ERROR ' + str(item.get('error')) if item.get('error') else str(item.get('output'))[:300]}")
        elif kind == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    print("ANSWER:", part["text"])
    usage = result.get("usage", {})
    print("tokens in/out:", usage.get("input_tokens"), usage.get("output_tokens"))


if __name__ == "__main__":
    main()
