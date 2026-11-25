"""Step-sizing canary: one bit per slot, encoded in decode steps, read from timing.

The mechanism this encodes. Continuous batching puts co-scheduled requests in
the same engine step. A sender that keeps many decode steps in the batch makes
every step of that batch wider and slower; a co-batched receiver measures that
widening in its own inter-token latencies. So the *sender* writes by choosing how
many generation steps to occupy, and the *receiver* reads from its own timings
alone — no socket between them.

Roles in the code, kept strictly apart:

* ``encode(bit)`` — the sender's step count for a bit.
* ``bit_from_sender(row)`` — ground truth recovered from the sender's row. Used
  only for scoring and for the null control; the decoder never sees it, because
  reading the sender's own record would measure the harness rather than the
  channel.
* ``calibrate`` / ``decode`` — receiver-side threshold and decision, from
  receiver timings only.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import numpy as np

from .models import ProbeTrace

ENCODER = "step-sizing"
BITS_PER_ROUND = 1

#: Decode steps the sender keeps in the batch, per bit. The gap is what the
#: receiver's step time is supposed to move by; 4 vs 32 is roughly one order of
#: magnitude in batch width on a 0.5B model served on a single card.
SENDER_STEPS: dict[int, int] = {0: 4, 1: 32}

#: Approximate prompt sizes the sender uses ("small tool call" vs "large tool
#: call"), so the bit also changes prefill pressure, not only decode width.
SENDER_PROMPT_TOKENS: dict[int, int] = {0: 16, 1: 1024}

#: The boundary between "small" and "large" generations when recovering ground
#: truth from a recorded sender row.
_SENDER_STEPS_MIDPOINT = (SENDER_STEPS[0] + SENDER_STEPS[1]) // 2


class CalibrationError(RuntimeError):
    """The calibration rounds did not cover both bit classes."""


def encode(bit: int) -> int:
    """Decode steps the sender must occupy to transmit ``bit``."""
    try:
        return SENDER_STEPS[int(bit)]
    except KeyError:
        raise ValueError(f"bit must be 0 or 1, got {bit!r}") from None


def calibration_bit(index: int) -> int:
    """The known schedule for calibration rounds: alternate, so both classes appear."""
    return index % 2


def bit_from_sender(row: ProbeTrace) -> int:
    """Ground truth for a round, recovered from what the sender actually generated."""
    return 1 if row.resp_tokens >= _SENDER_STEPS_MIDPOINT else 0


def slot_score(row: ProbeTrace) -> float:
    """The receiver's observation for one round: its mean step time in the batch."""
    samples = row.itl_ms or [row.ttft_ms]
    return float(np.mean(samples))


def calibrate(scores: Sequence[float], bits: Sequence[int]) -> float:
    """Midpoint between the two calibrated step-time distributions.

    Calibration rounds carry known bits, so the threshold is supervised — that is
    the receiver agreeing a decision boundary before the exchange, and it is why a
    channel that does not move step times lands on a threshold that decodes noise
    (and reads at the null baseline) instead of a flattering guess.
    """
    scores = np.asarray(scores, dtype=np.float64)
    bits = np.asarray(bits, dtype=np.int8)
    low = scores[bits == 0]
    high = scores[bits == 1]
    if low.size == 0 or high.size == 0:
        raise CalibrationError(
            "calibration needs rounds in both bit classes "
            f"(got {low.size} small, {high.size} large)"
        )
    return float((np.median(low) + np.median(high)) / 2.0)


def decode(scores: Sequence[float], threshold: float) -> np.ndarray:
    """Receiver decision per round: a wider step is a 1."""
    return (np.asarray(scores, dtype=np.float64) >= threshold).astype(np.int8)


def net_bits(decoded: Iterable[int], truth: Iterable[int]) -> np.ndarray:
    """Per-round contribution to throughput: +1 for a correct bit, -1 for a wrong one.

    Subtracting errors is what makes the estimate an honest lower bound: a channel
    read at chance yields ~0, and a mostly-correct channel yields its reliable
    fraction rather than its raw round rate.
    """
    decoded = np.asarray(list(decoded), dtype=np.int8)
    truth = np.asarray(list(truth), dtype=np.int8)
    if decoded.shape != truth.shape:
        raise ValueError(f"decoded and truth differ in length: {decoded.size} vs {truth.size}")
    return np.where(decoded == truth, 1, -1).astype(np.float64)


def shuffled_bits(bits: Sequence[int], rng: np.random.Generator) -> np.ndarray:
    """The null control: the same bit stream with its round assignment broken."""
    return rng.permutation(np.asarray(bits, dtype=np.int8))
