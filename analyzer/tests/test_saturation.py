"""Tests for throughput-plateau capacity recovery (analyzer.saturation).

Synthetic sweeps where one service saturates (its throughput plateaus) and
another keeps headroom, checking the clean-μ recovery, the supported-load
figures, and the reports.

  python3 -m pytest analyzer/tests/ -q
  python3 -m analyzer.tests.test_saturation
"""

from __future__ import annotations

from analyzer.capacity import size_services
from analyzer.saturation import (
    capacity_ceiling_explain,
    capacity_ceiling_report,
    estimate_capacity,
    system_supported,
)
from analyzer.scaling import LevelResult
from analyzer.spec import analyze

_SPEC = {"name": "C", "services": ["web", "db"],
         "infrastructure": [], "operations": {}}
_TARGET = 0.7

# db: true capacity 3 replicas × 6 = 18 req/s; its measured (latency) μ collapses
# under load, but achieved throughput PLATEAUS at 18 -> the clean signal.
_DB_LATENCY_MU = {50: 1.5, 100: 0.7, 200: 0.3, 500: 0.1}


def _level(vus: int) -> LevelResult:
    meas = {"services": {
                "web": {"replicas": 3, "arrival_rate": round(vus * 0.05, 3),
                        "service_rate": 10.0, "failure_rate": 0.01,
                        "repair_rate": 100.0},
                "db": {"replicas": 3, "arrival_rate": 18.0,          # plateau
                       "service_rate": _DB_LATENCY_MU[vus],          # collapsing
                       "failure_rate": 0.01, "repair_rate": 100.0}},
            "infrastructure": {}}
    return LevelResult(vus, meas, analyze(_SPEC, meas),
                       size_services(_SPEC, meas, _TARGET))


_LEVELS = [_level(v) for v in (50, 100, 200, 500)]


def test_saturated_service_recovers_mu_from_plateau():
    db = estimate_capacity(_LEVELS, _SPEC, _TARGET)["db"]
    assert db.saturated and db.method == "throughput-plateau"
    assert abs(db.mu_clean - 6.0) < 1e-9          # 18 / 3 replicas
    assert abs(db.ceiling_throughput - 18.0) < 1e-9
    assert abs(db.sustainable_throughput - 12.6) < 1e-9   # 0.7 * 18
    assert db.replicas_for_peak == 5              # ceil(3 / 0.7)
    assert db.supported_vus is None               # no un-saturated sample


def test_unsaturated_service_keeps_latency_mu():
    web = estimate_capacity(_LEVELS, _SPEC, _TARGET)["web"]
    assert not web.saturated and web.method.startswith("latency")
    assert abs(web.mu_clean - 10.0) < 1e-9
    assert web.supported_vus is not None and web.supported_vus > 0


def test_system_supported_blocked_by_saturated_service():
    vus, who = system_supported(estimate_capacity(_LEVELS, _SPEC, _TARGET))
    assert vus is None and who == "db"


def test_system_supported_gives_number_when_all_healthy():
    spec = {"name": "H", "services": ["web"], "infrastructure": [],
            "operations": {}}

    def lvl(v):
        m = {"services": {"web": {"replicas": 3, "arrival_rate": round(v * 0.05, 3),
                                  "service_rate": 10.0, "failure_rate": 0.01,
                                  "repair_rate": 100.0}}, "infrastructure": {}}
        return LevelResult(v, m, analyze(spec, m), size_services(spec, m, _TARGET))

    levels = [lvl(v) for v in (50, 100, 200)]
    vus, who = system_supported(estimate_capacity(levels, spec, _TARGET))
    assert who == "web" and vus is not None and vus > 0


def test_ceiling_report_renders_and_unpoisons():
    text = capacity_ceiling_report(_LEVELS, _SPEC, _TARGET)
    assert "Supported load" in text and "ceiling" in text
    assert "db" in text
    assert "Un-poisoned sizing" in text           # db saturated -> throughput sizing
    assert "5 replicas" in text                    # its recovered clean count


def test_ceiling_explain_is_jargon_free():
    text = capacity_ceiling_explain(_LEVELS, _SPEC, _TARGET)
    assert "db" in text
    for jargon in ("ρ", "μ", "M/M/c", "utilization"):
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
