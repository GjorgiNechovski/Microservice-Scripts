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
    plateau_level,
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


def test_saturation_survives_effective_rate_rounding():
    # The pipeline's second pass analyses each level WITH the recovered
    # service_rate_effective, rounded to 4 decimals. 17.99995 / 3 rounds up to
    # 6.0, so the top level reads rho = 0.9999972 -- just "stable". The verdict
    # must still come from the latency mu, or db silently loses its plateau rate.
    def lvl(v):
        m = _level(v).measurements
        db = dict(m["services"]["db"], arrival_rate=17.99995,
                  service_rate_effective=6.0)
        m = {**m, "services": {**m["services"], "db": db}}
        return LevelResult(v, m, analyze(_SPEC, m), {})

    levels = [lvl(v) for v in (50, 100, 200, 500)]
    assert all(lv.results["services"]["db"].queue.stable for lv in levels)
    db = estimate_capacity(levels, _SPEC, _TARGET)["db"]
    assert db.saturated and db.method == "throughput-plateau"
    assert abs(db.mu_clean - 17.99995 / 3) < 1e-9


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


# api: a thread-pool service under a think-time load (as measured on a .NET API).
# Its throughput keeps pace with the users, 0.59 req/s each, while 1/latency
# falls far below what one replica demonstrably completes -- so the latency rule
# alone calls it overloaded from 800 VUs on.
_CSPEC = {"name": "K", "services": ["api"], "infrastructure": [],
          "operations": {}}
_API_LATENCY_MU = {200: 330.0, 400: 280.0, 800: 200.0, 1600: 40.0}


def _clevel(vus, lam=None):
    lam = round(vus * 0.59, 3) if lam is None else lam
    meas = {"services": {"api": {"replicas": 2, "arrival_rate": lam,
                                 "service_rate": _API_LATENCY_MU[vus],
                                 "failure_rate": 0.01, "repair_rate": 100.0}},
            "infrastructure": {}}
    return LevelResult(vus, meas, analyze(_CSPEC, meas),
                       size_services(_CSPEC, meas, _TARGET))


_KEPT_PACE = [_clevel(v) for v in (200, 400, 800, 1600)]


def test_plateau_level_needs_a_real_drop():
    assert plateau_level([(100, 10.0), (200, 20.0), (400, 37.0)]) is None  # -7.5%
    assert plateau_level([(100, 10.0), (200, 20.0), (400, 30.0)]) == 400   # -25%
    assert plateau_level([(100, 10.0)]) is None                            # no trend
    assert plateau_level([(50, 0.0), (100, 10.0), (200, 20.0)]) is None    # idle skipped


def test_service_that_kept_pace_is_a_floor_not_saturated():
    api = estimate_capacity(_KEPT_PACE, _CSPEC, _TARGET)["api"]
    assert api.lower_bound and not api.saturated
    assert api.method.startswith("lower bound")
    assert abs(api.mu_clean - 944.0 / 2) < 1e-9       # peak / c, as a floor
    assert api.replicas_for_peak is None              # nothing sized from it
    assert api.supported_vus is None and api.top_vus == 1600
    assert system_supported({"api": api}) == (None, None)


def test_service_that_levelled_off_is_still_saturated():
    levels = [_clevel(200), _clevel(400), _clevel(800), _clevel(1600, lam=500.0)]
    api = estimate_capacity(levels, _CSPEC, _TARGET)["api"]
    assert api.saturated and not api.lower_bound
    assert api.method == "throughput-plateau"
    assert abs(api.mu_clean - 250.0) < 1e-9
    assert api.replicas_for_peak == 3                 # ceil(500 / (0.7 * 250))


def test_single_level_keeps_the_latency_rule():
    api = estimate_capacity([_clevel(1600)], _CSPEC, _TARGET)["api"]
    assert api.saturated and not api.lower_bound      # no trend to read


def test_reports_say_the_limit_was_not_reached():
    text = capacity_ceiling_report(_KEPT_PACE, _CSPEC, _TARGET)
    assert "≥472.00" in text and "≥1600" in text
    assert "kept up at every tested level, up to 1600 users" in text
    assert "Un-poisoned sizing" not in text and "replicas;" not in text
    plain = capacity_ceiling_explain(_KEPT_PACE, _CSPEC, _TARGET)
    assert "kept up the whole way" in plain and "up to 1600 users" in plain
    for jargon in ("ρ", "μ", "M/M/c", "utilization"):
        assert jargon not in plain, f"jargon leaked: {jargon}"


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
