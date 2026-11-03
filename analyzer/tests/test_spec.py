"""Tests for the spec loader / analysis glue (analyzer.spec).

Focus: the optional user-supplied ``[failure_rates]`` override and its
precedence. Runs with pytest or as a bare script.
"""

from __future__ import annotations

import math

from analyzer.spec import (_validate_system, analyze, default_measurements,
                           failure_rate_for, infra_instances,
                           unmeasured_recovery)


def _close(a, b):
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


def test_failure_rate_precedence():
    spec = {"failure_rates": {"db": 0.0002, "default": 0.001}}
    assert failure_rate_for(spec, "db", 0.05) == 0.0002    # explicit entry wins
    assert failure_rate_for(spec, "svc", 0.05) == 0.001    # then the default
    assert failure_rate_for({}, "svc", 0.05) == 0.05       # else measured value


def test_analyze_applies_failure_rate_override():
    spec = {
        "name": "t", "services": ["svc"], "infrastructure": ["db"],
        "operations": {"op": ["svc", "db"]},
        "failure_rates": {"svc": 0.001, "db": 0.0002},
    }
    meas = {
        "services": {"svc": {"replicas": 1, "failure_rate": 0.05,
                             "repair_rate": 100.0, "arrival_rate": 1.0,
                             "service_rate": 10.0}},
        "infrastructure": {"db": {"failure_rate": 0.05, "repair_rate": 100.0}},
    }
    result = analyze(spec, meas)
    # availability = repair / (failure + repair), using the OVERRIDES not 0.05
    assert _close(result["services"]["svc"].per_replica_availability,
                  100.0 / (0.001 + 100.0))
    assert _close(result["infrastructure"]["db"].availability,
                  100.0 / (0.0002 + 100.0))


def test_analyze_without_override_uses_measured():
    spec = {"name": "t", "services": ["svc"], "infrastructure": [],
            "operations": {}}
    meas = {"services": {"svc": {"replicas": 1, "failure_rate": 0.02,
                                 "repair_rate": 10.0, "arrival_rate": 1.0,
                                 "service_rate": 10.0}},
            "infrastructure": {}}
    result = analyze(spec, meas)
    assert _close(result["services"]["svc"].per_replica_availability,
                  10.0 / (0.02 + 10.0))


def test_effective_service_rate_fixes_concurrent_overload():
    """A concurrent single server must not read as overloaded.

    isotope gamecenterapi @ 25 VUs: latency-μ = 48.7 (poisoned), but the
    throughput plateau reveals a clean per-replica rate of ~273. With c=1 the
    latency-μ says ρ=1.97 (false overload); the effective rate says ρ≈0.35.
    """
    spec = {"name": "t", "services": ["api"], "infrastructure": [],
            "operations": {}}
    base = {"replicas": 1, "failure_rate": 0.02, "repair_rate": 100.0,
            "arrival_rate": 95.9, "service_rate": 48.7}

    # Without the effective rate: latency-μ -> overloaded (ρ ~ 1.97).
    poisoned = analyze(spec, {"services": {"api": dict(base)},
                              "infrastructure": {}})
    assert not poisoned["services"]["api"].queue.stable
    assert poisoned["services"]["api"].queue.rho > 1.5

    # With the effective (throughput-clean) rate: comfortable (~35%).
    clean = analyze(spec, {"services": {"api": {**base,
                    "service_rate_effective": 273.0}}, "infrastructure": {}})
    q = clean["services"]["api"].queue
    assert q.stable
    assert _close_rel(q.rho, 95.9 / 273.0)
    assert 0.30 < q.rho < 0.40


def test_effective_rate_does_not_touch_reliability():
    """Layer 2 uses the replica COUNT, not concurrency: a single concurrent
    box is still one point of failure even with a huge effective rate."""
    spec = {"name": "t", "services": ["api"], "infrastructure": [],
            "operations": {}}
    meas = {"services": {"api": {"replicas": 1, "failure_rate": 0.02,
            "repair_rate": 10.0, "arrival_rate": 10.0, "service_rate": 5.0,
            "service_rate_effective": 500.0}}, "infrastructure": {}}
    r = analyze(spec, meas)["services"]["api"]
    assert r.replicas == 1                                   # not inflated
    assert _close(r.per_replica_availability, 10.0 / (0.02 + 10.0))


