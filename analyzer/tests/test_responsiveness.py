"""Tests for judging each load level by what k6 measured (analyzer.responsiveness).

The numbers are a real sweep's: three levels whose throughput kept pace with
the users while p95 went 44 -> 72 -> 208 ms and 0.37% of requests failed at the
top -- "kept up" by the capacity math, felt by the users.

  python3 -m pytest analyzer/tests/ -q
  python3 -m analyzer.tests.test_responsiveness
"""

from __future__ import annotations

import os
import tempfile

from analyzer.responsiveness import (
    comfortable,
    first_strained,
    judge_levels,
    judge_one,
    judge_sweep_dir,
    level_explain,
    level_report,
    plain_strain,
    read_k6,
    tech_summary,
)
from analyzer.validity import K6Summary, k6_ms, parse_k6_summary

_SPEC = {"name": "R", "services": ["api"], "infrastructure": [],
         "operations": {}}


def _k6(p95, failed=0.0, avg=None):
    return K6Summary(p95_ms=p95, failed=failed, avg_ms=avg)


_SWEEP = {1000: _k6(43.67, 0.0, 20.48), 1200: _k6(72.33, 0.0, 27.88),
          1600: _k6(207.64, 0.0037, 52.04)}


def test_k6_durations_in_every_unit():
    assert abs(k6_ms("423.54µs") - 0.42354) < 1e-9
    assert abs(k6_ms("423.54Âµs") - 0.42354) < 1e-9     # mis-decoded mu
    assert k6_ms("20.48ms") == 20.48 and k6_ms("5ms") == 5.0
    assert k6_ms("1.37s") == 1370.0
    assert abs(k6_ms("1m2.3s") - 62300.0) < 1e-6 and k6_ms("2m0s") == 120000.0
    assert k6_ms("n/a") is None


def test_summary_reads_response_times():
    text = ("    http_req_duration..............: avg=52.04ms min=1.15ms "
            "med=6.64ms max=2.26s p(90)=83.81ms p(95)=207.64ms\n"
            "      { expected_response:true }...: avg=50ms min=1ms med=6ms "
            "max=2s p(90)=80ms p(95)=200ms\n"
            "    http_req_failed................: 0.37%  445 out of 117642\n"
            "    http_reqs......................: 117642 919.29/s\n")
    s = parse_k6_summary(text)
    assert (s.avg_ms, s.median_ms, s.p95_ms) == (52.04, 6.64, 207.64)
    assert abs(s.failed - 0.0037) < 1e-12 and s.rate == 919.29


def test_the_top_level_is_felt_though_it_kept_up():
    feels = judge_levels(_SPEC, _SWEEP)
    assert [feels[v].status for v in (1000, 1200, 1600)] == ["ok", "ok", "failing"]
    top = feels[1600]
    assert top.baseline_p95_ms == 43.67
    assert abs(top.limit_p95_ms - 3 * 43.67) < 1e-9     # factor 3 beats the floor
    assert top.slow and top.failing and len(top.reasons) == 2   # judged apart
    assert abs(top.slowdown - 207.64 / 43.67) < 1e-9            # ~4.8x
    assert comfortable(feels) == 1200 and first_strained(feels) is top
    assert plain_strain(top) == (
        "responses were 4.8 times as slow as at the lightest test (95 of every "
        "100 responses within 208 ms, against 44 ms) and 0.37% of requests failed")


def test_slow_but_not_failing_says_both():
    # the case to tell apart: nothing failed, yet responses got 5x slower
    feels = judge_levels(_SPEC, {1000: _k6(40.0), 1600: _k6(200.0)})
    top = feels[1600]
    assert top.status == "slowing" and top.slow and not top.failing
    assert plain_strain(top).endswith(", though no requests failed")
    assert "5.0x slower" in tech_summary(top)
    assert tech_summary(top).endswith("; no failed requests")
    plain = "\n".join(level_explain(top, overloaded=[]))
    assert "Overloaded?       no, every part kept up" in plain
    assert "Failed requests?  none, so that part is fine" in plain
    assert "5.0 times as slow as at the lightest test" in plain
    assert "Too slow: the line is 120 ms" in plain


def test_the_floor_keeps_a_fast_baseline_from_crying_wolf():
    # 15 ms -> 60 ms is 4x, but 60 ms is still under the 100 ms floor
    feels = judge_levels(_SPEC, {200: _k6(15.0), 800: _k6(60.0)})
    assert feels[800].status == "ok" and feels[800].limit_p95_ms == 100.0


def test_a_fixed_budget_replaces_the_relative_rule():
    spec = dict(_SPEC, responsiveness={"max_p95_ms": 50})
    feels = judge_levels(spec, _SWEEP)
    assert feels[1000].status == "ok" and feels[1200].status == "slowing"
    assert comfortable(feels) == 1000


def test_failures_alone_mark_a_level():
    # 18 -> 75 ms is 4.1x, but under the 100 ms floor: errors bad, speed fine
    feels = judge_levels(_SPEC, {200: _k6(18.0), 800: _k6(75.0, failed=0.0056)})
    f = feels[800]
    assert f.status == "failing" and f.failing and not f.slow
    assert f.reasons == ["0.56% of requests failed (limit 0.10%)"]
    assert tech_summary(f).endswith(
        "speed fine (p95 75 ms, 4.2x the lightest level; limit 100 ms)")
    assert plain_strain(f).startswith(
        "0.56% of requests failed, though the speed stayed fine")
    assert plain_strain(f).endswith("; the line is 100 ms)")


def test_a_single_level_uses_only_the_fixed_limits():
    assert judge_one(_SPEC, _k6(900.0)).status == "ok"   # no baseline to compare
    spec = dict(_SPEC, responsiveness={"max_p95_ms": 500})
    assert judge_one(spec, _k6(900.0)).status == "slowing"


def test_levels_without_k6_output_are_unknown_and_skipped():
    out = tempfile.mkdtemp()
    os.makedirs(os.path.join(out, "load-100"))
    with open(os.path.join(out, "load-100", "k6-output.txt"), "w",
              encoding="utf-8") as fh:
        fh.write("    http_req_duration..: avg=10ms min=1ms med=5ms max=90ms "
                 "p(90)=20ms p(95)=30ms\n    http_req_failed....: 0.00%\n")
    feels = judge_sweep_dir(_SPEC, out, [50, 100])
    assert read_k6(os.path.join(out, "load-50", "k6-output.txt")) is None
    assert feels[50].status == "unknown" and feels[100].status == "ok"
    assert feels[100].baseline_p95_ms == 30.0           # first level WITH data
    assert comfortable(feels) == 100


def test_reports_render_and_the_plain_one_has_no_jargon():
    top = judge_levels(_SPEC, _SWEEP)[1600]
    tech = level_report(top)
    assert "Responsiveness" in tech and "-> FAILING" in tech and "-> SLOWING" in tech
    assert "p95 208 ms, 4.8x the lightest level's 44 ms" in tech
    plain = "\n".join(level_explain(top))
    assert "95 of every 100 responses came back within 208 ms" in plain
    assert "Failed requests?  0.37%: more than the 0.10%" in plain
    assert "Too slow" in plain and "Overloaded?" not in plain   # not passed in
    for jargon in ("p95", "rho", "utilization", "percentile"):
        assert jargon not in plain, f"jargon leaked: {jargon}"
    assert level_report(None) == "" and level_explain(None) == []


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
