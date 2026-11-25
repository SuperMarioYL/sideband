"""Synthetic traces in, known bitrates and the right verdict out."""

from __future__ import annotations

import json
import os
import random
from datetime import datetime, timezone

import httpx
import numpy as np
import pytest

from sideband import canary, report
from sideband.engine import EngineClient
from sideband.measure import (
    MeasurementError,
    SweepConfig,
    analyse,
    compute_point,
    run_measurement,
    summarise,
)
from sideband.models import (
    ChannelBudget,
    DeclaredLaunchConfig,
    EngineFingerprint,
    LoadPointSpec,
    LoadSpec,
    ProbeTrace,
    SweepPoint,
    TraceFile,
    TraceMeta,
    Verdict,
)
from sideband.replay import replay

SLOT_MS = 300.0
N_CALIB = 8
N_PAYLOAD = 40
BASE_MS = 10.0


def build_trace(
    separations: dict[str, float],
    *,
    noise_ms: float = 0.0,
    seed: int = 7,
    synthetic: bool = False,
) -> TraceFile:
    """A trace whose receiver step time moves by ``separations[point]`` on a 1 bit.

    No noise and a wide separation decodes every bit; no separation at all makes
    the receiver blind, which is the closed-channel case.
    """
    rng = random.Random(seed)
    specs: list[LoadPointSpec] = []
    rows: list[ProbeTrace] = []
    # A balanced, shuffled message: arbitrary bits, both classes represented, so a
    # blind receiver must land on zero net bits rather than a lucky imbalance.
    payload = [0, 1] * (N_PAYLOAD // 2)
    rng.shuffle(payload)
    for point_id, separation in separations.items():
        specs.append(
            LoadPointSpec(
                id=point_id,
                load=LoadSpec(concurrency=2, background_qps=0.0, prefix_cache=True),
                slot_ms=SLOT_MS,
                n_calib=N_CALIB,
                n_rounds=N_CALIB + N_PAYLOAD,
            )
        )
        for index in range(N_CALIB + N_PAYLOAD):
            bit = index % 2 if index < N_CALIB else payload[index - N_CALIB]
            score = BASE_MS + separation * bit + rng.gauss(0.0, noise_ms)
            rows.append(
                ProbeTrace(
                    point=point_id,
                    round=index,
                    session="sender",
                    req_tokens=canary.SENDER_PROMPT_TOKENS[bit],
                    ttft_ms=4.0,
                    itl_ms=[BASE_MS] * max(canary.encode(bit) - 1, 1),
                    resp_tokens=canary.encode(bit),
                    t_ms=index * SLOT_MS + 0.5,
                )
            )
            rows.append(
                ProbeTrace(
                    point=point_id,
                    round=index,
                    session="receiver",
                    req_tokens=6,
                    ttft_ms=5.0,
                    itl_ms=[score] * 5,
                    resp_tokens=6,
                    t_ms=index * SLOT_MS,
                )
            )
    meta = TraceMeta(
        engine=EngineFingerprint(
            name="vllm",
            version="0.11.0",
            served_model="Qwen/Qwen2.5-0.5B-Instruct",
            max_model_len=32768,
            prefix_caching=True,
            declared=DeclaredLaunchConfig(
                max_num_seqs=8, launch_cmd="vllm serve Qwen/Qwen2.5-0.5B-Instruct"
            ),
        ),
        points=specs,
        synthetic=synthetic,
        created_at=datetime(2026, 11, 20, tzinfo=timezone.utc),
    )
    return TraceFile(meta=meta, rows=rows)


def point(pid: str, bits: float, ci: tuple[float, float], *, null_bits: float = 0.0,
          null_ci: tuple[float, float] = (-0.4, 0.4)) -> SweepPoint:
    return SweepPoint(
        id=pid,
        load=LoadSpec(concurrency=2, background_qps=0.0, prefix_cache=True),
        bits_per_session_second=bits,
        ci95=ci,
        rounds=N_PAYLOAD,
        decode_errors=0,
        null_bits_per_session_second=null_bits,
        null_ci95=null_ci,
    )


def test_compute_point_recovers_a_known_bitrate() -> None:
    trace = build_trace({"idle": 4.0})
    spec = trace.spec("idle")
    result = compute_point(spec, trace.rows_for("idle"))

    # One bit per 300 ms slot, every bit decoded: 1 / 0.3 bits per session second.
    assert result.bits_per_session_second == pytest.approx(1.0 / (SLOT_MS / 1000.0))
    assert result.decode_errors == 0
    assert result.rounds == N_PAYLOAD
    assert result.ci95[0] <= result.bits_per_session_second <= result.ci95[1]
    assert abs(result.null_bits_per_session_second) < 1.0


def test_open_verdict_when_the_sender_moves_step_times() -> None:
    trace = build_trace({"idle": 4.0, "0.5x": 1.6, "1.0x": 0.7}, noise_ms=0.9, seed=3)
    budget = analyse(trace)

    assert budget.verdict is Verdict.open
    assert "rises above" in budget.verdict_reason
    rates = [p.bits_per_session_second for p in budget.points]
    assert rates[0] > rates[-1], rates  # the channel collapses under load
    assert all(p.rounds == N_PAYLOAD for p in budget.points)
    assert budget.channel.rounds == 3 * N_PAYLOAD


def test_closed_verdict_when_the_receiver_is_blind() -> None:
    # No separation at all: the receiver's score is the same in both classes, the
    # threshold lands on it, and the decode carries no information.
    trace = build_trace({"idle": 0.0, "0.5x": 0.0, "1.0x": 0.0})
    budget = analyse(trace)

    assert budget.verdict is Verdict.closed
    assert "no measurable step-sizing channel" in budget.verdict_reason
    for sweep_point in budget.points:
        assert sweep_point.bits_per_session_second <= max(
            p.null_ci95[1] for p in budget.points
        )


def test_summarise_three_way_rule() -> None:
    assert summarise([point("idle", 3.1, (2.6, 3.6))])[0] is Verdict.open
    assert summarise([point("idle", 0.05, (-0.3, 0.4))])[0] is Verdict.closed
    # Above the floor, but the interval still reaches zero: not separated.
    assert summarise([point("idle", 0.7, (-0.1, 1.5))])[0] is Verdict.inconclusive
    # One open point is enough, even when a slower load reads at the floor.
    mixed = [point("idle", 3.1, (2.6, 3.6)), point("1.0x", 0.2, (-0.2, 0.6))]
    verdict, reason = summarise(mixed)
    assert verdict is Verdict.open
    assert "1 of 2 load points read open" in reason


def test_summarise_rejects_an_empty_table() -> None:
    with pytest.raises(MeasurementError):
        summarise([])


def test_engine_fingerprint_uses_only_the_public_http_surface() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/version":
            return httpx.Response(200, json={"version": "0.11.0"})
        if request.url.path == "/v1/models":
            return httpx.Response(
                200, json={"data": [{"id": "Qwen/Qwen2.5-0.5B-Instruct", "max_model_len": 32768}]}
            )
        if request.url.path == "/metrics":
            return httpx.Response(
                200,
                text='vllm:cache_config_info{block_size="16",enable_prefix_caching="True"} 1.0\n',
            )
        return httpx.Response(404)

    declared = DeclaredLaunchConfig(max_num_seqs=8, launch_cmd="vllm serve ...")
    client = EngineClient("http://127.0.0.1:8000/v1", client=httpx.Client(transport=httpx.MockTransport(handler)))
    engine = client.fingerprint(declared, model="Qwen/Qwen2.5-0.5B-Instruct")

    assert engine.version == "0.11.0"
    assert engine.served_model == "Qwen/Qwen2.5-0.5B-Instruct"
    assert engine.max_model_len == 32768
    assert engine.prefix_caching is True
    assert engine.declared.max_num_seqs == 8
    assert set(seen) == {"/version", "/v1/models", "/metrics"}
    assert all("/server_info" not in path for path in seen)
    assert engine.unavailable() == []


def test_unobtainable_read_back_fields_are_explicit() -> None:
    client = EngineClient(
        "http://127.0.0.1:8000/v1",
        client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404))),
    )
    engine = client.fingerprint(DeclaredLaunchConfig(max_num_seqs=1, launch_cmd="vllm serve ..."))
    assert engine.readback() == {
        "version": None,
        "served_model": None,
        "max_model_len": None,
        "prefix_caching": None,
    }
    assert engine.unavailable() == list(EngineFingerprint.READBACK_FIELDS)
    assert engine.declared.max_num_seqs == 1


