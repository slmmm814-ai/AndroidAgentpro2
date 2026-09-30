"""APEX — Adaptive Phone EXecution layer.

State-of-the-art device control built on top of the v2 bridge. APEX keeps the
old FSM/tool system untouched and adds the layers that make control reliable:

* ``physics``      — spinal-cord primitives (fling until end, wait stable) with
                     no model in the loop.
* ``appmap``       — app-state graph: screens, scroll position, "seen bottom".
* ``grounder``     — hybrid targeting: exact UI-tree element first, VLM only
                     for pixel-only targets.
* ``tiered``       — one fast model per step, a big model for hard moments.
* ``apex_agent``   — the observe -> plan -> act -> verify loop that ties them
                     together.
* ``bridge_driver``— the live-device driver for that loop.
* ``run``          — ``python3 -m agentpro.apex.run "<goal>"`` entry point.

Importing this package must never touch the network or the device.
"""

__all__ = [
    "physics",
    "appmap",
    "grounder",
    "tiered",
    "apex_agent",
    "bridge_driver",
    "run",
]
