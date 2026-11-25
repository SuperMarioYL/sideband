"""Encoder/decoder round trip, threshold calibration, and the shuffled-label null."""

from __future__ import annotations

import numpy as np
import pytest

from sideband import canary
from sideband.models import ProbeTrace

CALIB = 8
PAYLOAD = 32


def receiver(round_: int, score_ms: float, *, itl: bool = True, point: str = "idle") -> ProbeTrace:
    return ProbeTrace(
        point=point,
        round=round_,
        session="receiver",
        req_tokens=6,
        ttft_ms=5.0 if itl else score_ms,
        itl_ms=[score_ms] * 5 if itl else [],
        resp_tokens=6 if itl else 0,
        t_ms=round_ * 300.0,
    )


def sender(round_: int, bit: int, *, point: str = "idle") -> ProbeTrace:
    steps = canary.encode(bit)
    return ProbeTrace(
        point=point,
        round=round_,
        session="sender",
        req_tokens=canary.SENDER_PROMPT_TOKENS[bit],
        ttft_ms=4.0,
        itl_ms=[9.0] * max(steps - 1, 1),
        resp_tokens=steps,
        t_ms=round_ * 300.0 + 0.5,
    )


def test_one_bit_per_slot_encoded_by_step_count() -> None:
    assert canary.BITS_PER_ROUND == 1
    assert canary.encode(0) < canary.encode(1)
    assert canary.bit_from_sender(sender(0, 0)) == 0
    assert canary.bit_from_sender(sender(0, 1)) == 1
    # The calibration schedule must cover both classes, or the threshold has no gap.
    assert {canary.calibration_bit(i) for i in range(6)} == {0, 1}


def test_rejects_bits_outside_the_alphabet() -> None:
    with pytest.raises(ValueError):
        canary.encode(2)


def test_threshold_sits_between_the_calibrated_classes() -> None:
    scores = [10.0, 10.2, 10.1, 14.0, 14.3, 13.9]
    bits = [0, 0, 0, 1, 1, 1]
    threshold = canary.calibrate(scores, bits)
    assert 10.2 < threshold < 13.9


def test_calibration_needs_rounds_in_both_classes() -> None:
    with pytest.raises(canary.CalibrationError):
        canary.calibrate([10.0, 10.1], [0, 0])


def test_round_trip_recovers_every_bit() -> None:
    truth = [i % 2 for i in range(PAYLOAD)]
    scores = [14.0 if bit else 10.0 for bit in truth]

    threshold = canary.calibrate(
        [14.0 if canary.calibration_bit(i) else 10.0 for i in range(CALIB)],
        [canary.calibration_bit(i) for i in range(CALIB)],
    )
    decoded = canary.decode(scores, threshold)

    assert list(decoded) == truth
    assert canary.net_bits(decoded, truth).tolist() == [1.0] * PAYLOAD


def test_shuffled_labels_read_near_zero() -> None:
    truth = np.asarray([i % 2 for i in range(PAYLOAD)], dtype=np.int8)
    decoded = canary.decode([14.0 if bit else 10.0 for bit in truth], 12.0)

    per_round = []
    for seed in range(8):
        shuffled = canary.shuffled_bits(truth, np.random.default_rng(seed))
        per_round.append(abs(canary.net_bits(decoded, shuffled).sum()) / PAYLOAD)

    assert max(per_round) <= 0.25, per_round


def test_slot_score_falls_back_to_ttft_without_itl() -> None:
    assert canary.slot_score(receiver(0, 7.5, itl=False)) == pytest.approx(7.5)
    assert canary.slot_score(receiver(0, 7.5)) == pytest.approx(7.5)


def test_decode_is_a_pure_function_of_receiver_timings() -> None:
    # Same threshold, same scores: same bits. Nothing else is consulted.
    scores = [9.0, 11.0, 12.0, 8.0]
    assert canary.decode(scores, 10.0).tolist() == [
        int(value >= 10.0) for value in scores
    ]
