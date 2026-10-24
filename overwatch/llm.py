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
        cfg.base_url = normalize_base_url(cfg.base_url)
        # Nightshift identifies the calling app and request scope through these headers. A call gets 180 s (Meterless
        # Runtime's own limit; a cold model load can take minutes, the slowest investigation step on record took 70 s); the SDK's own retries are
        # off, because _create retries with a policy of its own and the two would multiply.
        headers = {"meterless-app": "overwatch", "meterless-scope": "user", **cfg.headers}
        self.client = OpenAI(
            base_url=cfg.base_url, api_key=cfg.api_key or "none", default_headers=headers, timeout=180, max_retries=0
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

    RETRY_DELAYS = (4, 12, 30, 60)  # seconds; a rate limit or a blip should not fail a whole investigation

    def _create(self, patience: float | None = None, **kwargs: Any) -> Any:
        """One model call, retried with backoff on a rate limit (429), a server error or a dropped connection.
        `patience` is for optional extras (the first guess, the critic): one attempt, that many seconds at most. A call
        that times out is retried once, never repeatedly, so a stalled provider cannot hold an incident for minutes."""
        import openai

        retryable = (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError)
        client = self.client
        if patience and hasattr(client, "with_options"):
            client = client.with_options(timeout=patience, max_retries=0)
        delays = () if patience else self.RETRY_DELAYS
        for attempt, delay in enumerate((*delays, None)):
            try:
                return client.chat.completions.create(**kwargs)
            except retryable as e:
                if delay is None or (isinstance(e, openai.APITimeoutError) and attempt >= 1):
                    raise
                wait = delay
                retry_after = getattr(getattr(e, "response", None), "headers", {}).get("retry-after")
                if retry_after and str(retry_after).isdigit():
                    wait = max(delay, min(int(retry_after), 90))
                time.sleep(wait)

    def chat(self, messages: list[dict[str, str]], model: str, agent: str, extra_body: dict | None = None,
             patience: float | None = None) -> str:
        start = time.monotonic()
        try:
            resp = self._create(patience=patience, model=model, messages=messages, temperature=self.cfg.temperature,
                                extra_body=extra_body or None)
        except Exception as e:
            hint = " (the provider is rate limiting; Overwatch retried and gave up)" if "429" in str(e) else ""
            raise LLMError(f"model call failed ({model}): {e}{hint}") from e
        content = resp.choices[0].message.content or ""
        self._trace(agent, model, messages, content, time.monotonic() - start, resp.usage)
        return content

    def chat_json(
        self, messages: list[dict[str, str]], model: str, agent: str, retries: int = 2, extra_body: dict | None = None,
        patience: float | None = None,
    ) -> dict[str, Any]:
        """Ask for a JSON object; on unparseable output, tell the model and retry."""
        msgs = list(messages)
        for _ in range(retries + 1):
            raw = self.chat(msgs, model, agent, extra_body=extra_body, patience=patience)
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


def normalize_base_url(url: str) -> str:
    """`http://host:8088`, `.../v1` and `.../v1/` all mean the same endpoint; the client wants `<host>/v1`."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return url
    if url.endswith("/chat/completions"):
        url = url.removesuffix("/chat/completions")
    return url if url.endswith("/v1") else f"{url}/v1"


RUNTIME_PORTS = (8088, 8090, 8787, 11434)
RUNTIME_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def detect_runtime() -> str | None:
    """Look for a local Meterless Runtime the way its own Auto Detect does: well-known ports, three loopback names."""
    import httpx

    for port in RUNTIME_PORTS:
        for host in RUNTIME_HOSTS:
            try:
                if httpx.get(f"http://{host}:{port}/healthz", timeout=1).status_code == 200:
                    return f"http://{host}:{port}/v1"
            except Exception:
                continue
    return None


def probe(base_url: str, api_key: str = "", timeout: float = 15) -> dict[str, Any]:
    """Check an AI provider. A Meterless Runtime (answers /healthz) gets its extra checks: the placeholder
    token `rt-local` is rejected, the app headers are sent, and 401/403 (running, not authorised) is told apart from
    offline. /healthz only proves the process is up; a 200 from /v1/models with `meterless/*` ids proves it is usable."""
    import httpx
    import openai

    base_url = normalize_base_url(base_url)
    root = base_url.removesuffix("/v1")
    runtime = False
    try:
        runtime = httpx.get(f"{root}/healthz", timeout=3).status_code == 200
    except Exception:
        pass
    if runtime and api_key.strip() == "rt-local":
        return {"ok": False, "error": "rt-local is a placeholder and Meterless Runtime rejects it. "
                "Use the token in <stateDir>/proxy-token."}
    try:
        client = OpenAI(base_url=base_url, api_key=api_key or "none", timeout=timeout, max_retries=0,
                        default_headers={"meterless-app": "overwatch", "meterless-scope": "user"} if runtime else None)
        models = [m.id for m in client.models.list().data]
    except (openai.AuthenticationError, openai.PermissionDeniedError):
        who = "Meterless Runtime is running but" if runtime else "The provider"
        return {"ok": False, "error": f"{who} did not accept the token (401/403). Use the token in <stateDir>/proxy-token."}
    except Exception as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {str(e)[:200]}"}
    return {"ok": True, "models": models[:200], "meterless_runtime": runtime and any(m.startswith("meterless/") for m in models)}
