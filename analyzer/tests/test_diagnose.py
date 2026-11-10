"""Tests for the scale-out-vs-optimize classifier (analyzer.diagnose).

  python3 -m pytest analyzer/tests/ -q
  python3 -m analyzer.tests.test_diagnose
"""

from __future__ import annotations

import os
import tempfile

from analyzer.capacity import size_services
from analyzer.diagnose import diagnose, diagnosis_explain, diagnosis_report
from analyzer.scaling import LevelResult
from analyzer.spec import analyze

_TARGET = 0.7
# heavy  -> overloaded + slow request (low clean μ)  -> optimize
# cheap  -> overloaded + fast request (high clean μ) -> add replicas
# idle   -> not overloaded                            -> ok
_SPEC = {"name": "D", "services": ["heavy", "cheap", "idle"],
         "infrastructure": [], "operations": {}}


def _levels():
    meas = {"services": {
                # overloaded (ρ»1), throughput plateaus at 5 -> μ_true=5 -> S=200ms
                "heavy": {"replicas": 1, "arrival_rate": 5.0, "service_rate": 0.5,
                          "failure_rate": 0.01, "repair_rate": 100.0},
                # overloaded, plateaus at 200 -> μ_true=200 -> S=5ms
                "cheap": {"replicas": 1, "arrival_rate": 200.0, "service_rate": 20.0,
                          "failure_rate": 0.01, "repair_rate": 100.0},
                # light load, fast -> not overloaded
                "idle": {"replicas": 3, "arrival_rate": 3.0, "service_rate": 50.0,
                         "failure_rate": 0.01, "repair_rate": 100.0}},
            "infrastructure": {}}
    return [LevelResult(100, meas, analyze(_SPEC, meas),
                        size_services(_SPEC, meas, _TARGET))]


_LEVELS = _levels()


def test_heavy_overloaded_service_routes_to_optimize():
    dr = diagnose(_LEVELS, _SPEC, _TARGET)
    h = dr.diagnoses["heavy"]
    assert h.overloaded and h.heavy_request
    assert h.verdict == "optimize"
    assert h.service_time_ms > 100          # ~200 ms/request


def test_cheap_overloaded_service_routes_to_scale():
    dr = diagnose(_LEVELS, _SPEC, _TARGET)
    c = dr.diagnoses["cheap"]
    assert c.overloaded and not c.heavy_request
    assert c.verdict == "scale"
    assert c.service_time_ms < 20           # ~5 ms/request


def test_unloaded_service_is_ok():
    dr = diagnose(_LEVELS, _SPEC, _TARGET)
    assert dr.diagnoses["idle"].verdict == "ok"


def test_heavy_factor_is_configurable():
    # a huge factor makes nothing count as "heavy" -> the heavy one becomes scale
    spec = dict(_SPEC, diagnose={"heavy_factor": 1000.0})
    dr = diagnose(_LEVELS, spec, _TARGET)
    assert dr.diagnoses["heavy"].verdict == "scale"


def test_report_and_explain_render():
    dr = diagnose(_LEVELS, _SPEC, _TARGET)
    rep = diagnosis_report(dr, _SPEC)
    assert "OPTIMIZE" in rep and "ADD REPLICAS" in rep
    assert "heavy" in rep and "cheap" in rep
    exp = diagnosis_explain(dr, _SPEC)
    assert "heavy" in exp and "copies" in exp


def test_explain_is_plain_language():
    text = diagnosis_explain(diagnose(_LEVELS, _SPEC, _TARGET), _SPEC)
    for jargon in ("ρ", "μ", "M/M/c", "utilization", "MTTR"):
        assert jargon not in text, f"jargon leaked: {jargon}"


def test_quadrant_figure_renders():
    try:
        from analyzer.figures.plots import fig_diagnose_quadrant
    except ImportError:
        print("  (skip: matplotlib not installed)")
        return
    out = tempfile.mkdtemp()
    p = fig_diagnose_quadrant(_SPEC, diagnose(_LEVELS, _SPEC, _TARGET), out)
    assert os.path.exists(p) and os.path.getsize(p) > 0


def test_a_service_that_kept_pace_is_not_sent_to_scale():
    # throughput 0.59 req/s per user at every level while 1/latency collapses:
    # a concurrent service whose limit the sweep never reached
    spec = {"name": "K", "services": ["api"], "infrastructure": [],
            "operations": {}}
    levels = []
    for vus, mu in ((200, 330.0), (800, 200.0), (1600, 40.0)):
        meas = {"services": {"api": {"replicas": 2, "arrival_rate": vus * 0.59,
                                     "service_rate": mu,
                                     "service_rate_effective": 472.0,
                                     "service_rate_lower_bound": True,
                                     "failure_rate": 0.01, "repair_rate": 100.0}},
                "infrastructure": {}}
        levels.append(LevelResult(vus, meas, analyze(spec, meas),
                                  size_services(spec, meas, _TARGET)))
    dr = diagnose(levels, spec, _TARGET)
    api = dr.diagnoses["api"]
    assert api.lower_bound and not api.overloaded and api.verdict == "ok"
    text = diagnosis_report(dr, spec)
    assert "ok — limit not reached" in text and "ADD REPLICAS" not in text
    plain = diagnosis_explain(dr, spec)
    assert "never ran out of room" in plain and "add copies" not in plain.lower()


