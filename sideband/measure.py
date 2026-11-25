"""The measurement pipeline: sweep, bitrate with a bootstrap interval, verdict.

Both entry points run the same code: a live run builds a :class:`TraceFile` from
probe traffic and analyses it; ``sideband replay`` loads a recorded trace and
analyses it. There is no second statistics path, so a replay cannot read rosier
than the live run that produced the trace.

Per payload round the receiver contributes one decoded bit; the round's cost is
the wall-clock slot it occupied. Throughput is therefore a ratio estimator
``sum(correct - wrong) / sum(slot seconds)``, and its interval comes from a
bootstrap over rounds. Subtracting wrong bits is what gives the null an
expectation of ~0: with the ground-truth bits permuted against the decoded ones,
correct and wrong cancel at chance.
"""

from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

from . import canary
from .engine import ChatTiming, EngineClient, EngineError
from .models import (
    ChannelBudget,
    ChannelSummary,
    DeclaredLaunchConfig,
    LoadPointSpec,
    LoadSpec,
    ProbeTrace,
    Provenance,
    SweepPoint,
    TraceFile,
    TraceMeta,
    Verdict,
)

#: Fixed so that replaying the same trace reproduces the same interval.
DEFAULT_SEED = 20260119
BOOTSTRAP_SAMPLES = 2000

BACKGROUND_TOKENS = 8
BACKGROUND_PROMPT = "Summarize batching behaviour in one short line."
RECEIVER_PROMPT = "Reply with the single word: ok"
_FILLER_WORDS = (
    "the quick brown fox jumps over the lazy engine while continuous batching "
    "admits a wide step and every tenant waits for the same kernel"
).split()

#: (label, fraction of baseline qps, background worker streams). The receiver and
#: sender are always in flight, so concurrency is workers + 2.
_LOAD_SHAPES: tuple[tuple[str, float, int], ...] = (
    ("idle", 0.0, 0),
    ("0.5x", 0.5, 2),
    ("1.0x", 1.0, 4),
)


class MeasurementError(RuntimeError):
    """A load point could not be evaluated from the rows it has."""


class SweepConfig(BaseModel):
    """How the live sweep is driven. Replay ignores this: it obeys the trace."""

    seconds: float = Field(default=60.0, gt=0, description="wall-clock target for the sweep")
    slot_ms: float = Field(default=300.0, gt=0)
    calib_rounds: int = Field(default=12, ge=4)
    baseline_qps: float = Field(default=4.0, ge=0)
    probe_tokens: int = Field(default=6, ge=1)
    seed: int = DEFAULT_SEED


# ---------------------------------------------------------------------------
# Analysis — the one statistics path
# ---------------------------------------------------------------------------


def compute_point(
    spec: LoadPointSpec,
    rows: list[ProbeTrace],
    *,
    seed: int = DEFAULT_SEED,
) -> SweepPoint:
    """Decode one load point from its rows and report bits/session/s with a null."""
    senders = _by_round(rows, "sender")
    receivers = _by_round(rows, "receiver")
    paired = sorted(set(senders) & set(receivers))
    if not paired:
        raise MeasurementError(f"point {spec.id}: no round has both sender and receiver rows")

    calib = [r for r in paired if r < spec.n_calib]
    payload = [r for r in paired if r >= spec.n_calib]
    if not calib:
        raise MeasurementError(f"point {spec.id}: no calibration rounds survived")
    if not payload:
        raise MeasurementError(f"point {spec.id}: no payload rounds survived")

    threshold = canary.calibrate(
        [canary.slot_score(receivers[r]) for r in calib],
        [canary.bit_from_sender(senders[r]) for r in calib],
    )
    decoded = canary.decode([canary.slot_score(receivers[r]) for r in payload], threshold)
    truth = np.asarray([canary.bit_from_sender(senders[r]) for r in payload], dtype=np.int8)

    slot_seconds = _slot_seconds(
        {r: receivers[r].t_ms for r in paired}, payload, spec.slot_ms
    )
    dt = np.asarray([slot_seconds[r] for r in payload], dtype=np.float64)

    rng = np.random.default_rng(seed)
    delta = canary.net_bits(decoded, truth)
    bits = _ratio(delta, dt)
    ci95 = _bootstrap_ci(delta, dt, rng)

    null_delta = canary.net_bits(decoded, canary.shuffled_bits(truth, rng))
    return SweepPoint(
        id=spec.id,
        load=spec.load,
        slot_ms=spec.slot_ms,
        bits_per_session_second=bits,
        ci95=ci95,
        rounds=len(payload),
        decode_errors=int((decoded != truth).sum()),
        null_bits_per_session_second=_ratio(null_delta, dt),
        null_ci95=_bootstrap_ci(null_delta, dt, rng),
    )