def test_budget_json_round_trips_through_the_model() -> None:
    budget = analyse(build_trace({"idle": 4.0, "1.0x": 1.0}, noise_ms=0.8, seed=11))
    parsed = ChannelBudget.model_validate(json.loads(report.budget_json(budget)))
    assert parsed.verdict is budget.verdict
    assert parsed.points == budget.points
    assert parsed.engine.declared.max_num_seqs == 8

    markdown = report.markdown(budget)
    assert "## Engine fingerprint" in markdown
    assert "| load | bits/session/s | ci95 | verdict |" in markdown
    assert budget.engine.served_model in markdown


def test_bundled_trace_replays_the_sweep() -> None:
    trace_path = os.path.join(os.path.dirname(__file__), "..", "examples", "trace-vllm-qwen05b.jsonl")
    trace = TraceFile.load(trace_path)

    assert trace.meta.synthetic is True
    assert [spec.id for spec in trace.meta.points] == ["idle", "0.5x", "1.0x"]
    assert len(trace.rows) >= 20

    budget = replay(trace)
    assert budget.verdict is Verdict.open
    rates = [p.bits_per_session_second for p in budget.points]
    assert rates[0] > rates[1] > rates[2], rates
    assert budget.engine.version == "0.11.0"
    assert budget.engine.declared.max_num_seqs == 8
    # Same rows in, same numbers out: replay must be deterministic.
    assert replay(trace).points == budget.points


