"""Command line: ``sideband measure``, ``sideband replay``, ``sideband fingerprint``."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from . import __version__, report
from .canary import CalibrationError
from .engine import EngineClient, EngineError
from .measure import MeasurementError, SweepConfig, run_measurement
from .models import ChannelBudget, DeclaredLaunchConfig
from .replay import replay as replay_trace

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Measure how many bits per session per second cross between two sessions "
    "sharing one LLM inference server.",
)
console = Console()


@app.callback(invoke_without_command=True)
def main(
    version: bool = typer.Option(
        False, "--version", help="Print the version and exit.", is_eager=True
    ),
) -> None:
    """Sideband — the covert-channel meter for sessions that share one inference server."""
    if version:
        console.print(f"sideband {__version__}")
        raise typer.Exit()


@app.command()
def measure(
    endpoint: str = typer.Option(
        ...,
        "--endpoint",
        "-e",
        help="OpenAI-compatible base URL, e.g. http://127.0.0.1:8000/v1",
    ),
    model: str = typer.Option(..., "--model", "-m", help="Model id the engine serves"),
    seconds: float = typer.Option(
        60.0, "--seconds", min=1.0, help="Wall-clock target for the whole three-point sweep"
    ),
    declare_max_num_seqs: int | None = typer.Option(
        None,
        "--declare-max-num-seqs",
        min=1,
        help="The engine's --max-num-seqs. Declared, because no production route serves it.",
    ),
    launch_cmd: str | None = typer.Option(
        None, "--launch-cmd", help="Exact command used to launch the engine, for attribution"
    ),
    slot_ms: float = typer.Option(300.0, "--slot-ms", min=50.0, help="Wall-clock budget per round"),
    baseline_qps: float = typer.Option(
        4.0, "--baseline-qps", min=0.0, help="Background request rate the 1.0x load point hits"
    ),
    trace_out: Path | None = typer.Option(
        None, "--trace-out", help="Write the probe trace JSONL here so the run can be replayed"
    ),
    out: Path = typer.Option(Path("."), "--out", help="Directory for channel_budget.json / .md"),
) -> None:
    """Drive the canary against a live engine and write a channel budget."""
    declared = DeclaredLaunchConfig(max_num_seqs=declare_max_num_seqs, launch_cmd=launch_cmd)
    if declare_max_num_seqs is None:
        console.print(
            "[yellow]warning[/yellow] --declare-max-num-seqs not given: the number is measured "
            "but not attributable to a launch config."
        )
    config = SweepConfig(seconds=seconds, slot_ms=slot_ms, baseline_qps=baseline_qps)
    try:
        with EngineClient(endpoint) as client:
            trace, budget = run_measurement(client, model, declared, config)
    except (EngineError, MeasurementError, CalibrationError) as exc:
        _fail(str(exc))
        return
    if trace_out is not None:
        trace.save(trace_out)
        # The budget is written below, so its provenance records where the
        # evidence for this run went.
        budget.provenance.trace_path = str(trace_out)
        console.print(f"trace written to {trace_out}", style="dim")
    _emit(budget, out)


@app.command()
def replay(
    trace: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        help="Recorded probe trace (JSON lines)",
    ),
    out: Path = typer.Option(Path("."), "--out", help="Directory for channel_budget.json / .md"),
) -> None:
    """Recompute the same table from a recorded trace. No engine, no GPU, no network."""
    try:
        budget = replay_trace(trace)
    except (ValueError, MeasurementError, CalibrationError) as exc:
        _fail(str(exc))
        return
    _emit(budget, out)


@app.command()
def fingerprint(
    endpoint: str = typer.Option(
        ..., "--endpoint", "-e", help="OpenAI-compatible base URL, e.g. http://127.0.0.1:8000/v1"
    ),
    model: str | None = typer.Option(None, "--model", "-m", help="Select this model if several"),
    declare_max_num_seqs: int | None = typer.Option(
        None, "--declare-max-num-seqs", min=1, help="The engine's --max-num-seqs"
    ),
    launch_cmd: str | None = typer.Option(
        None, "--launch-cmd", help="Exact command used to launch the engine"
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the fingerprint as JSON"),
) -> None:
    """Show what the engine's public HTTP surface reports, and what it does not."""
    declared = DeclaredLaunchConfig(max_num_seqs=declare_max_num_seqs, launch_cmd=launch_cmd)
    try:
        with EngineClient(endpoint) as client:
            engine = client.fingerprint(declared, model=model)
    except EngineError as exc:
        _fail(str(exc))
        return

    if as_json:
        console.print_json(data=engine.model_dump(mode="json"))
        return
    console.print(f"engine: {engine.name}", style="bold")
    for name, value in engine.readback().items():
        console.print(f"  {name}: {'(not served)' if value is None else value}")
    console.print(f"  declared.max_num_seqs: {engine.declared.max_num_seqs}")
    console.print(f"  declared.launch_cmd: {engine.declared.launch_cmd}")
    missing = engine.unavailable()
    if missing:
        console.print(
            f"not served by this engine: {', '.join(missing)} — those fields cannot be "
            "attributed from the HTTP surface.",
            style="dim",
        )


def _emit(budget: ChannelBudget, out: Path) -> None:
    console.print(report.load_name(budget), style="bold")
    console.print(report.provenance_note(budget))
    console.print(report.table(budget))
    console.print(f"verdict: [bold]{budget.verdict.value}[/bold] — {budget.verdict_reason}")
    console.print(report.fingerprint_line(budget), style="dim")
    json_path, markdown_path = report.write(budget, out)
    console.print(f"wrote {json_path} and {markdown_path}", style="dim")


def _fail(message: str) -> None:
    console.print(f"error: {message}", style="red")
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
