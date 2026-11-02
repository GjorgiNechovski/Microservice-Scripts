"""Tests for the cross-level scaling analysis (analyzer.scaling).

Builds a small synthetic sweep where one service saturates mid-range and another
is over-provisioned, then checks the findings, the reports and the figures.

  python3 -m pytest analyzer/tests/ -q
  python3 -m analyzer.tests.test_scaling
"""

from __future__ import annotations

import os
import tempfile

from analyzer.capacity import size_services
from analyzer.scaling import (
    LevelResult,
    bottleneck,
    first_saturating_level,
    max_safe_load,
    provisioning,
    scaling_explain,
    scaling_report,
)
from analyzer.spec import analyze

_SPEC = {"name": "T", "services": ["blog", "comment"],
         "infrastructure": [], "operations": {}}
_TARGET = 0.7


def _level(vus: int) -> LevelResult:
    # blog load grows fast (saturates at 200 VUs); comment stays light (over-provisioned)
    meas = {"services": {
                "blog": {"replicas": 3, "arrival_rate": vus * 0.15,
                         "service_rate": 10.0, "failure_rate": 0.01,
                         "repair_rate": 100.0},
                "comment": {"replicas": 3, "arrival_rate": vus * 0.02,
                            "service_rate": 10.0, "failure_rate": 0.01,
                            "repair_rate": 100.0}},
            "infrastructure": {}}
    results = analyze(_SPEC, meas)
    sizings = size_services(_SPEC, meas, _TARGET)
    return LevelResult(vus, meas, results, sizings)


_LEVELS = [_level(v) for v in (50, 100, 200, 500)]


# A second sweep exercising all four provisioning buckets + the guardrail:
#   web   -> reliably under-provisioned (ρ climbs to 0.9, needs a 4th replica)
#   cache -> over-provisioned (barely used)
#   db    -> overloaded at EVERY level (ρ≥1) -> μ unreliable, cannot be sized
_SPEC3 = {"name": "S", "services": ["web", "cache", "db"],
          "infrastructure": [], "operations": {}}


def _slevel(vus: int) -> LevelResult:
    meas = {"services": {
                "web": {"replicas": 3, "arrival_rate": round(vus * 0.054, 3),
                        "service_rate": 10.0, "failure_rate": 0.01,
                        "repair_rate": 100.0},
                "cache": {"replicas": 3, "arrival_rate": round(vus * 0.02, 3),
                          "service_rate": 50.0, "failure_rate": 0.01,
                          "repair_rate": 100.0},
                "db": {"replicas": 1, "arrival_rate": round(vus * 0.15, 3),
                       "service_rate": 5.0, "failure_rate": 0.01,
                       "repair_rate": 100.0}},
            "infrastructure": {}}
    return LevelResult(vus, meas, analyze(_SPEC3, meas),
                       size_services(_SPEC3, meas, _TARGET))


_STRESSED = [_slevel(v) for v in (50, 100, 200, 500)]


def test_first_saturating_level():
    assert first_saturating_level(_LEVELS, "blog", _TARGET) == 200
    assert first_saturating_level(_LEVELS, "comment", _TARGET) is None


def test_bottleneck_is_the_first_to_saturate():
    assert bottleneck(_SPEC, _LEVELS, _TARGET) == ("blog", 200)


def test_max_safe_load_is_last_all_within_target():
    assert max_safe_load(_SPEC, _LEVELS, _TARGET) == 100


def test_provisioning_four_buckets():
    under, over, ok, unreliable = provisioning(_SPEC3, _STRESSED)
    assert under == ["web"]
    assert over == ["cache"]
    assert unreliable == ["db"]          # overloaded throughout -> cannot size


def test_guardrail_suppresses_overloaded_replica_count():
    text = scaling_report(_SPEC3, _STRESSED, _TARGET)
    assert "Could not size" in text
    assert "db=n/a" in text              # the recipe refuses to invent a number
    # sanity: a sane count still appears for the services we CAN size
    assert "web=" in text and "web=n/a" not in text


def test_guardrail_in_plain_explain_stays_jargon_free():
    text = scaling_explain(_SPEC3, _STRESSED, _TARGET)
    assert "db" in text and "Left out" in text
    for jargon in ("ρ", "μ", "M/M/c", "utilization", "MTTR"):
        assert jargon not in text, f"jargon leaked: {jargon}"


def test_scaling_report_has_all_sections():
    text = scaling_report(_SPEC, _LEVELS, _TARGET)
    for chunk in ("Throughput", "Utilization", "response time",
                  "Replicas needed", "Verdict", "Bottleneck", "blog"):
        assert chunk in text, f"missing: {chunk}"


def test_scaling_report_marks_overload():
    # blog is unstable at 200/500 -> the response table shows OVER somewhere
    assert "OVER" in scaling_report(_SPEC, _LEVELS, _TARGET)


def test_scaling_explain_is_plain_language():
    text = scaling_explain(_SPEC, _LEVELS, _TARGET)
    assert "SCALING SUMMARY" in text and "blog" in text
    for jargon in ("ρ", "M/M/c", "utilization", "MTTR"):
        assert jargon not in text, f"jargon leaked: {jargon}"


def test_scaling_report_survives_single_level():
    # a degenerate sweep of one level should still render without error
    text = scaling_report(_SPEC, [_LEVELS[0]], _TARGET)
    assert "Verdict" in text


def test_scaling_figures_render():
    try:
        from analyzer.figures.plots import generate_scaling
    except ImportError:
        print("  (skip: matplotlib/numpy not installed)")
        return
    out = tempfile.mkdtemp()
    paths = generate_scaling(_SPEC, _LEVELS, _TARGET, out)
    assert len(paths) == 5          # 4 sweep curves + the diagnose quadrant
    for p in paths:
        assert os.path.exists(p) and os.path.getsize(p) > 0


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
