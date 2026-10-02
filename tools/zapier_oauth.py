#!/usr/bin/env python3
"""Two-step Zapier MCP OAuth (manual code copy).

Step 1: build the authorization URL and persist PKCE state to a file.
Step 2: paste the redirect URL the browser tried to open (it contains
        ?code=...) and exchange it for a token.

Usage:
  python3 tools/zapier_oauth.py start
  python3 tools/zapier_oauth.py finish 'http://127.0.0.1:8765/callback?code=...&state=...'
"""
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentpro.mcp_oauth import MCPOAuthClient, OAuthError, save_token  # noqa: E402

RESOURCE = "https://mcp.zapier.com/mcp"
STATE_FILE = os.path.expanduser("~/.agentpro/zapier_pkce.json")
REDIRECT_URI = "http://127.0.0.1:8765/callback"


def step_start() -> int:
    client = MCPOAuthClient(RESOURCE)
    client.register()  # cache the dynamic client id
    url, state, verifier = client.begin_authorization(REDIRECT_URI)
    with open(STATE_FILE, "w") as fh:
        json.dump(
            {
                "client_id": client._client_id(),
                "state": state,
                "verifier": verifier,
                "redirect_uri": REDIRECT_URI,
            },
            fh,
        )
    print(url)
    return 0


def step_finish(redirect_url: str) -> int:
    if not os.path.exists(STATE_FILE):
        print("no PKCE state: run 'start' first", file=sys.stderr)
        return 2
    with open(STATE_FILE) as fh:
        pkce = json.load(fh)

    parsed = urllib.parse.urlparse(redirect_url)
    query = dict(urllib.parse.parse_qsl(parsed.query))
    code = query.get("code")
    state = query.get("state")

    if not code:
        print("the URL has no ?code= parameter", file=sys.stderr)
        return 2
    if state and pkce["state"] and state != pkce["state"]:
        print("state mismatch: this code is from a different session", file=sys.stderr)
        return 2

    client = MCPOAuthClient(RESOURCE)
    # reuse the registered client id so PKCE matches
    from agentpro.mcp_oauth import ClientRegistration

    client._registration = ClientRegistration(
        client_id=pkce["client_id"],
        client_secret=None,
        redirect_uris=(pkce["redirect_uri"],),
        supports_device_flow=False,
    )
    try:
        token=[REDACTED], pkce["verifier"], pkce["redirect_uri"])
    except OAuthError as exc:
        print(f"token exchange failed: {exc}", file=sys.stderr)
        return 1
    path = save_token(token)
    os.remove(STATE_FILE)
    print(f"saved token to {path} (expires in {token.expires_in}s)")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    if args[0] == "start":
        return step_start()
    if args[0] == "finish":
        if len(args) < 2:
            print("usage: finish '<redirect-url>'", file=sys.stderr)
            return 2
        return step_finish(args[1])
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
