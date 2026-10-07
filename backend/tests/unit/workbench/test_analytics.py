from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from policy_api.workbench.analytics import (
    AnalyticsError,
    count_metric,
    percentile_metric,
    ratio_metric,
    resolve_window,
    suppress_small_segment,
)


NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def test_metric_math_keeps_zero_denominator_unavailable() -> None:
    metric = ratio_metric(0, 0)

    assert metric.numerator == 0
    assert metric.denominator == 0
    assert metric.value is None
    assert metric.available is False
    assert metric.sample_size == 0
    assert metric.metric_version == "v1"

    available = ratio_metric(3, 4)
    assert available.value == 0.75
    assert available.available is True
    assert available.sample_size == 4

    answered = ratio_metric(5, 10)
    clarified = ratio_metric(3, 10)
    abstained = ratio_metric(2, 10)
    assert answered.numerator == 5
    assert clarified.numerator == 3
    assert abstained.numerator == 2
    assert sum(
        metric.numerator or 0 for metric in (answered, clarified, abstained)
    ) == 10


def test_count_percentile_and_small_segment_semantics_are_explicit() -> None:
    count = count_metric(0)
    assert count.value == 0
    assert count.available is True
    assert count.sample_size == 0

    percentile = percentile_metric(125.5, sample_size=3)
    assert percentile.value == 125.5
    assert percentile.numerator == 125.5
    assert percentile.denominator is None
    assert percentile.sample_size == 3

    unavailable = percentile_metric(None, sample_size=0)
    assert unavailable.value is None
    assert unavailable.available is False

    hidden = suppress_small_segment(ratio_metric(2, 3), minimum_sample_size=5)
    assert hidden.numerator is None
    assert hidden.denominator is None
    assert hidden.value is None
    assert hidden.available is False
    assert hidden.sample_size == 3


def test_window_defaults_to_seven_days_and_accepts_exact_ninety_days() -> None:
    default = resolve_window(None, None, now=NOW)
    assert default.end == NOW
    assert default.start == NOW - timedelta(days=7)

    exact = resolve_window(NOW - timedelta(days=90), NOW, now=NOW)
    assert exact.start == NOW - timedelta(days=90)
    assert exact.end == NOW


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (NOW, NOW),
        (NOW, NOW - timedelta(seconds=1)),
        (NOW - timedelta(days=90, seconds=1), NOW),
        (NOW - timedelta(days=1), NOW + timedelta(seconds=1)),
        (datetime(2026, 8, 1), NOW),
    ],
)
def test_window_rejects_reversed_too_wide_future_or_naive_values(
    start: datetime, end: datetime,
) -> None:
    with pytest.raises(AnalyticsError, match="analytics_range_invalid"):
        resolve_window(start, end, now=NOW)
