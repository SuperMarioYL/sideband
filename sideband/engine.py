"""OpenAI-compatible engine adapter — public HTTP surface only.

What this module may touch:

* ``GET  {root}/version``     — served by vLLM; absent elsewhere.
* ``GET  {base}/models``      — ``served_model`` and ``max_model_len``.
* ``GET  {root}/metrics``     — ``vllm:cache_config_info`` for ``prefix_caching``.
* ``POST {base}/chat/completions`` (streaming) — the probe traffic itself.

What it must never touch: ``GET /server_info`` (registered only under
``VLLM_SERVER_DEV_MODE=1``, documented as not for production), process
environment variables such as ``VLLM_BATCH_INVARIANT``, or any guess that infers
scheduler config from timings. Fields the surface does not serve stay ``None``
and surface in the report as unavailable; the ones an operator must supply live
in :class:`~sideband.models.DeclaredLaunchConfig`.

Nothing here opens a socket at import time.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

import httpx

from .models import DeclaredLaunchConfig, EngineFingerprint

_PREFIX_CACHE_SERIES = "vllm:cache_config_info"
_LABEL_RE = re.compile(r'(\w+)="([^"]*)"')


class EngineError(RuntimeError):
    """The endpoint is unreachable or answered in a way we cannot use."""


@dataclass(slots=True)
class ChatTiming:
    """What one streamed completion cost, as observed from the client side."""

    req_tokens: int
    ttft_ms: float
    itl_ms: list[float] = field(default_factory=list)
    resp_tokens: int = 0


def estimate_tokens(text: str) -> int:
    """Rough prompt size for the trace's ``req_tokens``.

    Exact prompt token counts need ``stream_options: {"include_usage": true}``,
    which not every OpenAI-compatible server accepts. The count is descriptive
    metadata — the measurement uses step counts and timings — so an estimate is
    honest here and a compatibility hazard avoided.
    """
    return max(1, (len(text) + 3) // 4)


class EngineClient:
    """Thin streaming client plus the read-back half of the engine fingerprint."""

    def __init__(
        self,
        endpoint: str,
        *,
        timeout_s: float = 120.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._timeout = timeout_s
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_s)

    # ---- URLs -----------------------------------------------------------

    @property
    def root(self) -> str:
        """Endpoint without the ``/v1`` suffix, where vLLM serves /version and /metrics."""
        return self._endpoint.removesuffix("/v1")

    @property
    def api_base(self) -> str:
        """Base for OpenAI routes, whether or not the caller passed ``/v1``."""
        return self._endpoint if self._endpoint.endswith("/v1") else self._endpoint + "/v1"

    @property
    def chat_url(self) -> str:
        return self.api_base + "/chat/completions"

    # ---- lifecycle ------------------------------------------------------

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> EngineClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---- fingerprint ----------------------------------------------------

    def fingerprint(
        self,
        declared: DeclaredLaunchConfig,
        *,
        model: str | None = None,
    ) -> EngineFingerprint:
        """Read back what the server serves; take the rest from ``declared``."""
        version = self._read_version()
        served_model, max_model_len = self._read_models(model)
        return EngineFingerprint(
            name="vllm" if version is not None else "openai-compatible",
            version=version,
            served_model=served_model,
            max_model_len=max_model_len,
            prefix_caching=self._read_prefix_caching(),
            declared=declared,
        )

    def _read_version(self) -> str | None:
        body = self._get_json(self.root + "/version")
        if not isinstance(body, dict):
            return None
        version = body.get("version")
        return str(version) if version is not None else None

    def _read_models(self, model: str | None) -> tuple[str | None, int | None]:
        body = self._get_json(self.api_base + "/models")
        if not isinstance(body, dict):
            return None, None
        entries = [e for e in body.get("data") or [] if isinstance(e, dict)]
        if not entries:
            return None, None
        chosen = next((e for e in entries if e.get("id") == model), entries[0])
        served = str(chosen["id"]) if chosen.get("id") is not None else None
        max_len = chosen.get("max_model_len")
        return served, int(max_len) if isinstance(max_len, int) else None

    def _read_prefix_caching(self) -> bool | None:
        text = self._get_text(self.root + "/metrics")
        if text is None:
            return None
        for line in text.splitlines():
            if not line.startswith(_PREFIX_CACHE_SERIES):
                continue
            labels = dict(_LABEL_RE.findall(line))
            raw = labels.get("enable_prefix_caching")
            if raw is not None:
                return raw.strip().lower() in ("true", "1")
        return None

    # ---- probe traffic --------------------------------------------------

    def chat_stream(self, *, model: str, prompt: str, max_tokens: int) -> ChatTiming:
        """Stream one completion and time it: TTFT, then every inter-token gap."""
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "stream": True,
        }
        started = time.perf_counter()
        ttft_ms: float | None = None
        last_ms: float | None = None
        itl_ms: list[float] = []
        resp_tokens = 0

        try:
            with self._client.stream(
                "POST", self.chat_url, json=payload, timeout=self._timeout
            ) as response:
                if response.status_code >= 400:
                    detail = response.read().decode("utf-8", "replace")[:300]
                    raise EngineError(
                        f"POST {self.chat_url} -> {response.status_code}: {detail}"
                    )
                for line in response.iter_lines():
                    data = _sse_payload(line)
                    if data is None:
                        continue
                    if data == "[DONE]":
                        break
                    delta = _delta_content(data)
                    if not delta:
                        continue
                    now_ms = (time.perf_counter() - started) * 1000.0
                    if ttft_ms is None:
                        ttft_ms = now_ms
                    else:
                        itl_ms.append(now_ms - (last_ms if last_ms is not None else ttft_ms))
                    last_ms = now_ms
                    resp_tokens += 1
        except httpx.HTTPError as exc:
            raise EngineError(f"POST {self.chat_url} failed: {exc}") from exc

        if ttft_ms is None or resp_tokens == 0:
            raise EngineError(f"POST {self.chat_url} streamed no content chunks")
        return ChatTiming(
            req_tokens=estimate_tokens(prompt),
            ttft_ms=ttft_ms,
            itl_ms=itl_ms,
            resp_tokens=resp_tokens,
        )

    # ---- HTTP helpers ---------------------------------------------------

    def _get_json(self, url: str) -> object | None:
        text = self._get_text(url)
        if text is None:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    def _get_text(self, url: str) -> str | None:
        try:
            response = self._client.get(url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise EngineError(f"cannot reach {url}: {exc}") from exc
        if response.status_code == 404:
            # The route does not exist on this engine; that is data, not failure.
            return None
        if response.status_code >= 400:
            raise EngineError(f"GET {url} -> {response.status_code}: {response.text[:300]}")
        return response.text


def _sse_payload(line: str) -> str | None:
    """Return the payload of one SSE ``data:`` line, or None for keepalives."""
    stripped = line.strip()
    if not stripped or not stripped.startswith("data:"):
        return None
    return stripped[len("data:") :].strip()


def _delta_content(payload: str) -> str | None:
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return None
    choices = obj.get("choices") or []
    if not choices:
        return None
    content = (choices[0].get("delta") or {}).get("content")
    return content if isinstance(content, str) and content else None