def summarise(points: list[SweepPoint]) -> tuple[Verdict, str]:
    """Turn the table into one verdict, with the sentence a reader needs.

    The decision turns on one number, the *noise floor*: the upper end of the
    shuffled-bits interval, taken across load points. That control is what the
    decoder reports when the ground-truth bits are permuted against the receiver's
    decoded bits, so a measured bitrate has to beat it before it counts as signal.

    * ``open`` — some point is above the floor *and* its interval excludes zero.
    * ``closed`` — every point sits at or below the floor: this build admits no
      measurable channel at the tested loads (``max_num_seqs 1`` is the usual way
      to get here).
    * ``inconclusive`` — above the floor somewhere, but no interval clears zero:
      more rounds or a quieter host, then re-run.
    """
    if not points:
        raise MeasurementError("no load points to judge")

    noise_floor = max(p.null_ci95[1] for p in points)
    assertive = [
        p
        for p in points
        if p.bits_per_session_second > noise_floor and p.ci95[0] > 0
    ]
    if assertive:
        best = max(assertive, key=lambda p: p.bits_per_session_second)
        return Verdict.open, (
            f"{best.bits_per_session_second:.1f} bits/session/s at {best.id} "
            f"(ci95 {best.ci95[0]:.1f}-{best.ci95[1]:.1f}) rises above the "
            f"{noise_floor:.1f} bits/session/s shuffled-bits floor; "
            f"{len(assertive)} of {len(points)} load points read open"
        )

    best = max(points, key=lambda p: p.bits_per_session_second)
    if all(p.bits_per_session_second <= noise_floor for p in points):
        return Verdict.closed, (
            f"No load point rises above the {noise_floor:.1f} bits/session/s "
            f"shuffled-bits floor (best {best.bits_per_session_second:.1f} at {best.id}); "
            "this build admits no measurable step-sizing channel at the tested loads"
        )
    return Verdict.inconclusive, (
        f"{best.bits_per_session_second:.1f} bits/session/s at {best.id} sits above the "
        f"{noise_floor:.1f} bits/session/s floor, but its interval still reaches zero — "
        "signal and noise are not separated by this sweep"
    )


def analyse(
    trace: TraceFile,
    *,
    source: Literal["live", "replay"],
    trace_path: str | None = None,
    seed: int = DEFAULT_SEED,
) -> ChannelBudget:
    """Compute the budget for a trace: the same call replay and live runs share.

    ``source`` has no default on purpose: a budget has to state whether this
    process drove the probes or re-read them from a file, and a trace marked
    synthetic carries a fingerprint that was authored, not read back.
    """
    points = [
        compute_point(trace.spec(spec.id), trace.rows_for(spec.id), seed=seed)
        for spec in trace.meta.points
    ]
    verdict, reason = summarise(points)
    channel = ChannelSummary(
        rounds=sum(p.rounds for p in points),
        decode_errors=sum(p.decode_errors for p in points),
        null_baseline=float(np.mean([p.null_bits_per_session_second for p in points])),
    )
    return ChannelBudget(
        provenance=Provenance(
            source=source,
            synthetic=trace.meta.synthetic,
            # Generated rows come with a generated fingerprint: neither was read
            # off an engine.
            fingerprint_source="authored" if trace.meta.synthetic else "read-back",
            trace_path=trace_path,
        ),
        engine=trace.meta.engine,
        channel=channel,
        points=points,
        verdict=verdict,
        verdict_reason=reason,
        # The stamp belongs to the evidence, not to this computation: a replay
        # reports when the probes ran and reproduces the same document twice.
        measured_at=trace.meta.created_at,
    )


