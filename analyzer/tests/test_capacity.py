"""Tests for capacity sizing (analyzer.capacity) -- the M/M/c replica math.

  python3 -m pytest analyzer/tests/ -q
  python3 -m analyzer.tests.test_capacity
"""

from __future__ import annotations

import math

from analyzer.capacity import (
    capacity_section,
    capacity_target,
    min_replicas,
    size_services,
)
from analyzer.models.queueing import mmc_metrics


def test_no_traffic_needs_one_replica():
    assert min_replicas(0.0, 10.0) == 1


def test_utilization_ceiling_is_respected():
    # smallest c with lam/(c*mu) <= 0.7
    c = min_replicas(8.0, 10.0, target_utilization=0.7)
    assert 8.0 / (c * 10.0) <= 0.7
    assert 8.0 / ((c - 1) * 10.0) > 0.7          # one fewer would breach it


def test_exact_boundary_needs_single_replica():
    # lam/mu = 0.7 exactly -> one replica already sits at the target
    assert min_replicas(7.0, 10.0, target_utilization=0.7) == 1


def test_higher_load_needs_more_replicas():
    low = min_replicas(20.0, 10.0, target_utilization=0.7)
    high = min_replicas(200.0, 10.0, target_utilization=0.7)
    assert high > low


def test_latency_budget_only_raises_c():
    # A tight response budget needs at least as many replicas as the util ceiling
    util_only = min_replicas(20.0, 10.0, target_utilization=0.7)
    with_budget = min_replicas(20.0, 10.0, target_utilization=0.7,
                               max_response=0.12)
    assert with_budget >= util_only
    # and the returned c actually meets the budget
    assert mmc_metrics(20.0, 10.0, with_budget).W <= 0.12


def test_latency_budget_is_monotone():
    tighter = min_replicas(20.0, 10.0, max_response=0.11)
    looser = min_replicas(20.0, 10.0, max_response=0.30)
    assert tighter >= looser


def test_invalid_mu_raises():
    for bad in (0.0, -1.0):
        try:
            min_replicas(5.0, bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for mu={bad}")


def test_invalid_target_raises():
    for bad in (0.0, 1.0, 1.5, -0.1):
        try:
            min_replicas(5.0, 10.0, target_utilization=bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for target={bad}")


def _spec():
    return {"name": "T", "services": ["blog", "comment"],
            "infrastructure": [], "operations": {}}


def _meas(blog_reps, comment_reps, blog_lam=18.0, comment_lam=2.0):
    return {"services": {
                "blog": {"replicas": blog_reps, "arrival_rate": blog_lam,
                         "service_rate": 10.0, "failure_rate": 0.01,
                         "repair_rate": 100.0},
                "comment": {"replicas": comment_reps, "arrival_rate": comment_lam,
                            "service_rate": 10.0, "failure_rate": 0.01,
                            "repair_rate": 100.0}},
            "infrastructure": {}}


def test_size_services_flags_under_and_over():
    # blog: needs ceil(18/(10*0.7))=3, runs 2 -> under (rho=0.9, still reliable)
    # comment: needs 1 but runs 3 -> over
    sizings = size_services(_spec(), _meas(2, 3))
    assert sizings["blog"].verdict == "under" and sizings["blog"].delta > 0
    assert sizings["blog"].reliable is True
    assert sizings["comment"].verdict == "over" and sizings["comment"].delta < 0


def test_size_services_right_sized():
    needed = min_replicas(18.0, 10.0, target_utilization=0.7)
    sizings = size_services(_spec(), _meas(needed, 1))
    assert sizings["blog"].verdict == "ok" and sizings["blog"].delta == 0


def test_sizing_reliable_flag_tracks_overload():
    # rho_current = 18/(2*10) = 0.9 -> reliable; 18/(1*10) = 1.8 -> not
    assert size_services(_spec(), _meas(2, 3))["blog"].reliable is True
    assert size_services(_spec(), _meas(1, 3))["blog"].reliable is False


def test_rho_current_infinite_when_zero_replicas_guard():
    # defensive: a Sizing with current=0 reports infinite load, not a crash
    from analyzer.capacity import Sizing
    s = Sizing("x", lam=5.0, mu=10.0, current=0, needed=1,
               target_utilization=0.7)
    assert s.rho_current == math.inf


def test_capacity_target_from_spec_and_override():
    spec = {"capacity": {"target_utilization": 0.6, "max_response_ms": 250}}
    tu, mr = capacity_target(spec)
    assert tu == 0.6 and abs(mr - 0.25) < 1e-9

    class _Args:
        target_utilization = 0.8
        max_response_ms = None
    tu2, mr2 = capacity_target(spec, _Args())
    assert tu2 == 0.8 and abs(mr2 - 0.25) < 1e-9   # override util, keep ms


def test_capacity_target_defaults():
    tu, mr = capacity_target({})
    assert tu == 0.70 and mr is None


def test_capacity_section_renders_and_names_verdicts():
    text = capacity_section(_spec(), _meas(2, 3))       # both reliable
    assert "Capacity sizing" in text
    assert "blog" in text and "comment" in text
    assert "under-provisioned" in text and "over-provisioned" in text


def test_capacity_section_suppresses_overloaded_sizing():
    # blog overloaded during measurement (rho=1.8) -> flagged, not a bogus number
    text = capacity_section(_spec(), _meas(1, 3))
    assert "could not size" in text
    assert "n/a" in text                                # no invented replica count


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
