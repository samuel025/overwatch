"""The single place model calls happen.

Points an OpenAI-compatible client at Meterless Nightshift (or any OpenAI-compatible endpoint).
Every call is appended to a JSONL trace so the demo and the submission can show real requests.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from openai import OpenAI

from .config import LLMConfig
from .models import now_iso


class LLMError(Exception):
    pass


class LLM:
    def __init__(self, cfg: LLMConfig, trace_dir: str | Path = "traces"):
        if not cfg.base_url:
            raise LLMError("llm.base_url is empty: start Nightshift and set LLM_BASE_URL")
        self.cfg = cfg
        # Nightshift identifies the calling app and request scope through these headers. 180s timeout per
        # the Meterless Runtime contract: a cold model load can take minutes.
        headers = {"meterless-app": "overwatch", "meterless-scope": "user", **cfg.headers}
        self.client = OpenAI(
            base_url=cfg.base_url, api_key=cfg.api_key or "none", default_headers=headers, timeout=180
        )
        self.trace_path = Path(trace_dir) / "llm_calls.jsonl"
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        self.on_call: Callable[[dict[str, Any]], None] | None = None  # e.g. publish to the UI

    def health(self) -> list[str]:
        """Return available model ids, or raise a readable error if the endpoint is down."""
        try:
            return [m.id for m in self.client.models.list().data]
        except Exception as e:
            raise LLMError(
                f"cannot reach the AI provider at {self.cfg.base_url} ({e.__class__.__name__}). "
                "Is Nightshift running?"
            ) from e

    def chat(self, messages: list[dict[str, str]], model: str, agent: str) -> str:
        start = time.monotonic()
        try:
            resp = self.client.chat.completions.create(
                model=model, messages=messages, temperature=self.cfg.temperature
            )
        except Exception as e:
            raise LLMError(f"model call failed ({model}): {e}") from e
        content = resp.choices[0].message.content or ""
        self._trace(agent, model, messages, content, time.monotonic() - start, resp.usage)
        return content

    def chat_json(self, messages: list[dict[str, str]], model: str, agent: str, retries: int = 2) -> dict[str, Any]:
        """Ask for a JSON object; on unparseable output, tell the model and retry."""
        msgs = list(messages)
        for _ in range(retries + 1):
            raw = self.chat(msgs, model, agent)
            parsed = extract_json(raw)
            if parsed is not None:
                return parsed
            msgs += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": "That was not a valid JSON object. Reply with only the JSON object."},
            ]
        raise LLMError(f"{agent}: model did not return valid JSON")

    def _trace(self, agent, model, messages, content, seconds, usage) -> None:
        record = {
            "ts": now_iso(),
            "agent": agent,
            "provider_base_url": self.cfg.base_url,
            "model": model,
            "seconds": round(seconds, 2),
            "usage": usage.model_dump() if usage else None,
            "last_prompt": messages[-1]["content"][-2000:],
            "response": content[:4000],
        }
        with self.trace_path.open("a") as f:
            f.write(json.dumps(record) + "\n")
        if self.on_call:
            self.on_call({k: record[k] for k in ("agent", "model", "seconds", "usage", "provider_base_url")})


def extract_json(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a reply, tolerating code fences and reasoning text."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    start = text.find("{")
    if start != -1:
        depth = 0
        in_str = esc = False
        for i, ch in enumerate(text[start:], start):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : i + 1])
                    break
    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None
