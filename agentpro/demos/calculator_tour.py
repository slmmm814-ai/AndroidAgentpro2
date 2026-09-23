"""
Calculator autonomy tour.

This demo shows the agent autonomously "building" a calculator app: it
composes a complete, working HTML calculator and opens it in the browser via
an ``open_url`` action carrying a ``data:text/html`` URL. The browser renders
the calculator immediately — no file installation, no APK build, no storage
permission required.

It runs fully offline using a scripted :class:`FakeLLMClient` and the
in-memory :class:`RecordingBridgeClient`, so no device and no API key are
needed. When a real LLM and a real device are connected, the same loop works:
the LLM generates the calculator HTML itself and emits the ``open_url``
action; the Kotlin bridge opens it.
"""

from __future__ import annotations

import json
from urllib.parse import quote

from ..agent_runner import AgentRunner, RecordingBridgeClient
from ..llm_planner import FakeLLMClient


_CALCULATOR_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Calculator</title><style>body{font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;background:#111;color:#fff}.calc{width:300px;background:#222;padding:16px;border-radius:16px;box-shadow:0 8px 24px rgba(0,0,0,.5)}#scr{background:#0a0;color:#0f0;font-size:2em;text-align:right;padding:12px;border-radius:8px;margin-bottom:12px;min-height:32px;word-break:break-all}#scr.err{background:#300;color:#f55}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}button{font-size:1.3em;padding:14px;border:0;border-radius:8px;background:#444;color:#fff}button:active{background:#666}.op{background:#f90}.eq{background:#0a0;color:#0f0}.clr{background:#e33}</style></head><body><div class="calc"><div id="scr">0</div><div class="grid"><button class="clr" onclick="C()">C</button><button onclick="B()">&#9003;</button><button class="op" onclick="A('%')">%</button><button class="op" onclick="A('/')">&#247;</button><button onclick="A('7')">7</button><button onclick="A('8')">8</button><button onclick="A('9')">9</button><button class="op" onclick="A('*')">&#215;</button><button onclick="A('4')">4</button><button onclick="A('5')">5</button><button onclick="A('6')">6</button><button class="op" onclick="A('-')">&#8722;</button><button onclick="A('1')">1</button><button onclick="A('2')">2</button><button onclick="A('3')">3</button><button class="op" onclick="A('+')">+</button><button onclick="A('0')" style="grid-column:span 2">0</button><button onclick="A('.')">.</button><button class="eq" onclick="E()">=</button></div></div><script>var e='';function R(){var s=document.getElementById('scr');s.textContent=e||'0';s.className=e=='Error'?'err':''}function A(c){if(e=='Error')e='';e+=c;R()}function C(){e='';R()}function B(){e=e.slice(0,-1);R()}function E(){try{if(!/^[0-9+\\-*/.%() ]+$/.test(e))throw 0;e=Function('return('+e+')')().toString()}catch(x){e='Error'}R()}</script></body></html>"""


def build_calculator_data_url() -> str:
    """Return a ``data:text/html`` URL containing a working calculator."""
    return "data:text/html;charset=utf-8," + quote(_CALCULATOR_HTML, safe="")


def _build_script() -> list[str]:
    url = build_calculator_data_url()
    step1 = {
        "thought": (
            "I'll build a working calculator as a self-contained HTML "
            "document and open it in the browser via a data: URL."
        ),
        "action": "open_url",
        "args": {"url": url},
        "done": False,
        "confidence": 0.92,
    }
    step2 = {
        "thought": (
            "The calculator is now open and working in the browser; "
            "the goal is achieved."
        ),
        "action": "finish",
        "args": {},
        "done": True,
        "confidence": 0.95,
    }
    return [json.dumps(step1, ensure_ascii=False), json.dumps(step2)]


def build_calculator_demo(
    *, max_actions: int = 10
) -> tuple[AgentRunner, FakeLLMClient, RecordingBridgeClient]:
    """
    Build an offline, self-contained calculator-building agent.

    Returns ``(runner, fake_llm, recording_client)``. Run
    ``runner.run("build me a calculator app")`` and inspect
    ``recording_client.open_urls`` to see the generated calculator URL.
    """
    fake_llm = FakeLLMClient(script=_build_script())
    recording_client = RecordingBridgeClient()
    runner = AgentRunner(
        recording_client,
        fake_llm,
        max_actions=max_actions,
        trace_path="/tmp/agentpro_calculator_trace.jsonl",
        include_screenshot=True,
    )
    return runner, fake_llm, recording_client
