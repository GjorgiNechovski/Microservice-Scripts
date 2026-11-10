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
    unreached,
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
    # blog is unstable at 200/500 -> its utilization is flagged '!'
    text = scaling_report(_SPEC, _LEVELS, _TARGET)
    assert "!" in text.split("Utilization")[1].split("Mean response time")[0]


def test_response_time_table_is_measured_not_modelled():
    # 1/service_rate per level (330, 200, 40 req/s -> 3.0, 5.0, 25 ms); the
    # model's W would read a fraction of a millisecond against the floor rate
    text = scaling_report(_KSPEC, _FLOORED, _TARGET)
    table = text.split("MEASURED at each service")[1].split("(each service")[0]
    rows = [ln.split() for ln in table.splitlines() if ln[:1].isdigit()]
    assert rows == [["200", "3.0"], ["800", "5.0"], ["1600", "25"]]
    assert "OVER" not in text


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


# A sweep whose service kept pace with the users throughout, as the pipeline
# writes it: the effective rate is the top level's throughput per replica,
# flagged as a floor. The top level then reads rho = 1 by construction, which
# must not turn into a bottleneck or a replica recommendation.
_KSPEC = {"name": "K", "services": ["api"], "infrastructure": [],
          "operations": {}}


def _flevel(vus: int) -> LevelResult:
    meas = {"services": {"api": {"replicas": 2, "arrival_rate": vus * 0.59,
                                 "service_rate": {200: 330.0, 800: 200.0,
                                                  1600: 40.0}[vus],
                                 "service_rate_effective": 472.0,
                                 "service_rate_lower_bound": True,
                                 "failure_rate": 0.01, "repair_rate": 100.0}},
            "infrastructure": {}}
    return LevelResult(vus, meas, analyze(_KSPEC, meas),
                       size_services(_KSPEC, meas, _TARGET))


_FLOORED = [_flevel(v) for v in (200, 800, 1600)]


def test_a_service_that_kept_pace_is_no_bottleneck():
    assert bottleneck(_KSPEC, _FLOORED, _TARGET) is None
    assert max_safe_load(_KSPEC, _FLOORED, _TARGET) == 1600
    assert unreached(_KSPEC, _FLOORED) == ["api"]
    assert provisioning(_KSPEC, _FLOORED) == ([], [], [], [])


def test_reports_do_not_size_a_service_that_kept_pace():
    text = scaling_report(_KSPEC, _FLOORED, _TARGET)
    assert "Limit not reached        : api" in text
    assert "api=2 (kept up)" in text and "≤1.00" in text
    for wrong in ("Under-provisioned", "ADD REPLICAS", "maxed out"):
        assert wrong not in text, f"still claims: {wrong}"
    rows = [ln for ln in text.splitlines() if ln[:1].isdigit()]   # per-level rows
    assert rows and not any("OVER" in ln or "!" in ln for ln in rows)
    plain = scaling_explain(_KSPEC, _FLOORED, _TARGET)
    assert "Never ran out of room" in plain and "2 × api (what you ran" in plain
    assert "Too few copies" not in plain and "add copies" not in plain.lower()


def test_what_users_felt_caps_the_comfortable_load():
    # the same kept-pace sweep, now with what k6 saw: fine at 200 and 800,
    # p95 208 ms and 0.37% failed at 1600
    from analyzer.responsiveness import judge_levels
    from analyzer.validity import K6Summary
    feels = judge_levels(_KSPEC, {
        200: K6Summary(p95_ms=15.0, failed=0.0),
        800: K6Summary(p95_ms=35.0, failed=0.0),
        1600: K6Summary(p95_ms=207.64, failed=0.0037)})
    levels = [LevelResult(lv.vus, lv.measurements, lv.results, lv.sizings,
                          feel=feels[lv.vus]) for lv in _FLOORED]
    text = scaling_report(_KSPEC, levels, _TARGET)
    assert "Responsiveness -- what the users saw" in text
    assert "Highest comfortable load : 800 VUs (at 1600 VUs users felt it" in text
    assert "api=2 (kept up, but users felt it)" in text
    plain = scaling_explain(_KSPEC, levels, _TARGET)
    assert "stayed comfortable up to about 800 users" in plain
    assert "0.37% of requests failed" in plain and "not\n  smoothly" in plain
    assert "there is\n  headroom" not in plain
    # without k6 data the verdict is the model's, as before
    assert "Highest comfortable load : 1600 VUs" in scaling_report(
        _KSPEC, _FLOORED, _TARGET)


def test_bottleneck_line_names_a_hidden_limit():
    from analyzer.tests.test_diagnose import _plateau_levels
    spec, levels = _plateau_levels()
    text = scaling_report(spec, levels, _TARGET)
    assert "Bottleneck               : hidden, not CPU" in text
    assert "HIDDEN LIMIT — not CPU" in text
    plain = scaling_explain(spec, levels, _TARGET)
    assert "the real limit is" in plain and "is the weakest link" not in plain
    # the top level (2000) is past the limit: size for the peak, with the caveat
    assert "To serve the most it handled (1600 VUs" in text
    assert "only if the hidden limit is per replica" in text
    assert "To serve 2000 VUs" not in text
    assert "The most it handled was at 1600 users" in plain
    assert "(add some)" not in plain and "by the numbers" in plain


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
