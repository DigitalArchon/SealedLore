"""Token estimation and the rolling per-model correction. See §5.1."""

from __future__ import annotations

from sealedlore.engine.tokens import (
    MAX_CORRECTION_FACTOR,
    MIN_CORRECTION_FACTOR,
    TokenEstimator,
    fallback_counter,
    updated_correction_factor,
)


def test_fallback_counter_is_deterministic():
    assert fallback_counter("") == 0
    assert fallback_counter("abcd") == 1
    assert fallback_counter("abcde") == 2


def test_injected_counter_is_treated_as_exact():
    estimator = TokenEstimator(counter=fallback_counter)
    assert estimator.is_approximate is False
    assert estimator.raw_count("abcdefgh") == 2


def test_correction_factor_scales_the_estimate():
    estimator = TokenEstimator(counter=lambda text: 100, factors={"model-a": 1.5})
    assert estimator.estimate("whatever", "model-a") == 150
    assert estimator.estimate("whatever", "model-b") == 100
    assert estimator.estimate("whatever") == 100


def test_empty_text_costs_nothing():
    estimator = TokenEstimator(counter=lambda text: 99)
    assert estimator.estimate("") == 0
    assert estimator.raw_count("") == 0


def test_factor_rolls_towards_the_reported_count():
    # Predicted 1000 at factor 1.0, provider actually charged 1200.
    rolled = updated_correction_factor(1.0, 1000, 1200, smoothing=0.5)
    assert rolled == 1.1  # halfway between 1.0 and the observed 1.2


def test_factor_divides_out_the_current_factor_before_comparing():
    # A corrected estimate of 1200 at factor 1.2 means the raw estimate was
    # 1000; a reported 1200 therefore confirms the factor rather than raising it.
    assert updated_correction_factor(1.2, 1200, 1200, smoothing=0.5) == 1.2


def test_factor_is_clamped():
    # Smoothing alone keeps a single wild observation from moving the factor
    # far, so drive it fully to check the clamp itself.
    assert updated_correction_factor(1.0, 100, 100_000, smoothing=1.0) == MAX_CORRECTION_FACTOR
    assert updated_correction_factor(1.0, 100_000, 100, smoothing=1.0) == MIN_CORRECTION_FACTOR


def test_smoothing_limits_how_far_one_observation_moves_the_factor():
    # The provider reported 40% more than predicted; the factor moves towards
    # that without jumping straight to it.
    rolled = updated_correction_factor(1.0, 1000, 1400)
    assert 1.0 < rolled < 1.4


def test_factor_ignores_unusable_inputs():
    assert updated_correction_factor(1.3, 0, 500) == 1.3
    assert updated_correction_factor(1.3, 500, 0) == 1.3