# ---------------------------------------------------------------------------
# Live sweep
# ---------------------------------------------------------------------------


def load_points(config: SweepConfig, prefix_cache: bool | None) -> list[LoadPointSpec]:
    """The fixed three-point sweep: idle, half and full baseline background load."""
    total_rounds = max(
        config.calib_rounds + 8,
        int(config.seconds * 1000.0 / (config.slot_ms * len(_LOAD_SHAPES))),
    )
    return [
        LoadPointSpec(
            id=label,
            load=LoadSpec(
                concurrency=workers + 2,
                background_qps=round(config.baseline_qps * fraction, 3),
                prefix_cache=prefix_cache,
            ),
            slot_ms=config.slot_ms,
            n_calib=min(config.calib_rounds, total_rounds - 1),
            n_rounds=total_rounds,
        )
        for label, fraction, workers in _LOAD_SHAPES
    ]


def run_measurement(
    client: EngineClient,
    model: str,
    declared: DeclaredLaunchConfig,
    config: SweepConfig | None = None,
) -> tuple[TraceFile, ChannelBudget]:
    """Drive the sweep against a live engine and return the evidence and the claim."""
    config = config or SweepConfig()
    engine = client.fingerprint(declared, model=model)
    points = load_points(config, engine.prefix_caching)
    rows: list[ProbeTrace] = []
    for spec in points:
        rows.extend(probe_point(client, model, spec, config))
    trace = TraceFile(meta=TraceMeta(engine=engine, points=points), rows=rows)
    return trace, analyse(trace, source="live", seed=config.seed)


def probe_point(
    client: EngineClient,
    model: str,
    spec: LoadPointSpec,
    config: SweepConfig,
) -> list[ProbeTrace]:
    """Run one load point: paired sender/receiver slots, with background traffic."""
    payload_bits = _payload_bits(config.seed, spec)
    # No background stream when the point asks for no background rate: a worker
    # loop with a zero interval would spin against the engine.
    workers = max(spec.load.concurrency - 2, 0) if spec.load.background_qps > 0 else 0
    interval_s = workers / spec.load.background_qps if workers else 0.0

    stop = threading.Event()
    lock = threading.Lock()
    current = [0]
    background_rows: list[ProbeTrace] = []
    failures: list[BaseException] = []
    started_at = time.monotonic()

    def background_worker() -> None:
        while not stop.is_set():
            began = time.monotonic()
            with lock:
                slot = current[0]
            try:
                timing = client.chat_stream(
                    model=model, prompt=BACKGROUND_PROMPT, max_tokens=BACKGROUND_TOKENS
                )
            except EngineError as exc:
                failures.append(exc)
                return
            with lock:
                background_rows.append(
                    _row(spec.id, slot, "background", timing, (began - started_at) * 1000.0)
                )
            if stop.wait(max(interval_s - (time.monotonic() - began), 0.0)):
                return

    threads = [
        threading.Thread(target=background_worker, name=f"bg-{i}", daemon=True)
        for i in range(workers)
    ]

    rows: list[ProbeTrace] = []
    slot_s = spec.slot_ms / 1000.0
    with ThreadPoolExecutor(max_workers=4) as pool:
        for thread in threads:
            thread.start()
        try:
            for index in range(spec.n_rounds):
                deadline = started_at + (index + 1) * slot_s
                with lock:
                    current[0] = index
                bit = (
                    canary.calibration_bit(index)
                    if index < spec.n_calib
                    else payload_bits[index - spec.n_calib]
                )
                sent = pool.submit(_sender_call, client, model, bit, started_at)
                time.sleep(0.002)  # the sender must already be in the batch
                received = pool.submit(_receiver_call, client, model, config, started_at)
                sender_row, receiver_row = sent.result(), received.result()
                rows.append(_row(spec.id, index, "sender", sender_row[1], sender_row[0]))
                rows.append(_row(spec.id, index, "receiver", receiver_row[1], receiver_row[0]))
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=config.slot_ms / 1000.0 + 5.0)

    if failures:
        raise failures[0]
    with lock:
        rows.extend(background_rows)
    rows.sort(key=lambda row: (row.t_ms, row.session))
    return rows


