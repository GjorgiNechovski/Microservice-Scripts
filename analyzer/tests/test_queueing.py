"""Tests for the M/M/c queueing model (Thesis Layer 1).

Runs two ways:
  * with pytest:            python3 -m pytest analyzer/tests/ -q
  * with a bare interpreter: python3 -m analyzer.tests.test_queueing

Correctness is validated three ways, none of which just re-runs the
implementation:
  1. Hand-derived closed-form reference values (M/M/1, M/M/2, M/M/3).
  2. An INDEPENDENT exact-arithmetic oracle (``_erlang_exact``) built with
     ``fractions.Fraction`` and the direct factorial summation -- a different
     algorithm from the module's Erlang-B recurrence -- checked across a range
     of ``c`` including large values.
  3. Structural invariants and regression tests for known edge cases.
"""

from __future__ import annotations

import math
from fractions import Fraction as F

from analyzer.models.queueing import QueueMetrics, erlang_c, mmc_metrics

REL = 1e-9  # relative tolerance for exact closed-form comparisons


def _close(a: float, b: float, rel: float = REL) -> bool:
    return math.isclose(a, b, rel_tol=rel, abs_tol=1e-12)


# --------------------------------------------------------------------------
# Independent exact oracle (exact rational arithmetic, direct summation).
# Deliberately uses a DIFFERENT algorithm than the module (which uses the
# Erlang-B recurrence), so agreement is a genuine cross-check, not a tautology.
# --------------------------------------------------------------------------

def _erlang_exact(lam: int, mu: int, c: int) -> dict:
    a = F(lam, mu)
    rho = a / c
    assert rho < 1, "oracle only defined for stable systems"
    terms = [F(1)]
    for n in range(1, c + 1):
        terms.append(terms[-1] * a / n)
    denom = sum(terms[:c]) + terms[c] / (1 - rho)
    p0 = 1 / denom
    p_wait = terms[c] / (1 - rho) * p0
    Lq = p_wait * rho / (1 - rho)
    L = Lq + a
    Wq = Lq / F(lam)
    W = Wq + F(1, mu)
    return {k: float(v) for k, v in
            dict(p0=p0, p_wait=p_wait, Lq=Lq, L=L, Wq=Wq, W=W).items()}


def test_matches_exact_oracle_across_c():
    # Includes large c (9, 12) where a summation/off-by-one bug would surface
    # but internal identities and monotonicity would NOT catch it.
    cases = [(1, 2, 1), (1, 1, 2), (2, 1, 3), (7, 2, 5), (30, 4, 9), (50, 6, 12)]
    for lam, mu, c in cases:
        exact = _erlang_exact(lam, mu, c)
        m = mmc_metrics(float(lam), float(mu), c)
        for key in ("p0", "p_wait", "Lq", "L", "Wq", "W"):
            got, want = getattr(m, key), exact[key]
            assert _close(got, want), f"M/M/{c}(lam={lam},mu={mu}) {key}: {got} != {want}"


# --------------------------------------------------------------------------
# Hand-derived closed-form reference cases
# --------------------------------------------------------------------------

def test_mm1_reference():
    # M/M/1, lam=0.5, mu=1  => rho=0.5
    #   W = 1/(mu-lam) = 2 ; Wq = rho/(mu-lam) = 1 ; Lq = rho^2/(1-rho) = 0.5
    #   L = rho/(1-rho) = 1 ; p0 = 1-rho = 0.5 ; p_wait = rho = 0.5
    m = mmc_metrics(lam=0.5, mu=1.0, c=1)
    assert m.stable
    assert _close(m.rho, 0.5)
    assert _close(m.p0, 0.5)
    assert _close(m.p_wait, 0.5)
    assert _close(m.Lq, 0.5)
    assert _close(m.L, 1.0)
    assert _close(m.Wq, 1.0)
    assert _close(m.W, 2.0)
    assert _close(m.throughput, 0.5)


def test_mm2_reference():
    # M/M/2, lam=1, mu=1 (a=1, rho=0.5)
    #   p0 = 1/3 ; Erlang-C = 1/3 ; Lq = 1/3 ; L = 4/3 ; Wq = 1/3 ; W = 4/3
    m = mmc_metrics(lam=1.0, mu=1.0, c=2)
    assert _close(m.p0, 1.0 / 3.0)
    assert _close(m.p_wait, 1.0 / 3.0)
    assert _close(m.Lq, 1.0 / 3.0)
    assert _close(m.L, 4.0 / 3.0)
    assert _close(m.Wq, 1.0 / 3.0)
    assert _close(m.W, 4.0 / 3.0)


def test_mm3_erlang_c_reference():
    # M/M/3, a=2 (lam=2, mu=1) => Erlang-C(3,2) = 4/9, p0 = 1/9
    m = mmc_metrics(lam=2.0, mu=1.0, c=3)
    assert _close(m.p_wait, 4.0 / 9.0)
    assert _close(m.p0, 1.0 / 9.0)
    assert _close(erlang_c(2.0, 3), 4.0 / 9.0)


