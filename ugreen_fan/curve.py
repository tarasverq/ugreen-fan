"""Fan curve maths: piecewise-linear interpolation with hysteresis."""

Curve = tuple[tuple[float, int], ...]


def interpolate(points: Curve, temp: float) -> int:
    if temp <= points[0][0]:
        return points[0][1]
    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        if temp <= t1:
            return round(p0 + (p1 - p0) * (temp - t0) / (t1 - t0))
    return points[-1][1]


def next_pwm(points: Curve, temp: float, current: int, hysteresis: float) -> int:
    """Rise immediately; fall only after the temperature has dropped by `hysteresis`."""
    return max(interpolate(points, temp), min(current, interpolate(points, temp + hysteresis)))
