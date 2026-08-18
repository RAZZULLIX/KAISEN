# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""LLM orchestration layer.

Model-agnostic: servers are configured in the general config (config.json
under `llm.servers`), can be added/removed live from the GUI, and are
selected for use via `llm.active_ids` (checkbox selection).

Server types:
  - "llama"   : llama.cpp /completion endpoint   {url, params}
  - "openai"  : OpenAI-compatible /chat/completions {base_url, model, api_key, params}
  - "remote"  : arbitrary {url, payload_template: "...{{PROMPT}}..."}

Behavior:
  - Per-server timeouts (connect/read/nodata) with a global default.
  - A server that fails is temporarily banned for the run; the pool falls
    back to the remaining active servers.
  - Per-server stats: requests, failures, avg seconds, tps — exposed to the
    GUI.
"""

from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

from .config import FRAMEWORK_ROOT, FrameworkConfig, get_config
from .util import load_json, save_json

DEFAULT_PARAMS = {
    "n_predict": -1,
    # Legacy parity: no prompt-cache reuse — a stale slot cache is the
    # classic trigger for degenerate streams, and these prompts change
    # every generation anyway.
    "cache_prompt": False,
    "stream": False,
}

TIER_RANK = {"tiny": 0, "small": 1, "large": 2}

# Chat templates for raw /completion endpoints.  OpenAI-compatible servers
# (type "openai") apply the model's template SERVER-side; these client-side
# renderers exist for llama.cpp /completion and "remote" servers, where the
# raw prompt must already be in the model's native format.  `auto` infers
# the template from the configured model name.
CHAT_TEMPLATES = ("auto", "gptoss", "chatml", "qwen", "llama3", "llama2",
                  "gemma", "mistral", "deepseek", "none")


def _infer_chat_template(model: str) -> str:
    """Guess the native chat format from a model name.  Unknown names fall
    back to ChatML — the most common open format (Qwen, Phi, Yi, MiniCPM,
    Mistral v0.3+, and most frontier APIs)."""
    m = (model or "").lower()
    if "gpt-oss" in m or "gpt_oss" in m:
        return "gptoss"
    if "qwen" in m:
        return "qwen"
    if "gemma" in m:
        return "gemma"
    if "llama-3" in m or "llama3" in m or "llama_3" in m:
        return "llama3"
    if "llama-2" in m or "llama2" in m:
        return "llama2"
    if "mistral" in m or "mixtral" in m:
        return "mistral"
    if "deepseek" in m:
        return "deepseek"
    return "chatml"


def _chat_transcript(messages: List[Dict[str, str]], template: str = "auto") -> str:
    """Role-tagged conversation text for raw /completion servers, in the
    model's native format.  Assistant turns render so the final answer
    channel is the only one the model continues.  Ends with the token(s)
    that open the assistant role."""
    t = template if template in CHAT_TEMPLATES else "auto"
    if t == "auto":
        t = "chatml"
    msgs = list(messages)
    if not msgs or msgs[0].get("role") != "system":
        msgs.insert(0, {"role": "system",
                        "content": "You are KAISEN, an AI coding framework agent. You reply exactly as each task instructs."})
    role_map = {"system": "system", "user": "user", "assistant": "assistant"}

    def content(m: Dict[str, str]) -> str:
        return str(m.get("content", ""))

    if t == "gptoss":
        parts = []
        for m in msgs:
            role = role_map.get(str(m.get("role", "user")).lower(), "user")
            if role == "assistant":
                parts.append(f"<|start|>assistant<|channel|>final<|message|>{content(m)}<|end|>")
            else:
                parts.append(f"<|start|>{role}<|message|>{content(m)}<|end|>")
        parts.append("<|start|>assistant")
        return "\n".join(parts)

    if t == "chatml" or t == "qwen":
        parts = []
        for m in msgs:
            role = role_map.get(str(m.get("role", "user")).lower(), "user")
            parts.append(f"<|im_start|>{role}\n{content(m)}<|im_end|>\n")
        parts.append("<|im_start|>assistant\n")
        return "".join(parts)

    if t == "llama3":
        parts = ["<|begin_of_text|>"]
        for m in msgs:
            role = role_map.get(str(m.get("role", "user")).lower(), "user")
            parts.append(f"<|start_header_id|>{role}<|end_header_id|>\n\n{content(m)}<|eot_id|>")
        parts.append("<|start_header_id|>assistant<|end_header_id|>\n\n")
        return "".join(parts)

    if t == "llama2":
        # System is folded into the first user turn; assistant turns are
        # wrapped between [INST]...[/INST] pairs.
        system = next((m for m in msgs if m.get("role") == "system"), None)
        first_user_idx = next((i for i, m in enumerate(msgs) if m.get("role") == "user"), 0)
        parts = ["<s>"]
        first_user_done = False
        for m in msgs:
            role = str(m.get("role", "user")).lower()
            if role == "system":
                continue
            if role == "assistant":
                parts.append(f"{content(m)} </s>")
            else:
                if not first_user_done and system:
                    body = f"<<SYS>>\n{content(system)}\n<</SYS>>\n\n{content(m)}"
                    first_user_done = True
                else:
                    body = content(m)
                parts.append(f"[INST] {body} [/INST]")
        return "".join(parts)

    if t == "gemma":
        # Gemma has no system role; fold it into the first user turn.
        system = next((m for m in msgs if m.get("role") == "system"), None)
        first_user = True
        parts = ["<bos>"]
        for m in msgs:
            role = str(m.get("role", "user")).lower()
            if role == "system":
                continue
            body = content(m)
            if first_user and system:
                body = f"{content(system)}\n\n{body}"
                first_user = False
            label = "model" if role == "assistant" else "user"
            parts.append(f"<start_of_turn>{label}\n{body}<end_of_turn>\n")
        parts.append("<start_of_turn>model\n")
        return "".join(parts)

    if t == "mistral":
        # Mistral v0.2: [INST] ... [/INST]; system folded into the first
        # user turn (v0.3+ uses ChatML — configure "chatml" for those).
        system = next((m for m in msgs if m.get("role") == "system"), None)
        first_user = True
        parts = []
        for m in msgs:
            role = str(m.get("role", "user")).lower()
            if role == "system":
                continue
            if role == "assistant":
                parts.append(f"{content(m)}</s>")
            else:
                body = f"{content(system)}\n\n{content(m)}" if first_user and system else content(m)
                first_user = False
                parts.append(f"[INST] {body} [/INST]")
        return "".join(parts)

    if t == "deepseek":
        # DeepSeek-V3/R1 native template: <｜begin▁of▁sentence｜>Role\n...<｜end▁of▁sentence｜>.
        role_map_ds = {"system": "System", "user": "User", "assistant": "Assistant"}
        parts = []
        for m in msgs:
            role = role_map_ds.get(str(m.get("role", "user")).lower(), "User")
            parts.append(f"<｜begin▁of▁sentence｜>{role}\n{content(m)}<｜end▁of▁sentence｜>\n")
        parts.append("# Assistant:\n")
        return "".join(parts)

    # none: plain concatenation with role prefixes — no template tokens.
    return "\n".join(f"{m.get('role', 'user')}: {content(m)}" for m in msgs) + "\nassistant:"


def _cap_predict(payload: Dict[str, Any], global_cfg: FrameworkConfig) -> Dict[str, Any]:
    """Prevent runaway generations: unless a server declares its own
    finite n_predict, cap it at the framework default (llm.max_tokens).
    An unlimited cap lets a degenerating model loop until context end —
    burning minutes of compute on garbage."""
    if payload.get("n_predict") in (None, -1):
        cap = int(global_cfg.llm.get("max_tokens", 8192) or 8192)
        payload["n_predict"] = max(1, cap)
    return payload






class ServerError(Exception):
    pass


class GenerationCancelled(Exception):
    """Raised when an in-flight generation is cancelled (engine stop)."""


class Server:
    """A configured LLM endpoint with live state."""

    def __init__(self, cfg: Dict[str, Any], global_cfg: FrameworkConfig):
        self._global_cfg = global_cfg
        self.id: str = cfg.get("id", "")
        # User-facing display label.  Free-form, deliberately NOT unique —
        # the user may name several endpoints the same ("GIPPO" x2) and
        # tell them apart by chat slot numbers.
        self.label: str = cfg.get("label", "") or ""
        self.type: str = cfg.get("type", "llama")
        self.url: str = cfg.get("url", "")
        self.base_url: str = cfg.get("base_url", "")
        self.model: str = cfg.get("model", "")
        # Client-side chat template for raw /completion servers.  "auto"
        # (default) infers from the model name; "none" disables templating.
        raw_tpl = cfg.get("chat_template")
        self.chat_template: str = raw_tpl if raw_tpl in CHAT_TEMPLATES else \
            (_infer_chat_template(self.model) if raw_tpl in (None, "", "auto") else "auto")
        # Secrets are env-first (KAISEN_SERVER_<ID>_API_KEY / KAISEN_OPENAI_API_KEY);
        # a value in config.json is only a fallback and is never shown in the GUI.
        self.api_key: str = global_cfg.server_api_key(self.id, cfg.get("api_key", ""))
        self.params: Dict[str, Any] = dict(cfg.get("params", {}) or {})
        self.payload_template: str = cfg.get("payload_template", "")
        self.max_concurrent: int = int(cfg.get("max_concurrent", 1))
        self.timeout: float = float(cfg.get("timeout", global_cfg.llm.get("read_timeout", 1200)))
        self.connect_timeout: float = float(cfg.get("connect_timeout", global_cfg.llm.get("connect_timeout", 15)))
        self.nodata_timeout: float = float(cfg.get("nodata_timeout", global_cfg.llm.get("nodata_timeout", 120)))
        self.spawn_cmd: List[str] = list(cfg.get("spawn_cmd", []) or [])
        self.enabled: bool = bool(cfg.get("enabled", True))
        # Routing profile: the orchestrator prefers the lowest tier that
        # can do the job, then the highest priority, then free capacity.
        self.tier: str = str(cfg.get("tier") or "small").lower()
        self.priority: int = int(cfg.get("priority", 1) or 1)
        self.context_window: int = int(cfg.get("context_window", 0) or 0)
        # Smartness score (0-10) and $ per 1M tokens — routing/cost math.
        # Explicit config wins; absent -> tier default. Local servers = $0.
        self.smartness: float = float(cfg.get("smartness", 0) or 0) or \
            {"tiny": 2.0, "small": 5.0, "large": 8.0}.get(self.tier, 5.0)
        cost_cfg = cfg.get("cost") if isinstance(cfg.get("cost"), dict) else {}
        self.cost_in: float = float(cfg.get("cost_in", cost_cfg.get("in", 0.0)) or 0.0)
        self.cost_out: float = float(cfg.get("cost_out", cost_cfg.get("out", 0.0)) or 0.0)
        self._lock = threading.Lock()
        self._inflight = 0
        self._banned_until: float = 0.0
        # Reachability, probed ONCE on activation (or proven by use):
        # None = unknown, True = reachable, False = offline.
        self._online: Optional[bool] = None
        self._stats = {"requests": 0, "failures": 0, "total_seconds": 0.0, "last_tps": 0.0, "last_error": None}

    # -- capacity / health -------------------------------------------------
    @property
    def busy(self) -> bool:
        with self._lock:
            return self._inflight >= self.max_concurrent

    def acquire(self) -> bool:
        with self._lock:
            if self._inflight >= self.max_concurrent or self._banned_until > time.time() or not self.enabled:
                return False
            self._inflight += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._inflight = max(0, self._inflight - 1)

    def ban(self, seconds: float = 60.0, reason: str = "") -> None:
        with self._lock:
            self._banned_until = time.time() + seconds
            self._stats["last_error"] = reason

    @property
    def banned(self) -> bool:
        return self._banned_until > time.time()

    def mark_online(self, ok: Optional[bool]) -> None:
        with self._lock:
            self._online = ok

    @property
    def online(self) -> Optional[bool]:
        with self._lock:
            return self._online
    def record(self, ok: bool, seconds: float, tokens: int = 0) -> None:
        with self._lock:
            self._stats["requests"] += 1
            if not ok:
                self._stats["failures"] += 1
            self._stats["total_seconds"] += seconds
            if seconds > 0 and tokens > 0:
                self._stats["last_tps"] = tokens / seconds

    def estimate(self, tokens_in: int, tokens_out: int = 0) -> Dict[str, Any]:
        """Cost/time estimate for one call: expected tokens, wall seconds
        from the measured tps (fallback 10 tps), and $ from per-Mtoken
        prices (0 = free/local server)."""
        with self._lock:
            tps = self._stats["last_tps"] or 10.0
        seconds = (tokens_in + max(tokens_out, tokens_in // 2)) / tps
        usd = (tokens_in / 1_000_000) * self.cost_in + \
              (max(tokens_out, tokens_in // 2) / 1_000_000) * self.cost_out
        return {
            "server": self.id,
            "tokens_in": tokens_in,
            "tokens_out": max(tokens_out, tokens_in // 2),
            "tps": round(tps, 1),
            "seconds": round(seconds, 1),
            "cost_usd": round(usd, 6),
        }

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            stats = dict(self._stats)
            avg = (stats["total_seconds"] / stats["requests"]) if stats["requests"] else None
            return {
                "id": self.id,
                "label": self.label,
                "type": self.type,
                "url": self.url or self.base_url,
                "model": self.model,
                "enabled": self.enabled,
                "busy": self._inflight >= self.max_concurrent,
                "online": self._online,
                "inflight": self._inflight,
                "max_concurrent": self.max_concurrent,
                "banned": self.banned,
                "tier": self.tier,
                "priority": self.priority,
                "context_window": self.context_window,
                "smartness": self.smartness,
                "cost_in": self.cost_in,
                "cost_out": self.cost_out,
                "stats": {**stats, "avg_seconds": avg},
            }

    # -- request -----------------------------------------------------------
    def request(self, prompt: str, extra_params: Optional[Dict[str, Any]] = None) -> str:
        t0 = time.time()
        try:
            if self.type == "llama":
                payload = dict(DEFAULT_PARAMS)
                payload.update(self.params)
                payload.update(extra_params or {})
                payload["prompt"] = prompt
                payload = _cap_predict(payload, self._global_cfg)
                resp = requests.post(
                    self.url, json=payload,
                    headers={"Authorization": "Bearer dummy", "Content-Type": "application/json"},
                    timeout=(self.connect_timeout, self.timeout),
                )
                resp.raise_for_status()
                resp.encoding = "utf-8"  # charset-less JSON bodies — decode explicitly
                data = resp.json()
                content = data.get("content", "") if isinstance(data, dict) else ""
                tokens = data.get("tokens_predicted") if isinstance(data, dict) else None
                if tokens is None:
                    tokens = max(1, len(content) // 4)
            elif self.type == "openai":
                messages = [{"role": "user", "content": prompt}]
                payload = {"model": self.model, "messages": messages, "stream": False}
                payload.update(self.params)
                payload.update(extra_params or {})
                resp = requests.post(
                    self.base_url.rstrip("/") + "/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    timeout=(self.connect_timeout, self.timeout),
                )
                resp.raise_for_status()
                resp.encoding = "utf-8"  # charset-less JSON bodies — decode explicitly
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                try:
                    tokens = int(data.get("usage", {}).get("completion_tokens") or 0) or max(1, len(content) // 4)
                except (KeyError, TypeError):
                    tokens = max(1, len(content) // 4)
            elif self.type == "remote":
                body = self.payload_template.replace("{{PROMPT}}", prompt)
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError:
                    payload = {"prompt": prompt, "text": body}
                resp = requests.post(
                    self.url, json=payload,
                    headers={"Authorization": "Bearer dummy", "Content-Type": "application/json"},
                    timeout=(self.connect_timeout, self.timeout),
                )
                resp.raise_for_status()
                resp.encoding = "utf-8"  # charset-less bodies — decode explicitly
                content = resp.text
                tokens = max(1, len(content) // 4)
            else:
                raise ServerError(f"unknown server type {self.type!r}")
            self.record(True, time.time() - t0, tokens)
            return content

        except Exception as e:
            self.record(False, time.time() - t0)
            raise ServerError(f"{self.id}: {e}") from e

    # -- streaming request ------------------------------------------------
    def request_stream(
        self,
        prompt: str,
        on_token: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> str:
        """Stream a completion token-by-token.

        - llama : SSE on /completion (stream: true)
        - openai: SSE on /chat/completions (stream: true)
        - remote: no streaming — plain request, delivered in one chunk

        `cancel_event` is checked between chunks; when set, raises
        GenerationCancelled.  The read timeout applies between chunks, so a
        stalled server fails fast instead of hanging a producer.
        """
        t0 = time.time()
        try:
            if self.type == "remote":
                content = self.request(prompt)
                if on_token:
                    on_token(content, max(1, len(content) // 4))
                self.record(True, time.time() - t0, max(1, len(content) // 4))
                return content
            if self.type == "llama":
                payload = dict(DEFAULT_PARAMS)
                payload.update(self.params)
                payload.update({"prompt": prompt, "stream": True})
                payload = _cap_predict(payload, self._global_cfg)
                resp = requests.post(
                    self.url, json=payload,
                    headers={"Authorization": "Bearer dummy", "Content-Type": "application/json"},
                    stream=True,
                    timeout=(self.connect_timeout, self.nodata_timeout),
                )
                resp.raise_for_status()
                content, tokens = self._consume_sse(resp, on_token, cancel_event, llama=True)
            elif self.type == "openai":
                messages = [{"role": "user", "content": prompt}]
                payload = {"model": self.model, "messages": messages, "stream": True}
                payload.update(self.params)
                resp = requests.post(
                    self.base_url.rstrip("/") + "/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    stream=True,
                    timeout=(self.connect_timeout, self.nodata_timeout),
                )
                resp.raise_for_status()
                content, tokens = self._consume_sse(resp, on_token, cancel_event, llama=False)
            else:
                raise ServerError(f"unknown server type {self.type!r}")
            self.record(True, time.time() - t0, tokens)
            return content
        except GenerationCancelled:
            raise
        except Exception as e:
            self.record(False, time.time() - t0)
            raise ServerError(f"{self.id}: {e}") from e
    def request_chat(self, messages: List[Dict[str, str]],
                     extra_params: Optional[Dict[str, Any]] = None) -> str:
        """Conversational request with proper roles. OpenAI-compatible
        servers get native `messages`; llama/remote get a role-tagged
        transcript so the model continues in assistant role."""
        if self.type == "openai":
            payload = {"model": self.model, "messages": messages, "stream": False}
            payload.update(self.params)
            payload.update(extra_params or {})
            resp = requests.post(
                self.base_url.rstrip("/") + "/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                timeout=(self.connect_timeout, self.timeout),
            )
            resp.raise_for_status()
            resp.encoding = "utf-8"
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        return self.request(_chat_transcript(messages, self.chat_template), extra_params)

    def request_chat_stream(self, messages: List[Dict[str, str]],
                            on_token: Optional[Callable[[str], None]] = None,
                            cancel_event: Optional[threading.Event] = None) -> str:
        """Streaming variant of request_chat."""
        if self.type == "openai":
            payload = {"model": self.model, "messages": messages, "stream": True}
            payload.update(self.params)
            resp = requests.post(
                self.base_url.rstrip("/") + "/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                stream=True,
                timeout=(self.connect_timeout, self.nodata_timeout),
            )
            resp.raise_for_status()
            content, _ = self._consume_sse(resp, on_token, cancel_event, llama=False)
            return content
        return self.request_stream(_chat_transcript(messages, self.chat_template), on_token=on_token,
                                   cancel_event=cancel_event)

    def _consume_sse(
        self,
        resp: Any,
        on_token: Optional[Callable[[str, int], None]],
        cancel_event: Optional[threading.Event],
        llama: bool,
    ) -> tuple:
        """Consume a streaming response.  Returns (full_text, token_count).

        Token counting is REAL: one SSE content event = one token for
        llama.cpp streams and OpenAI-style deltas; the final llama.cpp
        object's `tokens_predicted` (when present) is authoritative.

        Degenerate-stream guard: a reply that collapses into '?????...'
        repeats is rejected here (ServerError) so the orchestrator bans
        the endpoint and retries elsewhere instead of streaming junk.
        """
        chunks: List[str] = []
        token_count = 0
        final_count: Optional[int] = None
        total_chars = 0
        qmark_chars = 0
        t_start = time.time()
        try:
            for raw in resp.iter_lines(decode_unicode=False):
                # llama.cpp sends `text/event-stream` with no charset;
                # requests then guesses ISO-8859-1 and mangles every
                # non-ASCII token (mojibake like "commandâ\x80\x91line").
                # SSE payloads are UTF-8 by spec — decode explicitly.
                line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
                # Absolute cap (legacy parity): a server trickling one token
                # per chunk under the nodata timeout must not stream forever.
                if time.time() - t_start > self.timeout:
                    raise ServerError(f"{self.id}: stream exceeded total read timeout ({self.timeout:.0f}s)")
                if cancel_event is not None and cancel_event.is_set():
                    raise GenerationCancelled("generation cancelled")
                line = line.strip()
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("error"):
                        raise ServerError(f"{self.id}: server error: {obj['error']}")
                    if llama:
                        token = obj.get("content", "")
                        if isinstance(obj.get("tokens_predicted"), int):
                            final_count = obj["tokens_predicted"]
                        if obj.get("stop"):
                            if token:
                                chunks.append(token)
                                token_count += 1
                                total_chars += len(token)
                                qmark_chars += token.count("?")
                                if on_token:
                                    on_token(token, 1)
                            break
                    else:
                        try:
                            token = obj["choices"][0]["delta"].get("content", "")
                        except (KeyError, IndexError, TypeError):
                            token = ""
                        if token is None:
                            token = ""
                        if not token:
                            continue
                    if token:
                        chunks.append(token)
                        token_count += 1
                        total_chars += len(token)
                        qmark_chars += token.count("?")
                        if total_chars > 200 and qmark_chars / total_chars > 0.9:
                            raise ServerError(
                                f"{self.id}: degenerate stream "
                                f"({qmark_chars}/{total_chars} '?' chars) — rejecting"
                            )
                        if on_token:
                            on_token(token, 1)
                else:
                    # Non-SSE JSON body: a plain (non-streaming) reply from a
                    # server that ignored stream:true.  Parse it once.
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if llama:
                        token = obj.get("content", "")
                        if isinstance(obj.get("tokens_predicted"), int):
                            final_count = obj["tokens_predicted"]
                    else:
                        try:
                            token = obj["choices"][0]["message"]["content"]
                        except (KeyError, IndexError, TypeError):
                            token = ""
                    if token:
                        chunks.append(token)
                        token_count += 1
                        if on_token:
                            on_token(token, 1)
                    break
            return "".join(chunks), (final_count if final_count is not None else token_count)
        finally:
            try:
                resp.close()
            except Exception:
                pass


class ModelOrchestrator:
    """Round-robin pool over active servers with retries and banning."""

    def __init__(self, config: FrameworkConfig | None = None):
        self.cfg = config or get_config()
        self._lock = threading.Lock()
        self._servers: Dict[str, Server] = {}
        self._active_ids: List[str] = []
        self._rr = 0
        self._status: Dict[str, Any] = {"state": "idle", "last_activity": None}
        # Per-(server, skill) scoreboard: attempts / one-shot successes /
        # wins / accumulated $ — the data behind "which model does what
        # best, and what is better to just not use". Persisted NEXT TO the
        # config (sibling file, gitignored) — never inside config.json.
        self._stats_path = Path(str(self.cfg.path) + ".skill_stats.json") \
            if getattr(self.cfg, "path", None) else (FRAMEWORK_ROOT / "model_stats.json")
        self._skill_stats: Dict[tuple, Dict[str, Any]] = {}
        self._load_stats()
        self._reload_servers()

    # -- skill scoreboard --------------------------------------------------
    def _load_stats(self) -> None:
        data = load_json(self._stats_path, None)
        if isinstance(data, dict):
            self._skill_stats = {
                tuple(k.split("\x1f")): dict(v)
                for k, v in data.items()
                if isinstance(v, dict)
            }

    def _save_stats(self) -> None:
        try:
            save_json(self._stats_path, {
                "\x1f".join(k): v for k, v in self._skill_stats.items()
            })
        except Exception:  # stats must never break a generation
            pass

    def record_call(self, server_id: str, skill: str, cost_usd: float = 0.0) -> None:
        """Every completed LLM call for a skill: attempts + accumulated $."""
        with self._lock:
            st = self._skill_stats.setdefault(
                (server_id, skill),
                {"attempts": 0, "oneshots": 0, "wins": 0, "cost_usd": 0.0})
            st["attempts"] += 1
            st["cost_usd"] = round(st["cost_usd"] + cost_usd, 6)
            self._save_stats()

    def record_outcome(self, server_id: str, skill: str, kind: str) -> None:
        """Skill outcome: 'oneshot' (first-try success, no corrective
        loop) or 'win' (the skill's end goal achieved)."""
        if kind not in ("oneshot", "win"):
            return
        with self._lock:
            st = self._skill_stats.setdefault(
                (server_id, skill),
                {"attempts": 0, "oneshots": 0, "wins": 0, "cost_usd": 0.0})
            st[kind + "s"] += 1
            self._save_stats()

    def model_stats(self) -> List[Dict[str, Any]]:
        """The scoreboard: one row per (server, skill)."""
        with self._lock:
            out = []
            for (sid, skill), st in sorted(self._skill_stats.items()):
                s = self._servers.get(sid)
                attempts = int(st.get("attempts", 0))
                oneshots = int(st.get("oneshots", 0))
                wins = int(st.get("wins", 0))
                out.append({
                    "server_id": sid,
                    "label": s.label if s else sid,
                    "tier": s.tier if s else "?",
                    "skill": skill,
                    "attempts": attempts,
                    "oneshots": oneshots,
                    "wins": wins,
                    "oneshot_rate": round(oneshots / attempts, 3) if attempts else None,
                    "win_rate": round(wins / attempts, 3) if attempts else None,
                    "cost_usd": round(float(st.get("cost_usd", 0.0)), 6),
                })
            return out

    def allowlist_for(self, skill: str) -> List[str]:
        """Per-skill model allowlist from config: entries are server ids
        or 'tier:<tier>'. Empty/absent = no restriction."""
        return list((self.cfg.llm.get("allowlists") or {}).get(skill, []) or [])

    def _allowed(self, sid: str, skill: Optional[str]) -> bool:
        if not skill:
            return True
        rules = self.allowlist_for(skill)
        if not rules:
            return True
        s = self._servers.get(sid)
        if s is None:
            return False
        for r in rules:
            r = str(r).strip()
            if r.startswith("tier:"):
                if s.tier == r[5:].strip().lower():
                    return True
            elif r == sid:
                return True
        return False

    def _skill_quality(self, sid: str, skill: str) -> float:
        """Smoothed quality 0..1 for adaptive routing: one-shots count
        1, wins count 3 (a champion proves more than a pass)."""
        st = self._skill_stats.get((sid, skill), {})
        attempts = int(st.get("attempts", 0))
        oneshots = int(st.get("oneshots", 0))
        wins = int(st.get("wins", 0))
        return (oneshots + 3.0 * wins) / (attempts + 2.0)

    def _skill_cost(self, sid: str) -> float:
        s = self._servers.get(sid)
        if s is None:
            return 0.0
        est = s.estimate(1024, 512)
        return float(est.get("cost_usd", 0.0) or 0.0)


    # -- registry management ----------------------------------------------
    def _reload_servers(self) -> None:
        with self._lock:
            self._servers = {}
            for sc in self.cfg.llm.get("servers", []) or []:
                if sc.get("id"):
                    self._servers[sc["id"]] = Server(sc, self.cfg)
            self._active_ids = [i for i in self.cfg.llm.get("active_ids", []) if i in self._servers]
            if not self._active_ids:
                self._active_ids = [s for s in self._servers if self._servers[s].enabled]
                self.cfg.llm["active_ids"] = list(self._active_ids)
                self.cfg.save()

    def persist(self) -> None:
        with self._lock:
            self.cfg.llm["servers"] = [self._server_spec(s) for s in self._servers]
            self.cfg.llm["active_ids"] = list(self._active_ids)
        self.cfg.save()

    def _server_spec(self, sid: str) -> Dict[str, Any]:
        s = self._servers[sid]
        # api_key is deliberately NOT persisted: secrets come from the
        # environment (KAISEN_SERVER_<ID>_API_KEY) or are re-entered.
        return {
            "id": s.id, "label": s.label, "type": s.type, "url": s.url, "base_url": s.base_url,
            "model": s.model, "params": s.params, "chat_template": s.chat_template,
            "payload_template": s.payload_template, "max_concurrent": s.max_concurrent,
            "timeout": s.timeout, "enabled": s.enabled,
            "tier": s.tier, "priority": s.priority, "context_window": s.context_window,
            "smartness": s.smartness, "cost_in": s.cost_in, "cost_out": s.cost_out,
        }

    def list_servers(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [self._servers[s].snapshot() for s in sorted(self._servers)]

    def add_server(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        sid = spec.get("id")
        if not sid:
            raise ValueError("server id required")
        with self._lock:
            self._servers[sid] = Server(spec, self.cfg)
            if spec.get("enabled", True):
                if sid not in self._active_ids:
                    self._active_ids.append(sid)
        self.persist()
        return self._servers[sid].snapshot()

    def remove_server(self, sid: str) -> None:
        with self._lock:
            self._servers.pop(sid, None)
            self._active_ids = [i for i in self._active_ids if i != sid]
        self.persist()
    def set_active(self, ids: List[str], persist: bool = True) -> None:
        with self._lock:
            new_ids = [i for i in ids if i in self._servers]
            for i in new_ids:
                if i not in self._active_ids:
                    # Newly activated: forget reachability so the GUI
                    # probes exactly once.
                    self._servers[i].mark_online(None)
            self._active_ids = new_ids
        if persist:
            self.persist()

    def set_enabled(self, sid: str, enabled: bool) -> None:
        with self._lock:
            if sid in self._servers:
                self._servers[sid].enabled = enabled
        self.persist()

    def set_label(self, sid: str, label: str) -> Dict[str, Any]:
        """Set a server's display label.  Labels are free-form and NOT
        unique: two endpoints may share a label ("GIPPO" x2); the pill
        tells them apart by the endpoint address and chat slot numbers."""
        with self._lock:
            s = self._servers.get(sid)
            if s is None:
                raise ValueError("unknown server")
            s.label = (label or "").strip()
        self.persist()
        return self._servers[sid].snapshot()

    def check_health(self, sid: str) -> Dict[str, Any]:
        """Probe a server endpoint with a trivial request."""
        s = self._servers.get(sid)
        if s is None:
            return {"ok": False, "error": "unknown server"}
        try:
            # Cap the probe HARD (8 tokens): a rambling local model must
            # not turn a connectivity test into a minutes-long essay.
            cap = {"n_predict": 8} if s.type == "llama" else {"max_tokens": 8}
            text = s.request("Reply with the single word: ok", cap)
            s.mark_online(True)
            return {"ok": True, "reply": text[:80]}
        except Exception as e:
            s.mark_online(False)
            return {"ok": False, "error": str(e)}

    # -- use ---------------------------------------------------------------
    @property
    def active_config(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            for sid in self._active_ids:
                s = self._servers.get(sid)
                if s and not s.banned and s.enabled:
                    return s.snapshot()
        return None

    @property
    def is_busy(self) -> bool:
        with self._lock:
            return any(self._servers[s].busy for s in self._active_ids if s in self._servers)

    def ensure_active(self) -> bool:
        """True if at least one usable server exists."""
        return self.active_config is not None

    def request(self, prompt: str, pipeline_id: int = 0, max_retries: Optional[int] = None,
                min_tier: str = "tiny", skill: str = "unknown") -> str:
        """Send a prompt, routed to the lowest tier that can serve it, with
        per-server retries and fallback. Raises ServerError when all fail."""
        retries = max_retries if max_retries is not None else int(self.cfg.llm.get("max_retries", 3))
        backoff = float(self.cfg.llm.get("retry_backoff", 2.0))
        last_err: Optional[str] = None
        self._status.update({"state": "writing", "last_activity": time.time()})
        try:
            for attempt in range(retries):
                sid = self._acquire_server(min_tier=min_tier, skill=skill)
                s = self._servers[sid]
                try:
                    out = s.request(prompt)
                    s.mark_online(True)
                    self._status.update({"state": "idle"})
                    self.record_call(sid, skill, self._call_cost(s, prompt, out))
                    return out
                except ServerError as e:
                    last_err = str(e)
                    s.mark_online(False)
                    s.ban(seconds=min(300, 30 * (attempt + 1)), reason=last_err)
                    time.sleep(backoff * (attempt + 1))
                finally:
                    s.release()
            raise ServerError(f"all LLM servers failed: {last_err}")
        finally:
            self._status.update({"state": "idle"})
    def request_chat_stream(
        self,
        messages: List[Dict[str, str]],
        max_retries: Optional[int] = None,
        on_token: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        session: Optional[Any] = None,
        min_tier: str = "tiny",
        skill: str = "unknown",
    ) -> tuple:
        """Conversational stream over the pool (see request_stream)."""
        retries = max_retries if max_retries is not None else int(self.cfg.llm.get("max_retries", 3))
        backoff = float(self.cfg.llm.get("retry_backoff", 2.0))
        last_err: Optional[str] = None
        self._status.update({"state": "writing", "last_activity": time.time()})
        try:
            for attempt in range(retries):
                sid = self._acquire_server(cancel_event=cancel_event, session=session,
                                           min_tier=min_tier, skill=skill)
                s = self._servers[sid]
                try:
                    out = s.request_chat_stream(messages, on_token=on_token, cancel_event=cancel_event)
                    self._status.update({"state": "idle"})
                    prompt_txt = " ".join(str(m.get("content", "")) for m in messages)
                    self.record_call(sid, skill, self._call_cost(s, prompt_txt, out))
                    return out, sid
                except GenerationCancelled:
                    raise
                except ServerError as e:
                    last_err = str(e)
                    s.mark_online(False)
                    s.ban(seconds=min(300, 30 * (attempt + 1)), reason=last_err)
                    time.sleep(backoff * (attempt + 1))
                finally:
                    s.release()
            raise ServerError(f"all LLM servers failed: {last_err}")
        finally:
            self._status.update({"state": "idle"})

    def request_stream(
        self,
        prompt: str,
        pipeline_id: int = 0,
        max_retries: Optional[int] = None,
        on_token: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        session: Optional[Any] = None,
        min_tier: str = "tiny",
        skill: str = "unknown",
    ) -> tuple:
        """Stream a completion token-by-token (see Server.request_stream).

        Returns (full_text, server_id).  Raises ServerError when all servers
        fail and GenerationCancelled when `cancel_event` is set mid-stream
        (the failed server is NOT banned for user cancellations).

        `session` (optional): a live-view session object; its `server_id`
        is bound as soon as the server is picked so the GUI can show which
        LLM is streaming before it finishes."""
        retries = max_retries if max_retries is not None else int(self.cfg.llm.get("max_retries", 3))
        backoff = float(self.cfg.llm.get("retry_backoff", 2.0))
        last_err: Optional[str] = None
        self._status.update({"state": "writing", "last_activity": time.time()})
        try:
            for attempt in range(retries):
                sid = self._acquire_server(cancel_event=cancel_event, session=session,
                                           min_tier=min_tier, skill=skill)
                s = self._servers[sid]
                try:
                    out = s.request_stream(prompt, on_token=on_token, cancel_event=cancel_event)
                    self._status.update({"state": "idle"})
                    self.record_call(sid, skill, self._call_cost(s, prompt, out))
                    return out, sid
                except GenerationCancelled:
                    raise
                except ServerError as e:
                    last_err = str(e)
                    s.mark_online(False)
                    s.ban(seconds=min(300, 30 * (attempt + 1)), reason=last_err)
                    time.sleep(backoff * (attempt + 1))
                finally:
                    s.release()
            raise ServerError(f"all LLM servers failed: {last_err}")
        finally:
            self._status.update({"state": "idle"})

    def _acquire_server(
        self,
        cancel_event: Optional[threading.Event] = None,
        session: Optional[Any] = None,
        min_tier: str = "tiny",
        skill: str = "unknown",
    ) -> str:
        """Wait for a usable server. Busy/banned/disabled servers are
        TRANSIENT states — the caller just waits, the iteration never
        fails for them. Cancellation still wins while waiting."""
        self._status.update({"state": "waiting", "last_activity": time.time()})
        if session is not None:
            session.waiting = True
        try:
            while True:
                sid = self._pick_server(min_tier=min_tier, skill=skill)
                if sid is not None:
                    return sid
                if cancel_event is not None and cancel_event.is_set():
                    raise GenerationCancelled("cancelled while waiting for a free LLM server")
                time.sleep(float(self.cfg.llm.get("pool_wait_interval", 1.0)))
        finally:
            if session is not None:
                session.waiting = False
            self._status.update({"state": "writing", "last_activity": time.time()})

    def _call_cost(self, server: "Server", prompt: str, out: str) -> float:
        """Estimated $ for one call from the per-Mtoken prices."""
        try:
            est = server.estimate(max(1, len(prompt) // 4), max(1, len(out) // 4))
            return float(est.get("cost_usd", 0.0) or 0.0)
        except Exception:
            return 0.0

    def _pick_server(self, min_tier: str = "tiny", skill: Optional[str] = None) -> Optional[str]:
        """Routing: the LOWEST smartness tier that satisfies the
        requirement, then highest priority, then free capacity. Busy
        servers fall through so the pipeline never stalls.

        Per-skill model allowlists (config llm.allowlists) HARD-filter the
        candidates — a skill can be pinned to (or banned from) specific
        models or tiers. With config llm.routing = "adaptive", the best
        measured score-per-dollar for THIS skill reorders the eligible
        set (min_tier still applies; cost-first stays the default)."""
        with self._lock:
            candidates = [s for s in self._active_ids
                          if s in self._servers and self._servers[s]._online is not False]
            if skill:
                candidates = [s for s in candidates if self._allowed(s, skill)]
            if not candidates:
                return None
            rank_min = TIER_RANK.get(min_tier, 0)
            adaptive = str(self.cfg.llm.get("routing", "cost")).lower() == "adaptive"
            if adaptive and skill:
                # best measured quality-per-dollar for this skill first
                # (cost 0 local servers: cost floor keeps the order stable).
                ordered = sorted(
                    candidates,
                    key=lambda sid: (
                        -self._skill_quality(sid, skill) /
                        max(self._skill_cost(sid), 0.0001),
                        TIER_RANK.get(self._servers[sid].tier, 1),
                        -int(getattr(self._servers[sid], "priority", 1) or 1),
                        self._servers[sid]._inflight,
                    ),
                )
            else:
                ordered = sorted(
                    candidates,
                    key=lambda sid: (
                        TIER_RANK.get(self._servers[sid].tier, 1),
                        -int(getattr(self._servers[sid], "priority", 1) or 1),
                        self._servers[sid]._inflight,
                    ),
                )
            for sid in ordered:
                s = self._servers[sid]
                if s.banned or not s.enabled or s.busy:
                    continue
                if TIER_RANK.get(s.tier, 1) >= rank_min and s.acquire():
                    return sid
            # Requirement unsatisfiable (all qualifying servers busy):
            # fall back to any usable server rather than stalling.
            for _ in range(len(candidates) * 2):
                self._rr = (self._rr + 1) % len(candidates)
                sid = candidates[self._rr]
                s = self._servers[sid]
                if not s.banned and s.enabled and s.acquire():
                    return sid
            return None

    def release(self, sid: str) -> None:
        s = self._servers.get(sid)
        if s:
            s.release()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "state": self._status.get("state", "idle"),
                "last_activity": self._status.get("last_activity"),
                "active_ids": list(self._active_ids),
                "servers": [self._servers[s].snapshot() for s in sorted(self._servers)],
            }


def get_orchestrator() -> ModelOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = ModelOrchestrator()
    return _orchestrator


_orchestrator: ModelOrchestrator | None = None