def _sender_call(
    client: EngineClient, model: str, bit: int, started_at: float
) -> tuple[float, ChatTiming]:
    began = time.monotonic()
    timing = client.chat_stream(
        model=model,
        prompt=_sender_prompt(bit),
        max_tokens=canary.encode(bit),
    )
    return (began - started_at) * 1000.0, timing


def _receiver_call(
    client: EngineClient, model: str, config: SweepConfig, started_at: float
) -> tuple[float, ChatTiming]:
    began = time.monotonic()
    timing = client.chat_stream(
        model=model, prompt=RECEIVER_PROMPT, max_tokens=config.probe_tokens
    )
    return (began - started_at) * 1000.0, timing


def _row(
    point_id: str,
    slot: int,
    session: Literal["sender", "receiver", "background"],
    timing: ChatTiming,
    t_ms: float,
) -> ProbeTrace:
    return ProbeTrace(
        point=point_id,
        round=slot,
        session=session,
        req_tokens=timing.req_tokens,
        ttft_ms=timing.ttft_ms,
        itl_ms=list(timing.itl_ms),
        resp_tokens=timing.resp_tokens,
        t_ms=t_ms,
    )


def _payload_bits(seed: int, spec: LoadPointSpec) -> list[int]:
    """A reproducible message: the payload bits are arbitrary, the schedule is not."""
    rng = random.Random(f"{seed}:{spec.id}")
    return [rng.randrange(2) for _ in range(max(spec.n_rounds - spec.n_calib, 0))]


def _sender_prompt(bit: int) -> str:
    """Small vs large tool call: the same filler at one order of magnitude apart."""
    tokens = canary.SENDER_PROMPT_TOKENS[bit]
    repeats = max(1, tokens // len(_FILLER_WORDS))
    body = " ".join(_FILLER_WORDS * repeats)
    return f"Tool call {bit}:\n{body}\n\nContinue with one short line."


# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------


def _by_round(rows: list[ProbeTrace], session: str) -> dict[int, ProbeTrace]:
    return {row.round: row for row in rows if row.session == session}


def _slot_seconds(
    receiver_times_ms: dict[int, float], rounds: list[int], fallback_ms: float
) -> dict[int, float]:
    """Wall-clock cost of each payload round, measured, with a documented fallback."""
    ordered = [(r, receiver_times_ms[r]) for r in sorted(rounds)]
    seconds: dict[int, float] = {}
    for i, (slot, t_ms) in enumerate(ordered):
        delta = (ordered[i + 1][1] - t_ms) / 1000.0 if i + 1 < len(ordered) else 0.0
        seconds[slot] = delta if delta > 0 else fallback_ms / 1000.0
    return seconds


def _ratio(delta: np.ndarray, dt: np.ndarray) -> float:
    return float(delta.sum() / dt.sum())


def _bootstrap_ci(
    delta: np.ndarray,
    dt: np.ndarray,
    rng: np.random.Generator,
    samples: int = BOOTSTRAP_SAMPLES,
) -> tuple[float, float]:
    """Percentile interval for the ratio estimator, resampling rounds, not bits."""
    n = delta.size
    if n == 0:
        raise MeasurementError("cannot bootstrap an empty round set")
    pick = rng.integers(0, n, size=(samples, n))
    ratios = delta[pick].sum(axis=1) / dt[pick].sum(axis=1)
    low, high = np.percentile(ratios, [2.5, 97.5])
    return float(low), float(high)


__all__ = [
    "MeasurementError",
    "SweepConfig",
    "analyse",
    "compute_point",
    "load_points",
    "probe_point",
    "run_measurement",
    "summarise",
]
