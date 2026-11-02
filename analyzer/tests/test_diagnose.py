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
