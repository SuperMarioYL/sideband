# Method

This page is the citable description of what `sideband measure` and
`sideband replay` compute: the encoder, the calibration, the load sweep, the
statistics, what the fingerprint can and cannot read over the public HTTP
surface, and the limits of the number. It is written to be footnoted from a
review, so it states where each value comes from and which values this
repository never measured.

Sideband answers one question: **how many bits per session per second can two
sessions on the same inference engine exchange, given that neither opens a
socket to the other?** It prints a rate, a bootstrap interval and a verdict. It
does not judge whether that rate is acceptable.

## 1. The mechanism being measured

Continuous batching puts the requests that are in flight into the same engine
step. A request that is still generating keeps the batch wide; a request that
has finished does not. Step-sizing uses that difference:

1. The **sender** chooses how many decode steps it occupies in the batch — 4
   steps for a `0` bit, 32 steps for a `1` bit.
2. A **receiver** that is generating at the same time sees its own step time
   move with the width of the batch.
3. The receiver compares its own mean inter-token latency against a threshold
   and writes down a bit.

The sender and receiver never share a socket, a file or an API call. The only
shared resource is the engine's batch. Background requests are also issued, so
the measurement is not made on a quiet server by construction — but background
rows are recorded, never decoded.

Roles are kept apart in the code. `canary.encode(bit)` is the sender's step
count. `canary.bit_from_sender(row)` recovers the bit that was actually sent and
is used only for calibration, scoring and the null control.
`canary.calibrate` and `canary.decode` see receiver timings only. The decoder
never reads a sender row, because that would measure the harness rather than
the channel.

## 2. Encoder

| parameter | bit 0 | bit 1 | where |
| --- | --- | --- | --- |
| sender decode steps | 4 | 32 | `sideband/canary.py` `SENDER_STEPS` |
| sender prompt tokens (approx.) | 16 | 1024 | `SENDER_PROMPT_TOKENS` |
| bits per round | 1 | 1 | `BITS_PER_ROUND` |
| default round slot | 300 ms | 300 ms | `SweepConfig.slot_ms` |

The prompt size changes too, so the bit moves prefill pressure as well as decode
width. That is a property of the encoder, not a second channel: both come from
the same request.

One bit occupies one round, and a round occupies one slot. The arithmetic
ceiling is therefore `bits_per_round / slot_seconds` — **3.3 bits/session/s at
the default 300 ms slot**. Every published figure is a fraction of that ceiling,
and `channel_budget.md` prints the ceiling next to the table so the rate can be
divided back out by hand. Change the slot and the ceiling changes with it; the
rate is a cadence, not a property of the GPU alone.

## 3. Calibration

The first `n_calib` rounds of each load point carry known bits on an alternating
schedule (0, 1, 0, 1, …). They are not payload; they are the two sessions
agreeing a decision boundary before the exchange. The default is 12 rounds.

The threshold is the midpoint between the median receiver step time of the
calibration rounds that carried a 0 and the median of those that carried a 1:

```
threshold = (median(step_time | bit=0) + median(step_time | bit=1)) / 2
```

Calibration fails loudly with `CalibrationError` if either class is missing. A
receiver whose step times do not move between the two classes ends up with a
threshold in the middle of its own noise, which is the intended behaviour: it
then decodes at chance and the null control shows it.

## 4. Ground truth

Ground truth for a payload round is recovered from the sender's own row: a
response of at least 18 content chunks counts as the 32-step bit, anything less
as the 4-step bit. The midpoint sits between the two encoder values rather than
at either boundary.

This inference is deliberate and conservative. A live model that stops early
under the sender prompt relabels a `1` as a `0`, which can raise
`CalibrationError` or attenuate the measured rate; it cannot inflate the table.
Whether a particular 0.5B model reliably produces 32 chunks is a property of the
model, and this repository has no recorded live run to answer it for you.

`req_tokens` is an estimate (`(len(prompt)+3)//4`), not a tokenizer count. It is
descriptive metadata; the measurement uses step counts and timings, so an
estimate avoids depending on `stream_options.include_usage`, which not every
OpenAI-compatible server accepts.

