"""Tests for the CTMC reliability model (Thesis Layer 2).

Runs two ways:
  * with pytest:            python3 -m pytest analyzer/tests/ -q
  * with a bare interpreter: python3 -m analyzer.tests.test_ctmc

Correctness is validated against hand-derived closed forms AND by a genuine
cross-check: the availability (read from the steady-state solve) must equal
MTTF/(MTTF+MTTR) (read from the independent mean-time-to-absorption solve).
Those two numbers come from different code paths, so agreement is meaningful.
"""

from __future__ import annotations

import math

from analyzer.models.ctmc import (
    CTMC,
    component_reliability,
    replicated_reliability,
    three_state_reliability,
)

REL = 1e-9


def _close(a: float, b: float, rel: float = REL) -> bool:
    return math.isclose(a, b, rel_tol=rel, abs_tol=1e-12)


# --------------------------------------------------------------------------
# Two-state component: closed forms
# --------------------------------------------------------------------------

def test_two_state_reference():
    lam, mu = 0.02, 12.0      # e.g. per hour
    r = component_reliability(lam, mu)
    assert _close(r.availability, mu / (lam + mu))
    assert _close(r.unavailability, lam / (lam + mu))
    assert _close(r.mttf, 1.0 / lam)
    assert _close(r.mttr, 1.0 / mu)
    # renewal relation via independent code paths
    assert _close(r.availability, r.mttf / (r.mttf + r.mttr))


def test_two_state_steady_state_valid():
    r = component_reliability(0.1, 5.0)
    assert _close(sum(r.pi.values()), 1.0)
    assert all(p >= -1e-12 for p in r.pi.values())


# --------------------------------------------------------------------------
# Three-state component (working / failed / recovering)
# --------------------------------------------------------------------------

def test_three_state_reference():
    lam, detect, mu = 0.02, 60.0, 12.0
    r = three_state_reliability(lam, detect, mu)
    # A = 1 / (1 + lam/detect + lam/mu)
    assert _close(r.availability, 1.0 / (1.0 + lam / detect + lam / mu))
    assert _close(r.mttf, 1.0 / lam)
    assert _close(r.mttr, 1.0 / detect + 1.0 / mu)
    assert _close(r.availability, r.mttf / (r.mttf + r.mttr))


def test_three_state_worse_than_two_state():
    # Adding a detection stage (finite detect_rate) can only lengthen MTTR,
    # hence lower availability, vs the instantaneous-detect two-state model.
    lam, mu = 0.05, 10.0
    two = component_reliability(lam, mu)
    three = three_state_reliability(lam, detect_rate=8.0, repair_rate=mu)
    assert three.mttr > two.mttr
    assert three.availability < two.availability


# --------------------------------------------------------------------------
# Replicated k-out-of-n tier
# --------------------------------------------------------------------------

def test_replicated_n1_matches_two_state():
    lam, mu = 0.03, 9.0
    a = replicated_reliability(1, lam, mu, k=1)
    b = component_reliability(lam, mu)
    assert _close(a.availability, b.availability)
    assert _close(a.mttf, b.mttf)
    assert _close(a.mttr, b.mttr)


def test_replicated_n2_reference():
    # n=2, k=1, independent repair. r = lam/mu. All three quantities checked
    # against INDEPENDENT closed forms:
    #   A    = (1 + 2r) / (1 + r)^2
    #   MTTF = 3/(2 lam) + mu/(2 lam^2)          (mean time to FIRST failure)
    #   MTTR = 1/(2 mu)   (from the all-failed state, both replicas repaired @ mu)
    lam, mu = 0.04, 8.0
    r = lam / mu
    res = replicated_reliability(2, lam, mu, k=1, repair_mode="independent")
    assert _close(res.availability, (1.0 + 2.0 * r) / (1.0 + r) ** 2)
    assert _close(res.mttf, 3.0 / (2.0 * lam) + mu / (2.0 * lam ** 2))
    assert _close(res.mttr, 1.0 / (2.0 * mu))
    # NOTE: the simple relation A = MTTF/(MTTF+MTTR) is only APPROXIMATE for a
    # replicated tier (see ReliabilityResult docstring). It must still be close.
    assert _close(res.availability, res.mttf / (res.mttf + res.mttr), rel=1e-3)


