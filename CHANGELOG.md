# Changelog

## 0.1.0 — 2026-11-26

First release. A CLI that measures the cross-session bit rate one inference
engine build admits, and writes the claim plus the evidence it came from.

### Added

- **Step-sizing canary** (`sideband/canary.py`): a bit is encoded as 4 or 32
  decode steps, the receiver decodes from its own inter-token latencies against
  a threshold calibrated on known-bit rounds. Ground truth from the sender row
  is kept separate from the receiver-side decoder.
- **Measurement pipeline** (`sideband/measure.py`): three-point load sweep
  (idle, 0.5x, 1.0x), ratio estimator over round slots, 2000-sample bootstrap
  interval over rounds, shuffled-bit permutation null, and the three-way
  `open` / `closed` / `inconclusive` verdict with the noise floor it used.
- **Live and replay share one statistics path**: `sideband replay` calls the same
  `analyse()` as a live sweep, so a replay cannot read rosier than the run that
  produced the trace.
- **Engine fingerprint** (`sideband/engine.py`): read-back fields from
  `GET /version`, `GET /v1/models` and the `vllm:cache_config_info` series on
  `GET /metrics`; `max_num_seqs` and the launch command recorded as
  operator-declared. `/server_info` is never called.
- **Artifacts** (`sideband/report.py`): `channel_budget.json` and a one-page
  `channel_budget.md`, both carrying provenance (`live` / `replay`, synthetic or
  recorded, read-back or authored fingerprint) and the round slot.
- **Provenance**: a budget derived from a generated trace is labelled synthetic
  in both artifacts, and a replayed timestamp is never called "measured".
- **CLI** (`sideband measure`, `sideband replay`, `sideband fingerprint`).
- **GPU-less demo**: `examples/trace-vllm-qwen05b.jsonl`, `scripts/demo.sh`,
  `assets/demo.tape`, and a recorded `assets/demo.cast`.
- **`docs/method.md`**: encoder, calibration, ground truth, statistics, load
  sweep, fingerprint boundaries, limits, and the synthetic-fixture statement.
- **Bilingual README** (`README.md`, `README.zh-CN.md`).
- **CI and release workflows**: tests on push and pull request; tagged releases
  build a wheel and sdist, checksum them, and upload them as GitHub Release
  assets.

### Known limits

- The bundled example trace is synthetic; this repository contains no live
  measurement of a real engine.
- One encoder only. KV-cache reuse timing, kernel fingerprinting and
  token-length probing are not implemented.
- The fingerprint cannot read scheduler configuration from the public HTTP
  surface, and does not fingerprint batch invariance at all.
- It measures and reports; it does not mitigate, judge compliance, or bundle an
  engine.
