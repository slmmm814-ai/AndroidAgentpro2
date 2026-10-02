#!/usr/bin/env python3
"""Ask Gemini about the current phone screen (or any image).

Usage:
  source /root/.agentpro/agentpro_env.sh
  python3 -m agentpro.vision "ما التطبيق وما النتيجة المعروضة؟"
  python3 -m agentpro.vision "صف الصورة" --image /path/to.png
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_MODEL = "gemini-flash-latest"
API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


def _bridge_screenshot() -> bytes:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python_core"))
    from bridge_client import BridgeClient  # noqa: E402

    client = BridgeClient()
    response = client.screenshot()
    if not response.ok:
        raise RuntimeError(f"screenshot failed: {response.error_code} {response.error_message}")
    data = response.data or {}
    b64 = data.get("base64") or data.get("data") or data.get("image")
    if not b64:
        raise RuntimeError(f"screenshot returned no image payload: {list(data)}")
    return base64.b64decode(b64)


def _ask_gemini(image_bytes: bytes, prompt: str, model: str, api_key: str) -> str:
    url = f"{API_BASE}/{model}:generateContent?key={api_key}"
    body = json.dumps(
        {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {
                            "inline_data": {
                                "mime_type": "image/png",
                                "data": base64.b64encode(image_bytes).decode(),
                            }
                        },
                    ]
                }
            ]
        }
    ).encode()

    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read())
        return payload["candidates"][0]["content"]["parts"][0]["text"]
    except urllib.error.HTTPError as error:
        detail = error.read()[:200]
        raise RuntimeError(f"Gemini HTTP {error.code}: {detail}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", help="Question about the image")
    parser.add_argument(
        "--image",
        help="Path to an image (default: live phone screenshot)",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Gemini model (default: {DEFAULT_MODEL})",
    )
    args = parser.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("ERROR: GEMINI_API_KEY is not set", file=sys.stderr)
        return 1

    if args.image:
        with open(args.image, "rb") as handle:
            image_bytes = handle.read()
    else:
        image_bytes = _bridge_screenshot()

    try:
        answer = _ask_gemini(image_bytes, args.prompt, args.model, api_key)
    except (RuntimeError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
