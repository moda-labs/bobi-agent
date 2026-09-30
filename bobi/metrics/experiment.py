"""Statistical diagnostics for versioned router experiments."""

from __future__ import annotations

import math


def _regularized_gamma_q(shape: float, value: float) -> float:
    """Upper regularized gamma using stable series/fraction expansions."""
    if shape <= 0 or value < 0:
        raise ValueError("gamma arguments must be positive")
    if value == 0:
        return 1.0
    epsilon = 3e-14
    tiny = 1e-300
    if value < shape + 1:
        term = total = 1.0 / shape
        cursor = shape
        for _ in range(1000):
            cursor += 1
            term *= value / cursor
            total += term
            if abs(term) < abs(total) * epsilon:
                break
        lower = total * math.exp(-value + shape * math.log(value) - math.lgamma(shape))
        return max(0.0, min(1.0, 1.0 - lower))
    b = value + 1 - shape
    c = 1 / tiny
    d = 1 / max(b, tiny)
    fraction = d
    for index in range(1, 1001):
        coefficient = -index * (index - shape)
        b += 2
        d = coefficient * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + coefficient / c
        if abs(c) < tiny:
            c = tiny
        d = 1 / d
        delta = d * c
        fraction *= delta
        if abs(delta - 1) < epsilon:
            break
    upper = math.exp(-value + shape * math.log(value) - math.lgamma(shape)) * fraction
    return max(0.0, min(1.0, upper))


def sample_ratio_mismatch(
    observed: list[int], expected_weights: list[float], *, alpha: float = 0.001
) -> dict[str, object]:
    if len(observed) < 2 or len(observed) != len(expected_weights):
        raise ValueError("SRM requires matching observed and expected variant arrays")
    if any(value < 0 for value in observed):
        raise ValueError("SRM observed counts must be non-negative")
    if any(weight <= 0 for weight in expected_weights):
        raise ValueError("SRM expected weights must be positive")
    weight_total = sum(expected_weights)
    weights = [weight / weight_total for weight in expected_weights]
    total = sum(observed)
    if total == 0:
        return {
            "sample_size": 0,
            "chi_square": None,
            "degrees_of_freedom": len(observed) - 1,
            "p_value": None,
            "alpha": alpha,
            "detected": False,
        }
    expected = [total * weight for weight in weights]
    statistic = sum(
        (actual - target) ** 2 / target
        for actual, target in zip(observed, expected)
    )
    degrees = len(observed) - 1
    p_value = _regularized_gamma_q(degrees / 2, statistic / 2)
    return {
        "sample_size": total,
        "chi_square": statistic,
        "degrees_of_freedom": degrees,
        "p_value": p_value,
        "alpha": alpha,
        "detected": p_value < alpha,
    }
