"""Typed contract: the channel budget (the claim) and the probe trace (the evidence).

``ChannelBudget`` is what a run asserts. ``ProbeTrace`` is one recorded request
in the trace; a trace *file* is a ``TraceFile`` — one ``TraceMeta`` line followed
by probe rows. Keeping the engine fingerprint in the meta line is what lets
``sideband replay`` stamp a recorded run with the same provenance a live run has
without touching an engine.

The fingerprint is deliberately split in two:

* read-back fields — whatever the public HTTP surface actually returns
  (``GET /version``, ``GET /v1/models``, ``GET /metrics``);
* ``declared`` — launch configuration no production route serves, supplied by
  the operator and bound to the command they used to start the engine.

Nothing in this module reads the environment or the network.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import __version__

TRACE_SCHEMA = "sideband.probe-trace/1"


class Verdict(str, Enum):
    """Does this engine build admit a measurable cross-session channel?"""

    open = "open"
    closed = "closed"
    inconclusive = "inconclusive"


class LoadSpec(BaseModel):
    """One point of the load sweep, as driven by the harness."""

    concurrency: int = Field(ge=1, description="concurrent request streams at this point")
    background_qps: float = Field(ge=0.0, description="background requests per second")
    prefix_cache: bool | None = Field(
        default=None,
        description="prefix caching as read back from the engine, not as assumed",
    )


class DeclaredLaunchConfig(BaseModel):
    """Launch configuration the operator declares; no production route serves it.

    vLLM exposes scheduler config only from ``GET /server_info``, which is
    registered when ``VLLM_SERVER_DEV_MODE=1`` — dev mode, off by default. So
    these values are inputs, bound to the launch command, never read back.
    """

    max_num_seqs: int | None = Field(default=None, ge=1)
    launch_cmd: str | None = None


class EngineFingerprint(BaseModel):
    """What the engine build is: read-back fields plus declared launch config."""

    READBACK_FIELDS: ClassVar[tuple[str, ...]] = (
        "version",
        "served_model",
        "max_model_len",
        "prefix_caching",
    )

    name: str = "openai-compatible"
    version: str | None = None
    served_model: str | None = None
    max_model_len: int | None = None
    prefix_caching: bool | None = None
    declared: DeclaredLaunchConfig = Field(default_factory=DeclaredLaunchConfig)

    def readback(self) -> dict[str, object]:
        """The fields this fingerprint obtained from the HTTP surface."""
        return {name: getattr(self, name) for name in self.READBACK_FIELDS}

    def unavailable(self) -> list[str]:
        """Read-back fields the server did not serve, so the report can say so."""
        return [name for name, value in self.readback().items() if value is None]


class ChannelSummary(BaseModel):
    """The encoder side of the claim: what was sent, and how much noise."""

    encoder: Literal["step-sizing"] = "step-sizing"
    bits_per_round: int = 1
    rounds: int = Field(ge=0, description="payload rounds decoded across all load points")
    decode_errors: int = Field(ge=0, description="payload rounds whose bit decoded wrong")
    null_baseline: float = Field(
        description="mean bits/session/s with the sender/receiver labels shuffled"
    )


class SweepPoint(BaseModel):
    """One row of the table: a load, a bitrate, an interval, and its null."""

    id: str = Field(description="load point label, e.g. idle / 0.5x / 1.0x")
    load: LoadSpec
    bits_per_session_second: float
    ci95: tuple[float, float]
    rounds: int = Field(ge=0)
    decode_errors: int = Field(ge=0)
    null_bits_per_session_second: float
    null_ci95: tuple[float, float]


class ChannelBudget(BaseModel):
    """A machine-checkable record of how much cross-session bandwidth a build admits."""

    engine: EngineFingerprint
    channel: ChannelSummary
    points: list[SweepPoint]
    verdict: Verdict
    verdict_reason: str
    sideband_version: str = __version__
    measured_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ProbeTrace(BaseModel):
    """One recorded request. ``session`` says who issued it; ``round`` says when."""

    kind: Literal["probe"] = "probe"
    point: str
    round: int = Field(ge=0, description="slot index inside the load point")
    session: Literal["sender", "receiver", "background"]
    req_tokens: int = Field(ge=0)
    ttft_ms: float = Field(ge=0.0)
    itl_ms: list[float] = Field(default_factory=list)
    resp_tokens: int = Field(ge=0)
    t_ms: float = Field(ge=0.0, description="request start, ms since the point began")

    @field_validator("itl_ms")
    @classmethod
    def _non_negative_itl(cls, value: list[float]) -> list[float]:
        if any(ms < 0 for ms in value):
            raise ValueError("inter-token latencies must be non-negative")
        return value


class LoadPointSpec(BaseModel):
    """How a load point was driven, and how its rounds split calibration/payload."""

    id: str
    load: LoadSpec
    slot_ms: float = Field(gt=0.0, description="wall-clock budget per round")
    n_calib: int = Field(ge=1, description="leading rounds with known calibration bits")
    n_rounds: int = Field(ge=1)


class TraceMeta(BaseModel):
    """Provenance line of a trace file: what engine produced it, under which loads."""

    model_config = ConfigDict(populate_by_name=True)

    kind: Literal["meta"] = "meta"
    schema_id: str = Field(default=TRACE_SCHEMA, alias="schema")
    engine: EngineFingerprint
    points: list[LoadPointSpec]
    synthetic: bool = Field(
        default=False,
        description="true when rows were generated, not recorded from a real engine",
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TraceFile(BaseModel):
    """A recorded probe stream: one meta line, then rows, in emission order."""

    meta: TraceMeta
    rows: list[ProbeTrace] = Field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> TraceFile:
        path = Path(path)
        meta: TraceMeta | None = None
        rows: list[ProbeTrace] = []
        for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: not JSON ({exc})") from exc
            kind = obj.get("kind")
            if kind == "meta":
                if meta is not None:
                    raise ValueError(f"{path}:{lineno}: second meta line")
                meta = TraceMeta.model_validate(obj)
            elif kind in (None, "probe"):
                rows.append(ProbeTrace.model_validate(obj))
            else:
                raise ValueError(f"{path}:{lineno}: unknown line kind {kind!r}")
        if meta is None:
            raise ValueError(f"{path}: no meta line, cannot attribute the trace to an engine")
        return cls(meta=meta, rows=rows)

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [_dump_json(self.meta.model_dump(by_alias=True, mode="json"))]
        lines.extend(_dump_json(row.model_dump(mode="json")) for row in self.rows)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def rows_for(self, point_id: str) -> list[ProbeTrace]:
        return [row for row in self.rows if row.point == point_id]

    def spec(self, point_id: str) -> LoadPointSpec:
        for spec in self.meta.points:
            if spec.id == point_id:
                return spec
        raise KeyError(f"trace has no load point {point_id!r}")


def _dump_json(obj: object) -> str:
    """Stable, compact JSON: field order comes from the model, not the caller."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
