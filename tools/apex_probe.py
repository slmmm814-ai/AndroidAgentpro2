#!/usr/bin/env python3
"""Dump what is actually on a real screen, one or more screens deep.

Reconnaissance for live-test design: a task can only be written against
labels that really exist, and reading those labels needs the screen to itself.

    setsid nohup python3 tools/apex_probe.py com.instagram.android > /tmp/probe.log 2>&1 &

``--tap`` taps a row by label (exact first, then substring) and moves to the
next screen, so a route can be walked before a goal is written:

    python3 tools/apex_probe.py com.instagram.android --tap الملف --tap المستكشف
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

sys.path.insert(0, "/root/AndroidAgentpro2")

from agentpro.apex.apex_agent import _find_candidate  # noqa: E402
from agentpro.apex.bridge_driver import BridgeApexDriver  # noqa: E402


def _log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def _dump(driver: BridgeApexDriver, label: str) -> None:
    state = driver.snapshot()
    _log(f"===== {label} =====")
    _log(f"package={state['package']}")
    texts = [t for t in state["texts"] if t and t.strip()]
    _log(f"visible texts ({len(texts)}): {json.dumps(texts[:40], ensure_ascii=False)}")
    clickable = [
        (c.text or c.content_desc or "(no label)").strip()
        for c in state["candidates"]
        if c.clickable
    ]
    _log(f"clickable ({len(clickable)}): {json.dumps(clickable[:40], ensure_ascii=False)}")
    editable = [
        {"rid": c.resource_id, "text": c.text, "center": list(c.center())}
        for c in state["candidates"]
        if c.editable
    ]
    if editable:
        _log(f"EDITABLE FIELDS: {json.dumps(editable, ensure_ascii=False)}")
    labeled = [
        {
            "text": c.text,
            "desc": c.content_desc,
            "rid": c.resource_id,
            "center": list(c.center()),
            "clickable": c.clickable,
            "editable": c.editable,
        }
        for c in state["candidates"]
        if (c.text or "").strip() or (c.content_desc or "").strip()
    ]
    _log(f"labeled rows:\n{json.dumps(labeled[:40], ensure_ascii=False, indent=1)}")


def main() -> int:
    package = sys.argv[1] if len(sys.argv) > 1 else "com.instagram.android"
    script: list[str] = []
    for flag in ("--tap", "--type"):
        while flag in sys.argv:
            index = sys.argv.index(flag)
            script.append(f"{flag[2:]}:{sys.argv[index + 1]}")
            del sys.argv[index : index + 2]

    driver = BridgeApexDriver()
    driver._driver.client.launch_app(package)
    _log(f"launched {package}")
    time.sleep(3.0)
    _dump(driver, f"{package} / initial")

    for command in script:
        verb, _, value = command.partition(":")
        state = driver.snapshot()

        if verb == "type":
            # Search boxes are editable, not clickable: tapping the label is
            # not enough, the field has to be focused before text lands in it.
            field = next((c for c in state["candidates"] if c.editable), None)
            if field is None:
                _log(f"!! no editable field for typing {value!r}")
                break
            x, y = field.center()
            _log(f"focusing editable field {field.resource_id!r} at ({x},{y})")
            driver.tap(x, y)
            time.sleep(1.2)
            driver.input_text(value)
            _log(f"typed {value!r}")
            time.sleep(2.5)
            _dump(driver, f"after typing {value!r}")
            continue

        hit = _find_candidate(value, state["candidates"])
        if hit is None:
            _log(f"!! label {value!r} not found; stopping route here")
            break
        x, y = hit.center()
        _log(f"tapping {value!r} -> resolved {(hit.text or hit.content_desc)!r} at ({x},{y})")
        driver.tap(x, y)
        time.sleep(2.2)
        _dump(driver, f"after tapping {value!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