def _close_rel(a, b):
    return math.isclose(a, b, rel_tol=1e-6)


# ---------------------------------------------------------------------------
# [instances] -- declared redundancy for infrastructure components
# ---------------------------------------------------------------------------

def _redundancy_spec(instances=None):
    spec = {
        "services": ["svc"],
        "infrastructure": ["gw"],
        "operations": {"op": ["gw", "svc"]},
        "failure_rates": {"default": 0.01},
    }
    if instances is not None:
        spec["instances"] = instances
    return spec


_REDUNDANCY_MEAS = {
    "services": {"svc": {"replicas": 2, "failure_rate": 0.01,
                         "repair_rate": 100.0, "arrival_rate": 1.0,
                         "service_rate": 10.0}},
    "infrastructure": {"gw": {"failure_rate": 0.01, "repair_rate": 100.0}},
}


def test_infra_instances_defaults_to_one():
    assert infra_instances(_redundancy_spec()) == {"gw": 1}
    assert infra_instances(_redundancy_spec({"gw": 3})) == {"gw": 3}


def test_instances_must_name_infrastructure():
    for bad in ({"svc": 2}, {"nope": 2}):
        try:
            _validate_system(_redundancy_spec(bad))
        except ValueError:
            continue
        raise AssertionError(f"{bad} should have been rejected")


def test_instances_must_be_a_positive_int():
    for bad in ({"gw": 0}, {"gw": -1}, {"gw": 1.5}):
        try:
            _validate_system(_redundancy_spec(bad))
        except ValueError:
            continue
        raise AssertionError(f"{bad} should have been rejected")


def test_declared_redundancy_removes_the_spof_and_lifts_availability():
    single = analyze(_redundancy_spec(), _REDUNDANCY_MEAS)["top_events"]["op"]
    double = analyze(_redundancy_spec({"gw": 2}),
                     _REDUNDANCY_MEAS)["top_events"]["op"]

    assert any("gw" in c for c in single.spofs)       # one gw is a SPOF
    assert not any("gw" in c for c in double.spofs)   # two are not
    assert double.availability > single.availability


def test_intervention_labels_count_from_the_current_instances():
    single = analyze(_redundancy_spec(), _REDUNDANCY_MEAS)["top_events"]["op"]
    double = analyze(_redundancy_spec({"gw": 2}),
                     _REDUNDANCY_MEAS)["top_events"]["op"]

    assert any(lbl == "add 2nd gw" for lbl, _, _ in single.interventions)
    assert any(lbl == "add 3rd gw" for lbl, _, _ in double.interventions)


def test_redundancy_shrinks_the_gain_from_more_of_the_same():
    """Second instance helps a lot; a third adds almost nothing."""
    def gain(instances):
        ev = analyze(_redundancy_spec(instances),
                     _REDUNDANCY_MEAS)["top_events"]["op"]
        return dict((l, s) for l, _, s in ev.interventions)

    first = gain(None)["add 2nd gw"]
    second = gain({"gw": 2})["add 3rd gw"]
    assert first > second >= 0.0


def test_unmeasured_recovery_flags_placeholders_only():
    spec = {"name": "x", "services": ["svc"], "infrastructure": ["db"],
            "operations": {}}
    assert unmeasured_recovery(spec, default_measurements(spec)) == ["svc", "db"]
    # a file without the marker (every file written before it existed) is measured
    measured = {"services": {"svc": {"repair_rate": 100.0}},
                "infrastructure": {"db": {"repair_rate": 50.0}}}
    assert unmeasured_recovery(spec, measured) == []


def test_compose_project_must_be_a_name():
    base = {"services": ["svc"], "infrastructure": [], "operations": {}}
    _validate_system({**base, "compose_project": "shop"})
    for bad in ("", 3):
        try:
            _validate_system({**base, "compose_project": bad})
        except ValueError:
            continue
        raise AssertionError(f"accepted compose_project={bad!r}")


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
