"""Tests for previous-vs-current comparison (analyzer.compare).

  python3 -m pytest analyzer/tests/ -q
  python3 -m analyzer.tests.test_compare
"""

from __future__ import annotations

from analyzer.compare import (
    compare,
    comparison_explain,
    comparison_report,
)

_SPEC = {"name": "C", "services": ["blog"], "infrastructure": ["db"],
         "operations": {"post": ["blog", "db"]},
         "failure_rates": {"default": 0.001}}
_VUS = 50
_TARGET = 0.7


def _meas(arrival, service_rate, replicas=3):
    return {"services": {"blog": {"replicas": replicas, "arrival_rate": arrival,
                                  "service_rate": service_rate,
                                  "failure_rate": 0.001, "repair_rate": 100.0}},
            "infrastructure": {"db": {"failure_rate": 0.001,
                                      "repair_rate": 100.0}}}


# overloaded (poisoned μ) -> fast/healthy is the pagination-fix scenario
_SLOW = _meas(arrival=18.0, service_rate=1.5)     # ρ=4 -> saturated
_FAST = _meas(arrival=40.0, service_rate=50.0)    # ρ=0.27 -> healthy


def test_overall_improved_when_service_gets_faster():
    c = compare(_SPEC, _SLOW, _FAST, _VUS, _TARGET)
    assert c.overall == "improved"
    assert c.tally["improved"] > 0 and c.tally["worse"] == 0


def test_overall_worse_when_service_regresses():
    c = compare(_SPEC, _FAST, _SLOW, _VUS, _TARGET)
    assert c.overall == "worse"
    assert c.tally["worse"] > 0 and c.tally["improved"] == 0


def test_overall_no_change_when_identical():
    c = compare(_SPEC, _FAST, _FAST, _VUS, _TARGET)
    assert c.overall == "no change"
    assert c.tally["improved"] == 0 and c.tally["worse"] == 0


def test_response_time_flip_counts_as_improved():
    c = compare(_SPEC, _SLOW, _FAST, _VUS, _TARGET)
    w = next(m for m in c.metrics
             if m.entity == "blog" and m.name.startswith("response time"))
    assert w.verdict == "improved"          # was OVER (unstable), now finite


def test_capacity_up_with_tiny_utilization_uptick_reads_improved():
    # capacity rises; utilization ticks up but stays far under target -> not "worse"
    prev = _meas(arrival=2.0, service_rate=30.0)   # ~2% used, ceiling 90
    curr = _meas(arrival=6.0, service_rate=40.0)   # ~5% used (up, but «target), ceiling 120
    c = compare(_SPEC, prev, curr, _VUS, _TARGET)
    util = next(m for m in c.metrics if m.name == "utilization")
    assert util.verdict == "same"          # below significance floor
    assert c.overall == "improved"         # capacity up, nothing meaningfully worse


def test_near_zero_utilization_wiggle_is_not_worse():
    prev = _meas(arrival=0.001, service_rate=30.0)
    curr = _meas(arrival=0.002, service_rate=30.0)   # +100% relative, ~0 absolute
    util = next(m for m in compare(_SPEC, prev, curr, _VUS, _TARGET).metrics
                if m.name == "utilization")
    assert util.verdict == "same"


def test_no_double_counting_of_redundant_metrics():
    names = {m.name for m in compare(_SPEC, _SLOW, _FAST, _VUS, _TARGET).metrics}
    assert "capacity/replica (req/s)" not in names   # redundant with ceiling
    assert "availability" not in names               # redundant with downtime


def test_report_and_explain_render():
    c = compare(_SPEC, _SLOW, _FAST, _VUS, _TARGET)
    rep = comparison_report(c, prev_when="2026-07-13 22:00")
    assert "COMPARISON" in rep and "OVERALL: IMPROVED" in rep
    assert "blog" in rep and "post" in rep
    exp = comparison_explain(c, prev_when="2026-07-13 22:00")
    assert "DID IT GET BETTER" in exp and "improved" in exp.lower()


def test_explain_is_plain_language():
    c = compare(_SPEC, _SLOW, _FAST, _VUS, _TARGET)
    text = comparison_explain(c)
    for jargon in ("ρ", "μ", "M/M/c", "utilization", "MTTR"):
        assert jargon not in text, f"jargon leaked: {jargon}"


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
