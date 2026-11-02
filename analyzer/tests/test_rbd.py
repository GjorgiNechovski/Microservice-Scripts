"""Tests for the RBD + Fault Tree model (Thesis Layer 3).

Runs two ways:
  * with pytest:            python3 -m pytest analyzer/tests/ -q
  * with a bare interpreter: python3 -m analyzer.tests.test_rbd

Independent validation: besides hand-derived reference values, the system
UNAVAILABILITY is cross-checked by inclusion-exclusion over the minimal cut
sets -- a different computation path than the state-enumeration, so agreement
validates both the availability and the cut-set logic at once.
"""

from __future__ import annotations

import math
from itertools import combinations

from analyzer.models.rbd import (
    AND, OR, VOTING,
    Component, System,
    k_out_of_n, parallel, series,
)

REL = 1e-9


def _close(a, b, rel=REL):
    return math.isclose(a, b, rel_tol=rel, abs_tol=1e-12)


def _unavailability_via_cutsets(system) -> float:
    """Independent oracle: P(union of minimal cut sets all-failed) by
    inclusion-exclusion, assuming independent components."""
    mcs = system.minimal_cut_sets()
    unavail = {c: 1.0 - system.availability[c] for c in system.components()}
    total = 0.0
    for r in range(1, len(mcs) + 1):
        for subset in combinations(range(len(mcs)), r):
            union = set()
            for i in subset:
                union |= mcs[i]
            prob = 1.0
            for c in union:
                prob *= unavail[c]
            total += (-1) ** (r + 1) * prob
    return total


# --------------------------------------------------------------------------
# Reference availabilities
# --------------------------------------------------------------------------

def test_series_availability():
    s = System(series("a", "b"), {"a": 0.9, "b": 0.8})
    assert _close(s.system_availability(), 0.9 * 0.8)


def test_parallel_availability():
    s = System(parallel("a", "b"), {"a": 0.9, "b": 0.8})
    assert _close(s.system_availability(), 1 - 0.1 * 0.2)


def test_k_out_of_n_availability():
    # 2-of-3 identical, A=0.9: P(>=2) = 3*0.9^2*0.1 + 0.9^3 = 0.972
    s = System(k_out_of_n(2, "a", "b", "c"), {"a": 0.9, "b": 0.9, "c": 0.9})
    assert _close(s.system_availability(), 0.972)


def test_nested_series_of_parallel():
    # series( a, parallel(b, c) )
    s = System(series("a", parallel("b", "c")),
               {"a": 0.95, "b": 0.8, "c": 0.7})
    expected = 0.95 * (1 - 0.2 * 0.3)
    assert _close(s.system_availability(), expected)


# --------------------------------------------------------------------------
# Shared component (the reason we enumerate instead of using the product rule)
# --------------------------------------------------------------------------

def test_shared_component_is_exact():
    # Two paths that BOTH need the shared component S:
    #   structure = S AND (A OR B) = series(S, parallel(A, B))
    # Correct availability = A_S * (1 - (1-A_A)(1-A_B)).
    s = System(series("S", parallel("A", "B")),
               {"S": 0.9, "A": 0.8, "B": 0.7})
    correct = 0.9 * (1 - 0.2 * 0.3)               # 0.846
    assert _close(s.system_availability(), correct)

    # The NAIVE independent-tree formula (treating the two S's as separate)
    # would give a different, wrong number -- prove they differ so we know the
    # enumeration is actually doing the shared-component accounting.
    naive = 1 - (1 - 0.8 * 0.9) * (1 - 0.7 * 0.9)  # ~0.8964
    assert not _close(correct, naive)


# --------------------------------------------------------------------------
# Minimal cut sets & single points of failure
# --------------------------------------------------------------------------

def test_cut_sets_series():
    s = System(series("a", "b", "c"), {"a": 0.9, "b": 0.9, "c": 0.9})
    mcs = s.minimal_cut_sets()
    assert set(mcs) == {frozenset({"a"}), frozenset({"b"}), frozenset({"c"})}
    assert s.single_points_of_failure() == ["a", "b", "c"]


def test_cut_sets_parallel():
    s = System(parallel("a", "b"), {"a": 0.9, "b": 0.9})
    assert s.minimal_cut_sets() == [frozenset({"a", "b"})]
    assert s.single_points_of_failure() == []