## 5. Statistics

For each load point the analysis pairs sender and receiver rows by round, splits
calibration from payload, and computes one number:

```
rate = sum(+1 if decoded == truth else -1) / sum(slot_seconds)
```

Subtracting wrong bits makes the estimate a lower bound: a channel decoded at
chance lands near zero instead of reporting its raw round count.

* **Interval** — a percentile bootstrap with 2000 resamples over *rounds* (the
  unit that carries a bit), not over individual tokens. The default seed is
  fixed, so replaying a trace twice gives identical intervals.
* **Round cost** — the wall-clock gap between consecutive receiver request
  starts, falling back to the configured slot when the gap is non-positive.
  Both the per-point `slot_ms` and the derived rate are written to
  `channel_budget.json`.
* **Null control** — the receiver's decoded bits are kept and the ground-truth
  bit array is permuted across rounds. Sessions are never shuffled and the
  decode is never recomputed; only the bit-to-round pairing is broken. A
  channel read at chance must land near zero. The `null_baseline` field in the
  budget is the mean of the per-point null rates.
* **Noise floor** — the upper end of the null interval, taken across load
  points.

The verdict is then three-way:

| verdict | condition |
| --- | --- |
| `open` | at least one load point is above the noise floor *and* its interval excludes zero |
| `closed` | every load point is at or below the noise floor |
| `inconclusive` | above the floor somewhere, but no interval clears zero |

`closed` is a first-class result, not an error. It is what an engine launched
with `--max-num-seqs 1` should produce, and it is the useful answer for someone
deciding whether they need a second GPU.

## 6. Load sweep

One sweep is three points, driven in order:

| point | background rate | background streams | concurrency |
| --- | --- | --- | --- |
| `idle` | 0 qps | 0 | 2 |
| `0.5x` | 2 qps | 2 | 4 |
| `1.0x` | 4 qps | 4 | 6 |

The base background rate is `--baseline-qps` (default 4.0) and the fractions are
0, 0.5 and 1.0. Sender and receiver are always in flight, so concurrency is
background streams plus two. Background requests are short generations; their
rows are written to the trace with `session: "background"` and are never part of
the decode.

The total round count per point is `max(calib_rounds + 8, seconds * 1000 /
(slot_ms * 3))`, so `--seconds 60` with the default 300 ms slot yields 66 rounds
per point, 12 of them calibration.

A sweep is a curve, not a benchmark. Published rates for this class of channel
fall sharply under traffic, so a single idle figure is not representative; the
README does not quote one.

## 7. Engine fingerprint

The fingerprint is split on purpose.

**Read back over HTTP**, and only from these routes:

| field | route | note |
| --- | --- | --- |
| `version` | `GET /version` | vLLM serves it; other engines may return 404 |
| `served_model` | `GET /v1/models` | the entry matching `--model`, else the first |
| `max_model_len` | `GET /v1/models` | only if the server includes it |
| `prefix_caching` | `GET /metrics` | `vllm:cache_config_info{enable_prefix_caching="..."}` |

A 404 means the route does not exist on that engine; that is recorded as `None`
and reported as "not served", not treated as zero or false.

**Declared by the operator**, because no production route serves it:

* `max_num_seqs` (`--declare-max-num-seqs`)
* the launch command (`--launch-cmd`)

vLLM exposes scheduler configuration only from `GET /server_info`, which is
registered when `VLLM_SERVER_DEV_MODE=1`; that is a development mode, off by
default and documented as not for production. Sideband does not call it. The
response schema also declares a `system_fingerprint` field that is not
populated, so there is nothing to read there either.

**Not fingerprinted at all:** batch invariance. It exists as the startup
environment variable `VLLM_BATCH_INVARIANT` from vLLM 0.11.1, appears in no API
payload and in no configuration that `/server_info` dumps, and does not exist in
0.11.0. Sideband does not infer it from timings and does not guess it.

The consequence is documented in every artifact: a number is attributable to a
build only when the declared `max_num_seqs` and launch command are supplied. If
`max_num_seqs` is missing, `channel_budget.md` says so in its own section.

