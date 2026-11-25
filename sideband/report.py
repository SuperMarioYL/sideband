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
    parts = [f"read back: {read_back or 'nothing'}"]
    if declared:
        parts.append(f"declared: {declared}")
    if missing:
        parts.append(f"not served: {', '.join(missing)}")
    return f"{engine.name} · " + " · ".join(parts)


def markdown(budget: ChannelBudget) -> str:
    """One page: the table, the verdict, the fingerprint, the method."""
    lines = [
        f"# Channel budget — {load_name(budget)}",
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
            (
                f"encoder `{budget.channel.encoder}` · "
                f"{budget.channel.bits_per_round} bit per round · "
                f"{budget.channel.rounds} payload rounds · "
                f"{budget.channel.decode_errors} decode errors · "
                f"shuffled-label null {budget.channel.null_baseline:.1f} bits/session/s"
            ),
            "",
            "## Engine fingerprint",
            "",
            "Read back over HTTP (`/version`, `/v1/models`, `/metrics`):",
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
                "shuffled-label null repeats the decode with ground-truth bits permuted "
                "and must read about zero."
            ),
            "",
            f"`sideband {budget.sideband_version}` · measured {budget.measured_at.isoformat()}",
            "",
        ]
    )
    return "\n".join(lines)


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
