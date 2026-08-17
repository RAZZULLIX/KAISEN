# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Project agent — a multi-turn tool loop over one project.

The agent reads state, plans, and acts through VALIDATED tools (spec
edits are guardrail-scanned, smoke runs are real, snapshots are taken
before every mutation). Designed for small models: JSON-line actions,
short tool outputs, per-turn hints on failure. Tier-aware system prompt
from the prompt library.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from . import promptlib
from .llm import GenerationCancelled

MAX_TURNS = 14
MAX_TOOL_OUT = 4000



def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> None:
    """Recursive merge of override into base (mutates base)."""
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v

def extract_json_actions(text: str, key: str = "tool") -> List[Dict[str, Any]]:
    """Pull JSON objects out of a model reply: per-line first, then any
    balanced-brace spans (nested braces survive). `key` selects the
    discriminator field ("tool" for the agent loop, "action" for the
    config agent)."""

    actions: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and key in obj:
            actions.append(obj)
    if actions:
        return actions
    # Fallback: balanced-brace scan anywhere in the text.
    i = 0
    n = len(text)
    while i < n:
        start = text.find("{", i)
        if start == -1:
            break
        depth = 0
        in_str = False
        esc = False
        for j in range(start, n):
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:j + 1])
                        if isinstance(obj, dict) and key in obj:
                            actions.append(obj)
                    except json.JSONDecodeError:
                        pass
                    i = j + 1
                    break
        else:
            break
        if depth != 0:
            break
    return actions


class ProjectAgent:
    def __init__(
        self,
        request: Callable[[str], str],
        tier: str,
        project_name: str,
        language: str,
        goal: str,
        tools: Dict[str, Callable[[Dict[str, Any]], str]],
        mission: str = "",
        cancel: Optional[threading.Event] = None,
    ):
        self.request = request
        self.tools = tools
        self.cancel = cancel or threading.Event()
        self.turns: List[Dict[str, Any]] = []
        self.summary = ""
        self.system = promptlib.project_agent_prompt(tier, project_name, goal or mission or "Improve the project", language)
        if mission:
            self.system += f"\n\nYOUR MISSION: {mission}"

    def run(self) -> str:
        conversation = self.system
        hint_strikes = 0
        for turn in range(MAX_TURNS):
            if self.cancel.is_set():
                self.summary = "cancelled by user"
                return self.summary
            try:
                raw = self.request(conversation)
            except GenerationCancelled:
                self.summary = "cancelled by user"
                return self.summary
            except Exception as e:
                self.summary = f"agent request failed: {e}"
                return self.summary
            actions = extract_json_actions(raw)
            if not actions:
                hint_strikes += 1
                if hint_strikes >= 3:
                    self.summary = "agent produced no valid actions for 3 turns"
                    return self.summary
                conversation += (
                    "\n\nNo valid tool call found. Reply with EXACTLY one JSON line per turn, e.g.\n"
                    '{"tool": "read_spec"}\n'
                    '{"tool": "done", "summary": "..."}\n'
                )
                continue
            hint_strikes = 0
            for act in actions:
                tool = str(act.get("tool", ""))
                self.turns.append({"turn": turn, "tool": tool, "args": act})
                if tool == "done":
                    self.summary = str(act.get("summary", "done"))
                    return self.summary
                fn = self.tools.get(tool)
                if fn is None:
                    out = f"ERROR: unknown tool '{tool}'. Available: {', '.join(sorted(self.tools))}"
                else:
                    try:
                        out = fn(act)
                    except Exception as e:
                        out = f"ERROR: tool raised: {e}"
                conversation += f"\n\nTOOL OUTPUT ({tool}):\n{out[:MAX_TOOL_OUT]}"
        self.summary = "agent hit the turn limit"
        return self.summary