## 8. Limits

* **The rate is cadence-bounded.** One bit per slot caps the number at
  `1/slot_seconds`; comparing runs made with different slots compares different
  ceilings. The slot is recorded per point.
* **Load moves the number, and usually down.** The three-point sweep exists
  because an idle figure does not survive background traffic.
* **One encoder.** v0.1 measures step sizing only. KV-cache reuse timing, kernel
  fingerprinting and token-length probing are not implemented, and a `closed`
  verdict says nothing about those mechanisms.
* **Ground truth is inferred from realized chunk count**, so a model that
  truncates can only bias the rate downward (section 4).
* **The fingerprint is partial.** See section 7; absence of a field is recorded
  as absence, never inferred.
* **No mitigation, no compliance threshold.** Sideband measures; it does not
  patch kernels, isolate caches, change schedulers, or decide that *X* bits/s is
  unacceptable. The threshold is a conversation with your auditor.
* **No live evidence ships in this repository.** The bundled trace is synthetic;
  see section 9.
* **Single process, no daemon.** No Prometheus exporter, dashboard, hosted
  service or multi-user mode.

## 9. Synthetic fixture

`examples/trace-vllm-qwen05b.jsonl` is **authored, not measured**. Its meta line
carries `"synthetic": true`, and every artifact produced from it says so:

* the terminal provenance line says the rows were generated, not recorded;
* `channel_budget.json` records `provenance.synthetic: true` and
  `fingerprint_source: "authored"`;
* `channel_budget.md` prints "Authored in the trace — **not** read back over
  HTTP", and its stamp reads "synthetic fixture stamped …", never "measured".

The engine fields in that file (`vllm 0.11.0`, `Qwen/Qwen2.5-0.5B-Instruct`,
`max_model_len 32768`, `prefix_caching true`, `max_num_seqs 8`) were written
alongside the generated rows. They were not read from an engine. The row timings
were generated to make the pipeline produce a descending sweep, and the README's
table is the output of replaying that file — a demonstration that the pipeline
runs on a laptop without a GPU, not a reproduction of a measured run.

No live measurement of a real engine is included in this repository. The live
path exists, is exercised end to end against a mock OpenAI-compatible transport
in the test suite, and can be run against your engine with `sideband measure`.
A `synthetic: false` trace is what you get when you set `--trace-out` on such a
run.

## 10. Reproducing every number in the README

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
bash scripts/demo.sh
```

Expected, with the shipped fixture:

```
load  bits/session/s     ci95  verdict
idle             3.1  2.8-3.3  open
0.5x             1.7  0.9-2.3
1.0x             0.8  0.0-1.6
```

The verdict is `open`, with a shuffled-bits floor of 1.4 bits/session/s, over
180 payload rounds carrying 40 decode errors and a mean null of 0.3
bits/session/s. `channel_budget.json` and `channel_budget.md` are written to the
working directory. The run is deterministic: the same trace gives byte-identical
artifacts, including under a different `PYTHONHASHSEED`.

The test suite (`python3 -m pytest -q`) covers the encoder/decoder round trip,
the shuffled-bit null, the three-way verdict, the HTTP-surface-only fingerprint,
the synthetic provenance in both artifacts and the bundled trace replay. The live
test is skipped unless `SIDEBAND_TEST_ENDPOINT` points at a running
OpenAI-compatible server.

## 11. The recording

`assets/demo.cast` is a real asciinema recording made with asciinema 3.2.0
(asciicast v2, 104×40) on macOS. A shell driver ran `bash scripts/demo.sh`,
`sed -n '1,24p' channel_budget.json` and `python3 -m pytest -q`; every line of
output is genuine stdout from those commands. The driver only printed the
`$ command` prompt lines and inserted pauses for readability — it did not edit
or reprint program output. Every run of the demo is timed at a few seconds;
the pauses account for the recording's runtime.

No live-engine segment appears in the recording because no engine was running on
the recording machine. `assets/demo.tape` is the vhs tape that renders the same
commands to `assets/demo.gif` (1040×640, rendered with vhs 0.12.1); the cast is
the committed evidence of record.