def test_run_measurement_wires_the_probe_loop() -> None:
    """The live path end to end over a mock engine: rows, specs, budget, no GPU."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/version":
            return httpx.Response(200, json={"version": "0.11.0"})
        if path == "/v1/models":
            return httpx.Response(
                200, json={"data": [{"id": "Qwen/Qwen2.5-0.5B-Instruct", "max_model_len": 2048}]}
            )
        if path == "/v1/chat/completions":
            body = json.loads(request.read())
            chunks = max(int(body.get("max_tokens", 1)), 1)
            events = [
                "data: " + json.dumps({"choices": [{"delta": {"content": "x"}}]})
                for _ in range(chunks)
            ]
            events.append("data: [DONE]")
            return httpx.Response(
                200,
                text="\n\n".join(events) + "\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(404)

    engine = httpx.Client(transport=httpx.MockTransport(handler))
    with EngineClient("http://127.0.0.1:8000/v1", client=engine) as client:
        trace, budget = run_measurement(
            client,
            "Qwen/Qwen2.5-0.5B-Instruct",
            DeclaredLaunchConfig(max_num_seqs=8, launch_cmd="vllm serve ..."),
            SweepConfig(seconds=1.0, slot_ms=20.0, calib_rounds=4, baseline_qps=0.0),
        )

    assert [spec.id for spec in trace.meta.points] == ["idle", "0.5x", "1.0x"]
    assert {row.session for row in trace.rows} == {"sender", "receiver"}
    assert len(trace.rows) == 2 * sum(spec.n_rounds for spec in trace.meta.points)
    assert budget.engine.version == "0.11.0"
    assert budget.verdict in tuple(Verdict)
    assert all(point.rounds > 0 for point in budget.points)


def test_live_measurement_is_skipped_without_an_endpoint() -> None:
    endpoint = os.environ.get("SIDEBAND_TEST_ENDPOINT")
    if not endpoint:
        pytest.skip(
            "no engine available: set SIDEBAND_TEST_ENDPOINT to a running OpenAI-compatible "
            "server (e.g. http://127.0.0.1:8000/v1) to exercise the live probe loop"
        )

    model = os.environ.get("SIDEBAND_TEST_MODEL", "Qwen/Qwen2.5-0.5B-Instruct")
    with EngineClient(endpoint) as client:
        trace, budget = run_measurement(
            client,
            model,
            DeclaredLaunchConfig(max_num_seqs=8, launch_cmd="vllm serve ..."),
            SweepConfig(seconds=30.0, calib_rounds=8),
        )
    assert len(budget.points) == 3
    assert trace.meta.engine.served_model is not None
    assert np.isfinite(budget.points[0].bits_per_session_second)