def test_renewal_relation_exact_only_for_single_component():
    # Exact for a single component (2-state); merely approximate for n=2.
    lam, mu = 0.04, 8.0
    one = replicated_reliability(1, lam, mu, k=1)
    assert _close(one.availability, one.mttf / (one.mttf + one.mttr))       # exact
    two = replicated_reliability(2, lam, mu, k=1)
    approx = two.mttf / (two.mttf + two.mttr)
    assert not _close(two.availability, approx)                             # NOT exact
    assert _close(two.availability, approx, rel=1e-3)                       # but close


def test_parallel_no_repair_mttf():
    # Two independent non-repairable replicas, system up if >=1 works:
    # classic 1-out-of-2 result MTTF = (1/lam)(1 + 1/2) = 3/(2 lam).
    lam = 0.1
    res = replicated_reliability(2, lam, repair_rate=0.0, k=1)
    assert _close(res.mttf, 3.0 / (2.0 * lam))
    assert math.isinf(res.mttr)          # never repaired -> infinite MTTR
    assert _close(res.availability, 0.0)  # eventually absorbed in all-failed


def test_redundancy_improves_availability_and_mttf():
    lam, mu = 0.05, 6.0
    avails, mttfs = [], []
    for n in (1, 2, 3, 4):
        res = replicated_reliability(n, lam, mu, k=1)
        avails.append(res.availability)
        mttfs.append(res.mttf)
    # More replicas => strictly higher availability and MTTF (k=1).
    for i in range(1, len(avails)):
        assert avails[i] > avails[i - 1]
        assert mttfs[i] > mttfs[i - 1]


def test_shared_repair_worse_than_independent():
    # With one repair crew, the tier spends longer fully-failed => lower A.
    lam, mu = 0.2, 4.0
    ind = replicated_reliability(3, lam, mu, k=1, repair_mode="independent")
    shd = replicated_reliability(3, lam, mu, k=1, repair_mode="shared")
    assert shd.availability < ind.availability


def test_k_of_n_needs_more_replicas():
    # 2-out-of-3 (needs >=2 up) is less available than 1-out-of-3 for same rates.
    lam, mu = 0.1, 5.0
    k1 = replicated_reliability(3, lam, mu, k=1)
    k2 = replicated_reliability(3, lam, mu, k=2)
    assert k2.availability < k1.availability


# --------------------------------------------------------------------------
# Generic CTMC engine
# --------------------------------------------------------------------------

def test_generic_ctmc_steady_state():
    # A hand-built 2-state chain must reproduce the analytic stationary dist.
    lam, mu = 1.0, 3.0
    c = CTMC(["A", "B"], [("A", "B", lam), ("B", "A", mu)])
    pi = c.steady_state()
    assert _close(pi["A"], mu / (lam + mu))
    assert _close(pi["B"], lam / (lam + mu))


def test_generic_ctmc_absorption_matches_two_state():
    lam, mu = 0.5, 2.0
    c = CTMC(["UP", "DOWN"], [("UP", "DOWN", lam), ("DOWN", "UP", mu)])
    assert _close(c.mttf(["UP"], "UP"), 1.0 / lam)
    assert _close(c.mttr(["UP"], "DOWN"), 1.0 / mu)


def test_nines_helper():
    r = component_reliability(1.0, 999.0)  # A = 999/1000 = 0.999
    assert _close(r.availability, 0.999)
    assert _close(r.nines(), 3.0, rel=1e-6)


def test_input_validation():
    checks = [
        lambda: CTMC(["A", "A"], []),                       # duplicate state
        lambda: CTMC(["A", "B"], [("A", "A", 1.0)]),        # self-transition
        lambda: CTMC(["A", "B"], [("A", "C", 1.0)]),        # unknown state
        lambda: CTMC(["A", "B"], [("A", "B", -1.0)]),       # negative rate
        lambda: replicated_reliability(0, 1.0, 1.0),        # n < 1
        lambda: replicated_reliability(2, 1.0, 1.0, k=3),   # k > n
        lambda: replicated_reliability(2, 1.0, 1.0, repair_mode="x"),
    ]
    for c in checks:
        raised = False
        try:
            c()
        except (ValueError, TypeError):
            raised = True
        assert raised, "expected an exception for invalid input"


# --------------------------------------------------------------------------
# Standalone runner (no pytest required)
# --------------------------------------------------------------------------

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