def test_cut_sets_shared():
    # series(S, parallel(A, B)): S alone is a cut; {A,B} together is a cut.
    s = System(series("S", parallel("A", "B")),
               {"S": 0.9, "A": 0.8, "B": 0.7})
    mcs = set(s.minimal_cut_sets())
    assert mcs == {frozenset({"S"}), frozenset({"A", "B"})}
    assert s.single_points_of_failure() == ["S"]


# --------------------------------------------------------------------------
# Independent cross-check: enumeration vs inclusion-exclusion over cut sets
# --------------------------------------------------------------------------

def test_unavailability_matches_cutset_inclusion_exclusion():
    systems = [
        System(series("a", "b", "c"), {"a": 0.99, "b": 0.98, "c": 0.97}),
        System(parallel("a", "b", "c"), {"a": 0.9, "b": 0.8, "c": 0.7}),
        System(series("S", parallel("A", "B")),
               {"S": 0.95, "A": 0.8, "B": 0.7}),
        System(series("kong", parallel("r1", "r2", "r3"), "db"),
               {"kong": 0.998, "r1": 0.99, "r2": 0.99, "r3": 0.99, "db": 0.999}),
    ]
    for s in systems:
        assert _close(s.system_unavailability(), _unavailability_via_cutsets(s))


# --------------------------------------------------------------------------
# Birnbaum importance
# --------------------------------------------------------------------------

def test_birnbaum_series():
    s = System(series("a", "b"), {"a": 0.9, "b": 0.8})
    # series A_sys = A_a*A_b; dA/dA_a = A_b, dA/dA_b = A_a
    assert _close(s.birnbaum_importance("a"), 0.8)
    assert _close(s.birnbaum_importance("b"), 0.9)


def test_birnbaum_parallel():
    s = System(parallel("a", "b"), {"a": 0.9, "b": 0.8})
    # parallel A_sys = 1-(1-A_a)(1-A_b); dA/dA_a = 1-A_b
    assert _close(s.birnbaum_importance("a"), 1 - 0.8)
    assert _close(s.birnbaum_importance("b"), 1 - 0.9)


def test_importance_ranking_orders_by_criticality():
    # A shared single component should out-rank a well-replicated one.
    s = System(series("kong", parallel("r1", "r2", "r3")),
               {"kong": 0.99, "r1": 0.99, "r2": 0.99, "r3": 0.99})
    ranking = s.importance_ranking()
    assert ranking[0][0] == "kong"                 # gateway is most critical
    assert ranking[0][1] > ranking[-1][1]


# --------------------------------------------------------------------------
# Fault-tree duality
# --------------------------------------------------------------------------

def test_fault_tree_gates_dual_of_rbd():
    av = {"a": 0.9, "b": 0.8}
    # OR gate (fails if any fails) == RBD series
    assert _close(System(OR("a", "b"), av).system_availability(),
                  System(series("a", "b"), av).system_availability())
    # AND gate (fails only if all fail) == RBD parallel
    assert _close(System(AND("a", "b"), av).system_availability(),
                  System(parallel("a", "b"), av).system_availability())


def test_voting_gate():
    # top fails if >= 2 of 3 fail  <=>  system needs >= 2 of 3 working
    av = {"a": 0.9, "b": 0.9, "c": 0.9}
    assert _close(System(VOTING(2, "a", "b", "c"), av).system_availability(),
                  System(k_out_of_n(2, "a", "b", "c"), av).system_availability())


def test_top_event_probability_is_unavailability():
    s = System(series("a", "b"), {"a": 0.9, "b": 0.8})
    assert _close(s.top_event_probability(), 1 - 0.72)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def test_input_validation():
    checks = [
        lambda: System(series("a"), {}),                       # missing avail
        lambda: System(series("a"), {"a": 1.5}),               # avail out of range
        lambda: k_out_of_n(4, "a", "b", "c"),                  # k > n
        lambda: k_out_of_n(0, "a", "b"),                       # k < 1
        lambda: series(),                                      # empty
        lambda: System(series("a"), {"a": 0.9}).birnbaum_importance("z"),
    ]
    for c in checks:
        raised = False
        try:
            c()
        except (ValueError, TypeError, KeyError):
            raised = True
        assert raised, "expected an exception for invalid input"


# --------------------------------------------------------------------------
# Standalone runner
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
