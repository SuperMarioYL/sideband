"""Replay: a recorded probe trace through the identical measurement pipeline.

This module is deliberately thin. The value of ``sideband replay`` is that it
calls :func:`sideband.measure.analyse` — the same function a live run calls after
its own probe loop — on rows that came from a file instead of a socket. Put any
extra arithmetic here and replay would start to drift from what a live run means.

No engine, no GPU, no network: a trace is JSON lines on disk.
"""

from __future__ import annotations

from pathlib import Path

from .measure import DEFAULT_SEED, analyse
from .models import ChannelBudget, TraceFile


def load(path: str | Path) -> TraceFile:
    """Read a trace file and check it carries the provenance a budget needs."""
    return TraceFile.load(path)


def replay(path: str | Path | TraceFile, *, seed: int = DEFAULT_SEED) -> ChannelBudget:
    """Recompute the channel budget for a recorded trace."""
    trace = path if isinstance(path, TraceFile) else load(path)
    return analyse(trace, seed=seed)
