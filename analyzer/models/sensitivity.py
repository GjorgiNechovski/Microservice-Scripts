"""Sensitivity analysis -- Thesis Layer 4.

Model-agnostic tools for asking "which parameter matters most?" and "where does
engineering effort pay off?". Nothing here knows about queues or Markov chains;
you pass in a function that evaluates a metric (internally using Layer 1/2/3),
and these utilities sweep it, differentiate it, and rank parameters by impact.

Provided:
  * ``sweep``       -- evaluate a metric across a range of one parameter (a curve).
  * ``elasticity``  -- normalized local sensitivity (dM/M)/(dx/x): dimensionless,
                       so different parameters are directly comparable.
  * ``tornado``     -- vary each of several parameters between a low/high value
                       (others held at baseline) and rank by the metric swing.
  * rendering helpers (``sparkline``, ``ascii_bar``) and ``Sweep.to_csv`` so the
    results are readable in a terminal now and plottable later.

No third-party dependencies::

    python3 -m analyzer.tests.test_sensitivity
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "Sweep", "sweep", "elasticity",
    "TornadoBar", "tornado",
    "sparkline", "ascii_bar",
]


@dataclass(frozen=True)
class Sweep:
    """A 1-D parameter sweep: metric evaluated over a range of one parameter."""

    param_name: str
    metric_name: str
    xs: tuple
    ys: tuple

    def points(self) -> list:
        return list(zip(self.xs, self.ys))

    def to_csv(self) -> str:
        lines = [f"{self.param_name},{self.metric_name}"]
        lines += [f"{x},{y}" for x, y in zip(self.xs, self.ys)]
        return "\n".join(lines)


def sweep(f, values, param_name: str = "param",
          metric_name: str = "metric") -> Sweep:
    """Evaluate metric ``f(value)`` over ``values``; returns a :class:`Sweep`."""
    xs = tuple(values)
    if not xs:
        raise ValueError("values must be non-empty")
    ys = tuple(f(v) for v in xs)
    return Sweep(param_name, metric_name, xs, ys)


def elasticity(f, x0: float, rel_delta: float = 1e-4) -> float:
    """Normalized local sensitivity (elasticity) of ``f`` w.r.t. its argument.

    Returns ``(dM/M) / (dx/x)`` at ``x0`` via a central finite difference:
    a dimensionless number giving the % change in the metric per % change in
    the parameter, so parameters with different units are comparable.
    """
    if x0 == 0:
        raise ValueError("elasticity is undefined at x0 = 0 (division by x)")
    f0 = f(x0)
    dx = abs(x0) * rel_delta
    deriv = (f(x0 + dx) - f(x0 - dx)) / (2.0 * dx)
    if f0 == 0:
        return math.inf if deriv != 0 else 0.0
    return deriv * x0 / f0


@dataclass(frozen=True)
class TornadoBar:
    """One parameter's contribution in a tornado analysis."""

    name: str
    low_metric: float    # metric when the parameter is at its low value
    high_metric: float   # metric when the parameter is at its high value

    @property
    def width(self) -> float:
        return abs(self.high_metric - self.low_metric)


def tornado(f, baseline: dict, ranges: dict) -> list:
    """One-at-a-time tornado analysis.

    Parameters
    ----------
    f : callable taking a dict of parameters -> metric.
    baseline : the baseline parameter dict.
    ranges : ``{param_name: (low_value, high_value)}`` to test.

    Returns bars sorted by descending swing width -- the most impactful
    parameter first.
    """
    bars = []
    for name, (lo, hi) in ranges.items():
        p_lo = dict(baseline); p_lo[name] = lo
        p_hi = dict(baseline); p_hi[name] = hi
        bars.append(TornadoBar(name, f(p_lo), f(p_hi)))
    bars.sort(key=lambda b: b.width, reverse=True)
    return bars


# --------------------------------------------------------------------------
# Terminal rendering helpers (pure ASCII / unicode blocks)
# --------------------------------------------------------------------------

_BLOCKS = "▁▂▃▄▅▆▇█"


def sparkline(ys) -> str:
    """A compact inline chart of a sequence, e.g. ``▁▂▃▅▇█``."""
    ys = list(ys)
    if not ys:
        return ""
    finite = [y for y in ys if math.isfinite(y)]
    if not finite:
        return "?" * len(ys)
    lo, hi = min(finite), max(finite)
    if hi == lo:
        return _BLOCKS[0] * len(ys)
    out = []
    for y in ys:
        if not math.isfinite(y):
            out.append("?")
        else:
            idx = int((y - lo) / (hi - lo) * (len(_BLOCKS) - 1))
            out.append(_BLOCKS[idx])
    return "".join(out)


def ascii_bar(value: float, vmax: float, width: int = 30) -> str:
    """A horizontal bar of length proportional to ``value / vmax``."""
    if vmax <= 0 or value <= 0:
        return ""
    n = int(round(min(value / vmax, 1.0) * width))
    return "█" * n