# A sweep that levels off while the machine idles (the measured case: a .NET API
# capped by its database connection pool). Throughput keeps pace up to 1600
# users, then falls at 2000 while the API's CPU falls too; the host never used
# more than ~9 of 22 cores. ``host`` scales every container's CPU to test the
# CPU-bound counterpart.
def _plateau_levels(host=1.0, with_cpu=True):
    from analyzer.validity import Validity
    spec = {"name": "P", "services": ["gw", "api"], "infrastructure": [],
            "operations": {}}
    levels = []
    for vus, lam, mu, api_cpu, gw_cpu in ((200, 118, 330.0, 0.4, 0.14),
                                          (800, 472, 200.0, 1.7, 0.5),
                                          (1200, 700, 120.0, 3.1, 0.75),
                                          (1600, 915, 40.0, 7.8, 0.7),
                                          (2000, 420, 3.0, 2.5, 0.2)):
        # as the pipeline writes them: the plateau's rate (915 / 1 replica) is
        # each level's effective rate; the latency one is kept beside it
        meas = {"services": {n: {"replicas": 1, "arrival_rate": lam,
                                 "service_rate": mu,
                                 "service_rate_effective": 915.0,
                                 "failure_rate": 0.01,
                                 "repair_rate": 100.0} for n in ("gw", "api")},
                "infrastructure": {}}
        validity = (Validity(f"load @ {vus}", [], 22.0,
                             {"api": api_cpu * host, "gw": gw_cpu * host,
                              "mongo": 0.4 * host}) if with_cpu else None)
        levels.append(LevelResult(vus, meas, analyze(spec, meas),
                                  size_services(spec, meas, _TARGET), validity))
    return spec, levels


def test_a_plateau_with_idle_cpu_is_a_hidden_limit():
    spec, levels = _plateau_levels()
    dr = diagnose(levels, spec, _TARGET)
    assert dr.hidden is not None and dr.hidden.vus == 1600
    assert all(dr.diagnoses[n].verdict == "hidden" for n in ("gw", "api"))
    assert dr.hidden.fall[:3] == (2000, 420, "api")     # fell, busiest = api
    tech = diagnosis_report(dr, spec)
    assert "HIDDEN LIMIT — not CPU" in tech and "ADD REPLICAS" not in tech
    assert "the limit is not CPU" in tech
    assert "8.9 of 22 cores" in tech                     # 7.8 + 0.7 + 0.4
    assert "work was refused, not done" in tech
    plain = diagnosis_explain(dr, spec)
    assert "not because" in plain and "turning work away" in plain
    assert "add copies" not in plain.lower().replace("more copies help only", "")
    for jargon in ("ρ", "μ", "M/M/c", "utilization", "replica"):
        assert jargon not in plain, f"jargon leaked: {jargon}"


def test_a_plateau_on_a_busy_host_still_says_add_replicas():
    spec, levels = _plateau_levels(host=2.0)              # 17.8 of 22 cores
    dr = diagnose(levels, spec, _TARGET)
    assert dr.hidden is None
    assert all(dr.diagnoses[n].verdict == "scale" for n in ("gw", "api"))


def test_without_cpu_numbers_the_verdict_is_unchanged():
    spec, levels = _plateau_levels(with_cpu=False)
    dr = diagnose(levels, spec, _TARGET)
    assert dr.hidden is None and dr.diagnoses["api"].verdict == "scale"


def test_each_level_knows_where_it_stood_against_the_limit():
    from analyzer.diagnose import level_position_report, sweep_position
    spec, levels = _plateau_levels()
    pos = sweep_position(levels, spec, _TARGET)
    assert (pos.peak_vus, pos.limit_vus) == (1600, 2000) and pos.hidden is not None
    states = {lv.vus: pos.at(lv.vus, lv.measurements, spec).state for lv in levels}
    assert states == {200: "below", 800: "below", 1200: "below", 1600: "at",
                      2000: "past"}
    past = pos.at(2000, levels[-1].measurements, spec)
    assert abs(past.share - 420 / 915) < 1e-9
    text = level_position_report(past)
    assert "PAST the limit" in text and "read this level as overloaded" in text
    assert "hidden, not CPU" in text
    # a sweep that never levelled off has no position
    assert sweep_position(levels[:2], spec, _TARGET) is None


def test_quadrant_figure_draws_a_hidden_limit():
    try:
        from analyzer.figures.plots import fig_diagnose_quadrant
    except ImportError:
        print("  (skip: matplotlib not installed)")
        return
    spec, levels = _plateau_levels()
    p = fig_diagnose_quadrant(spec, diagnose(levels, spec, _TARGET), tempfile.mkdtemp())
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
