"""Tests for the sensitivity-analysis tools (Thesis Layer 4).

Runs two ways:
  * with pytest:            python3 -m pytest analyzer/tests/ -q
  * with a bare interpreter: python3 -m analyzer.tests.test_sensitivity

Includes integration tests that drive the ACTUAL Layer 1/2 models, so the
sensitivity layer is checked end-to-end, not just on toy functions.
"""

from __future__ import annotations

import math

from analyzer.models.ctmc import replicated_reliability
from analyzer.models.queueing import mmc_metrics
from analyzer.models.sensitivity import (
    ascii_bar, elasticity, sparkline, sweep, tornado,
)

REL = 1e-6


def _close(a, b, rel=REL):
    return math.isclose(a, b, rel_tol=rel, abs_tol=1e-9)


# --------------------------------------------------------------------------
# sweep
# --------------------------------------------------------------------------

def test_sweep_values():
    s = sweep(lambda x: x * x, [1, 2, 3], "x", "x_squared")
    assert s.xs == (1, 2, 3)
    assert s.ys == (1, 4, 9)
    assert s.points() == [(1, 1), (2, 4), (3, 9)]


def test_sweep_csv():
    s = sweep(lambda x: 2 * x, [0, 1], "n", "m")
    assert s.to_csv() == "n,m\n0,0\n1,2"


def test_sweep_empty_raises():
    raised = False
    try:
        sweep(lambda x: x, [])
    except ValueError:
        raised = True
    assert raised


# --------------------------------------------------------------------------
# elasticity  (closed form: for f(x)=x^n, elasticity == n everywhere)
# --------------------------------------------------------------------------

def test_elasticity_power_law():
    for n in (1, 2, 3):
        e = elasticity(lambda x, n=n: x ** n, 5.0)
        assert _close(e, float(n), rel=1e-4)


def test_elasticity_constant_is_zero():
    assert _close(elasticity(lambda x: 7.0, 3.0), 0.0)


def test_elasticity_sign_and_undefined():
    assert elasticity(lambda x: 2 * x, 4.0) > 0        # increasing -> positive
    # f(x) = 1/x decreases while positive -> negative elasticity (== -1).
    assert _close(elasticity(lambda x: 1.0 / x, 4.0), -1.0, rel=1e-4)
    raised = False
    try:
        elasticity(lambda x: x, 0.0)                   # undefined at 0
    except ValueError:
        raised = True
    assert raised


# --------------------------------------------------------------------------
# tornado
# --------------------------------------------------------------------------

def test_tornado_ranks_by_swing():
    # f = a + 2b : b has twice the swing of a for equal ranges.
    f = lambda p: p["a"] + 2 * p["b"]
    bars = tornado(f, baseline={"a": 0, "b": 0},
                   ranges={"a": (0, 1), "b": (0, 1)})
    assert [b.name for b in bars] == ["b", "a"]        # b ranked first
    assert _close(bars[0].width, 2.0)
    assert _close(bars[1].width, 1.0)


# --------------------------------------------------------------------------
# rendering helpers
# --------------------------------------------------------------------------

def test_sparkline():
    assert sparkline([]) == ""
    assert len(sparkline([1, 2, 3, 4, 5])) == 5
    assert sparkline([3, 3, 3]) == "▁▁▁"               # flat -> lowest block
    sl = sparkline([0, 10])
    assert sl[0] == "▁" and sl[-1] == "█"              # min/max map to ends


def test_ascii_bar():
    assert ascii_bar(0, 10) == ""
    assert ascii_bar(10, 10, width=10) == "█" * 10
    assert len(ascii_bar(5, 10, width=10)) == 5
    assert len(ascii_bar(999, 10, width=10)) == 10     # clamped


# --------------------------------------------------------------------------
# Integration with the real models (end-to-end)
# --------------------------------------------------------------------------

def test_integration_response_time_vs_replicas():
    # More replicas -> lower response time; the sweep should be monotone down.
    lam, mu = 25.0, 14.0
    s = sweep(lambda c: mmc_metrics(lam, mu, c).W, [2, 3, 4, 5, 6],
              "replicas", "W")
    ys = s.ys
    assert all(ys[i] < ys[i - 1] for i in range(1, len(ys)))


def test_integration_availability_elastic_to_repair_rate():
    # Availability increases with repair rate -> positive elasticity.
    def avail(repair_rate):
        return replicated_reliability(3, 0.03, repair_rate, k=1).availability
    e = elasticity(avail, 12.0)
    assert e > 0.0


def test_integration_diminishing_returns_in_replicas():
    # The response-time gain from each extra replica shrinks (convex, flattening).
    lam, mu = 25.0, 14.0
    W = [mmc_metrics(lam, mu, c).W for c in (2, 3, 4, 5)]
    gains = [W[i - 1] - W[i] for i in range(1, len(W))]
    assert all(gains[i] < gains[i - 1] for i in range(1, len(gains)))


# --------------------------------------------------------------------------
# Standalone runner
# --------------------------------------------------------------------------

def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed"
          + (" -- ALL GREEN" if failed == 0 else f" -- {failed} FAILED"))
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    sys.exit(_run_all())
