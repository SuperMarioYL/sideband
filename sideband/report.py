"""Render a channel budget: the JSON record, the one-page markdown, the terminal table."""

from __future__ import annotations

import json
from pathlib import Path

from rich.table import Table

from .canary import SENDER_STEPS
from .models import ChannelBudget, SweepPoint

JSON_NAME = "channel_budget.json"
MARKDOWN_NAME = "channel_budget.md"


def budget_json(budget: ChannelBudget) -> str:
    return json.dumps(budget.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"


def load_name(budget: ChannelBudget) -> str:
    engine = budget.engine
    model = engine.served_model or "unknown model"
    if engine.version:
        return f"{model} on {engine.name} {engine.version}"
    return f"{model} on {engine.name} (no version reported)"


def table(budget: ChannelBudget) -> Table:
    """The sweep as a terminal table, in the shape the budget's own table takes."""
    rendered = Table(box=None, pad_edge=False, header_style="bold")
    rendered.add_column("load")
    rendered.add_column("bits/session/s", justify="right")
    rendered.add_column("ci95", justify="right")
    rendered.add_column("verdict")
    for index, point in enumerate(budget.points):
        rendered.add_row(
            point.id,
            f"{point.bits_per_session_second:.1f}",
            _interval(point),
            budget.verdict.value if index == 0 else "",
        )
    return rendered


def provenance_note(budget: ChannelBudget) -> str:
    """One sentence: where the rows and the fingerprint came from.

    This is the part a reader (or a screenshot of the markdown) must not have to
    infer, so it is written into the artifacts, not printed to a terminal only.
    """
    provenance = budget.provenance
    if provenance.source == "live":
        note = (
            "Provenance: live — the probe rows were recorded from a live engine in this run."
        )
        if provenance.trace_path:
            note += f" Evidence written to {provenance.trace_path}."
    else:
        origin = provenance.trace_path or "an in-memory trace"
        note = f"Provenance: replay — the probe rows were replayed from {origin}."
    if provenance.synthetic:
        note += (
            " The trace is marked synthetic: its rows were generated, not recorded from "
            "a running engine."
        )
    if provenance.fingerprint_source == "authored":
        note += (
            " The engine fingerprint below was authored in the trace; nothing in this "
            "document was read back from /version, /v1/models or /metrics."
        )
    else:
        note += " The engine fingerprint below is what the engine's HTTP surface served."
    return note


def slot_line(budget: ChannelBudget) -> str:
    """The round duration, so the headline rate can be divided back out by hand."""
    bits = budget.channel.bits_per_round
    slots = sorted({round(point.slot_ms, 3) for point in budget.points})
    if len(slots) == 1:
        slot_ms = slots[0]
        return (
            f"Each round occupies a {slot_ms:.0f} ms slot, so {bits} bit per slot caps the "
            f"rate at {bits / (slot_ms / 1000.0):.1f} bits/session/s; the figures above are "
            "what the receiver actually recovered."
        )
    listing = " · ".join(f"{point.id} {point.slot_ms:.0f} ms" for point in budget.points)
    return f"Round slot per load point: {listing}."


def fingerprint_line(budget: ChannelBudget) -> str:
    """One line, the same facts the terminal and the markdown both print."""
    engine = budget.engine
    read_back = ", ".join(
        f"{name}={value}" for name, value in engine.readback().items() if value is not None
    )
    declared = ", ".join(
        f"{key}={value}" for key, value in engine.declared.model_dump().items() if value is not None
    )
    missing = engine.unavailable()
    origin = "authored" if budget.provenance.fingerprint_source == "authored" else "read back"
    parts = [f"{origin}: {read_back or 'nothing'}"]
    if declared:
        parts.append(f"declared: {declared}")
    if missing:
        parts.append(f"not served: {', '.join(missing)}")
    return f"{engine.name} · " + " · ".join(parts)


def markdown(budget: ChannelBudget) -> str:
    """One page: provenance, the table, the verdict, the fingerprint, the method."""
    lines = [
        f"# Channel budget — {load_name(budget)}",
        "",
        f"> {provenance_note(budget)}",
        "",
        f"**Verdict: {budget.verdict.value}** — {budget.verdict_reason}.",
        "",
        "| load | bits/session/s | ci95 | verdict |",
        "| --- | --- | --- | --- |",
    ]
    for index, point in enumerate(budget.points):
        lines.append(
            f"| {point.id} | {point.bits_per_session_second:.1f} | {_interval(point)} "
            f"| {budget.verdict.value if index == 0 else ''} |"
        )
    lines.extend(
        [
            "",
            slot_line(budget),
            "",
            (
                f"encoder `{budget.channel.encoder}` · "
                f"{budget.channel.bits_per_round} bit per round · "
                f"{budget.channel.rounds} payload rounds · "
                f"{budget.channel.decode_errors} decode errors · "
                f"shuffled-bit null {budget.channel.null_baseline:.1f} bits/session/s"
            ),
            "",
            "## Engine fingerprint",
            "",
        ]
    )
    if budget.provenance.fingerprint_source == "authored":
        lines.append(
            "Authored in the trace — **not** read back over HTTP "
            "(`/version`, `/v1/models`, `/metrics`):"
        )
    else:
        lines.append("Read back over HTTP (`/version`, `/v1/models`, `/metrics`):")
    lines.extend(
        [
            "",
            "| field | value |",
            "| --- | --- |",
        ]
    )
    for name, value in budget.engine.readback().items():
        lines.append(f"| `{name}` | {_cell(value, '_not served_')} |")
    lines.extend(
        [
            "",
            "Operator-declared, because no production route serves it:",
            "",
        ]
    )
    declared = budget.engine.declared
    lines.append(f"- `max_num_seqs`: {_cell(declared.max_num_seqs, '_not declared_')}")
    lines.append(f"- `launch_cmd`: {_cell(declared.launch_cmd, '_not declared_')}")
    if declared.max_num_seqs is None:
        lines.append("")
        lines.append(
            "> `max_num_seqs` was not declared, so the number cannot be attributed to a "
            "launch command. Re-run with `--declare-max-num-seqs` matching the engine's "
            "`--max-num-seqs`."
        )
    lines.extend(
        [
            "",
            "## Method",
            "",
            (
                "Two sessions share the engine and never open a socket to each other. "
                f"Each round the sender occupies either {SENDER_STEPS[0]} or "
                f"{SENDER_STEPS[1]} decode steps of the batch; the receiver decodes a bit "
                "from its own inter-token latencies against a threshold calibrated on "
                "known-bit rounds. Throughput is correct-minus-wrong payload bits over the "
                "wall-clock slots they occupied; `ci95` is a bootstrap over rounds; the "
                "null keeps the decoded bits and permutes the ground-truth bits across "
                "rounds, and must read about zero."
            ),
            "",
            _stamp(budget),
            "",
        ]
    )
    return "\n".join(lines)


def _stamp(budget: ChannelBudget) -> str:
    """Which run this document reports, without calling a replay a measurement."""
    when = budget.measured_at.isoformat()
    if budget.provenance.source == "live":
        return f"`sideband {budget.sideband_version}` · measured {when}"
    origin = budget.provenance.trace_path or "an in-memory trace"
    if budget.provenance.synthetic:
        return (
            f"`sideband {budget.sideband_version}` · replayed from {origin} · "
            f"synthetic fixture stamped {when}"
        )
    return (
        f"`sideband {budget.sideband_version}` · replayed from {origin} · "
        f"trace recorded {when}"
    )


def write(budget: ChannelBudget, out_dir: str | Path) -> tuple[Path, Path]:
    """Write both artifacts and return their paths, so callers can print them."""
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / JSON_NAME
    markdown_path = directory / MARKDOWN_NAME
    json_path.write_text(budget_json(budget), encoding="utf-8")
    markdown_path.write_text(markdown(budget), encoding="utf-8")
    return json_path, markdown_path


def _interval(point: SweepPoint) -> str:
    return f"{point.ci95[0]:.1f}-{point.ci95[1]:.1f}"


def _cell(value: object, placeholder: str) -> str:
    return placeholder if value is None else f"`{value}`"