def test_mm5_reference_values():
    # Independently computed (fractions): M/M/5, lam=7, mu=2.
    m = mmc_metrics(lam=7.0, mu=2.0, c=5)
    assert _close(m.p_wait, 0.3778382267)
    assert _close(m.Lq, 0.8816225290)
    assert _close(m.L, 4.3816225290)
    assert _close(m.p0, 0.0258981161)


# --------------------------------------------------------------------------
# Structural invariants (necessary, but not sufficient on their own -- the
# oracle test above supplies the independent numeric anchors)
# --------------------------------------------------------------------------

def test_structural_invariants():
    for lam, mu, c in [(0.5, 1.0, 1), (1.0, 1.0, 2), (2.0, 1.0, 3),
                       (7.0, 2.0, 5), (30.0, 4.0, 9)]:
        m = mmc_metrics(lam, mu, c)
        assert m.stable
        # Little's law
        assert _close(m.L, m.lam * m.W)
        assert _close(m.Lq, m.lam * m.Wq)
        # Decomposition and ordering
        assert _close(m.L, m.Lq + m.a)
        assert _close(m.W, m.Wq + 1.0 / m.mu)
        assert m.L >= m.Lq - 1e-12          # system count >= queue count
        assert m.L >= m.a - 1e-12           # system count >= servers busy
        # Throughput of a stable queue equals its arrival rate
        assert _close(m.throughput, m.lam)


def test_probabilities_are_valid():
    for lam, mu, c in [(0.5, 1.0, 1), (1.0, 1.0, 2), (2.0, 1.0, 3), (7.0, 2.0, 5)]:
        m = mmc_metrics(lam, mu, c)
        assert 0.0 <= m.p0 <= 1.0
        assert 0.0 <= m.p_wait <= 1.0
        assert m.Lq >= 0.0 and m.Wq >= 0.0


def test_p_wait_monotonic_in_c():
    # For a fixed offered load, adding servers cannot increase the wait prob.
    a = 2.5
    prev = None
    for c in range(3, 12):
        pw = erlang_c(a, c)
        if prev is not None:
            assert pw <= prev + 1e-12
        prev = pw


def test_more_replicas_reduce_response_time():
    lam, mu = 2.5, 1.0
    prev = None
    for c in range(3, 8):
        m = mmc_metrics(lam, mu, c)
        assert m.stable
        if prev is not None:
            assert m.W < prev.W
            assert m.Wq < prev.Wq
            assert m.rho < prev.rho
        prev = m


def test_mm1_matches_general_formula():
    for lam, mu in [(0.3, 1.0), (0.9, 2.0), (4.0, 5.0)]:
        m = mmc_metrics(lam, mu, 1)
        assert _close(m.W, 1.0 / (mu - lam))
        assert _close(m.Wq, lam / (mu * (mu - lam)))
        assert _close(m.L, lam / (mu - lam))


# --------------------------------------------------------------------------
# Edge cases & regression tests for previously-found bugs
# --------------------------------------------------------------------------

def test_zero_arrivals():
    m = mmc_metrics(lam=0.0, mu=5.0, c=3)
    assert m.stable
    assert m.Lq == 0.0 and m.L == 0.0
    assert _close(m.W, 1.0 / 5.0)
    assert m.throughput == 0.0


def test_unstable_queue():
    m = mmc_metrics(lam=3.0, mu=1.0, c=2)   # a=3 > c=2
    assert not m.stable
    assert math.isinf(m.W) and math.isinf(m.Wq)
    assert math.isinf(m.Lq) and math.isinf(m.L)
    assert _close(m.throughput, 2.0)        # saturated: c*mu = 2


def test_critical_load_is_unstable():
    # Regression: lam == c*mu (true rho == 1) must be reported unstable, not a
    # finite ~1e16 nonsense value from dividing by a rounding residue.
    m = mmc_metrics(lam=0.15, mu=0.05, c=3)  # 0.05*3 == 0.15
    assert not m.stable
    assert math.isinf(m.Lq)


def test_no_overflow_nan_for_huge_load():
    # Regression: a >= ~715 used to overflow a**n/n! and yield NaN metrics.
    m = mmc_metrics(lam=745.0, mu=1.0, c=746)  # rho ~ 0.9987, stable
    assert m.stable
    for v in (m.p0, m.p_wait, m.Lq, m.L, m.Wq, m.W):
        assert not math.isnan(v), "metric is NaN -- overflow regression"
    assert 0.0 <= m.p_wait <= 1.0
    assert m.Lq >= 0.0 and math.isfinite(m.Lq)


def test_input_validation():
    for bad in [
        lambda: mmc_metrics(-1.0, 1.0, 1),
        lambda: mmc_metrics(1.0, 0.0, 1),
        lambda: mmc_metrics(1.0, -2.0, 1),
        lambda: mmc_metrics(1.0, 1.0, 0),
        lambda: mmc_metrics(1.0, 1.0, 2.5),
    ]:
        raised = False
        try:
            bad()
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
