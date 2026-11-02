"""Reliability Block Diagram + Fault Tree -- Thesis Layer 3.

Structural reliability: given the availability of each component (typically from
Layer 2, the CTMC), compose them by the SYSTEM'S STRUCTURE (series / parallel /
k-out-of-n) to get whole-system availability, and identify weak points via
minimal cut sets and single points of failure.

Why exact enumeration (not the product formula)
------------------------------------------------
The textbook shortcuts -- ``A_series = prod(A_i)``, ``A_parallel = 1 -
prod(1-A_i)`` -- are only valid when every block is a DISTINCT, independent
component. Real systems share components (e.g. one database behind several
services, one API gateway in front of everything). A shared component is a
*common cause*: the naive tree double-counts it and gets the wrong number.

This module instead evaluates the structure function over all 2^n component
states and weights by probability, which is EXACT even with shared components
(at the cost of being exponential -- fine for the modest component counts of a
Reliability Block Diagram, which we cap).

RBD <-> Fault Tree duality
--------------------------
An RBD describes SUCCESS (system up). A fault tree describes the top FAILURE
event. They are De-Morgan duals:
    RBD series (all must work)   <->  FT OR  gate (fails if any fails)
    RBD parallel (any works)     <->  FT AND gate (fails if all fail)
So the RBD's minimal cut sets and ``unavailability`` ARE the fault-tree
top-event analysis; ``AND`` / ``OR`` / ``VOTING`` constructors are provided for
users who prefer to think in fault-tree terms.

No third-party dependencies::

    python3 -m analyzer.tests.test_rbd
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

__all__ = [
    "Component", "Series", "Parallel", "KOutOfN",
    "series", "parallel", "k_out_of_n",
    "AND", "OR", "VOTING",
    "System",
]

# Guards: enumeration is 2^n. These are generous for an RBD but stop runaway.
_MAX_ENUM = 22          # availability enumeration
_MAX_CUTSET = 18        # minimal-cut-set search


# --------------------------------------------------------------------------
# Structure nodes (a coherent success structure function)
# --------------------------------------------------------------------------

class _Node:
    def evaluate(self, state: dict) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def components(self) -> set:              # pragma: no cover - interface
        raise NotImplementedError


@dataclass(frozen=True)
class Component(_Node):
    """A leaf: the block works iff its named component is up."""
    name: str

    def evaluate(self, state):
        return state[self.name]

    def components(self):
        return {self.name}


@dataclass(frozen=True)
class Series(_Node):
    """Works iff ALL parts work (any one failing brings it down)."""
    parts: tuple

    def evaluate(self, state):
        return all(p.evaluate(state) for p in self.parts)

    def components(self):
        s = set()
        for p in self.parts:
            s |= p.components()
        return s


@dataclass(frozen=True)
class Parallel(_Node):
    """Works iff AT LEAST ONE part works (redundancy)."""
    parts: tuple

    def evaluate(self, state):
        return any(p.evaluate(state) for p in self.parts)

    def components(self):
        s = set()
        for p in self.parts:
            s |= p.components()
        return s


@dataclass(frozen=True)
class KOutOfN(_Node):
    """Works iff at least ``k`` of the parts work."""
    k: int
    parts: tuple

    def evaluate(self, state):
        return sum(1 for p in self.parts if p.evaluate(state)) >= self.k

    def components(self):
        s = set()
        for p in self.parts:
            s |= p.components()
        return s


def _coerce(x) -> _Node:
    return x if isinstance(x, _Node) else Component(x)


def series(*parts) -> Series:
    """RBD series block (strings are auto-wrapped as components)."""
    if not parts:
        raise ValueError("series needs at least one part")
    return Series(tuple(_coerce(p) for p in parts))


def parallel(*parts) -> Parallel:
    """RBD parallel block."""
    if not parts:
        raise ValueError("parallel needs at least one part")
    return Parallel(tuple(_coerce(p) for p in parts))


def k_out_of_n(k: int, *parts) -> KOutOfN:
    """RBD k-out-of-n block (works iff >= k of the parts work)."""
    parts = tuple(_coerce(p) for p in parts)
    if not (1 <= k <= len(parts)):
        raise ValueError("k must satisfy 1 <= k <= number of parts")
    return KOutOfN(k, parts)


# -- Fault-tree-flavoured constructors (duals of the RBD blocks) -----------

def OR(*events) -> Series:
    """Fault-tree OR gate: top event fails if ANY input fails.

    Dual of an RBD series block. Use when thinking in failure terms.
    """
    return series(*events)


def AND(*events) -> Parallel:
    """Fault-tree AND gate: top event fails only if ALL inputs fail.

    Dual of an RBD parallel block.
    """
    return parallel(*events)


def VOTING(k_fail: int, *events) -> KOutOfN:
    """Fault-tree voting gate: top fails if >= ``k_fail`` of n inputs fail.

    Dual: the system survives iff >= (n - k_fail + 1) inputs work.
    """
    n = len(events)
    if not (1 <= k_fail <= n):
        raise ValueError("k_fail must satisfy 1 <= k_fail <= number of events")
    return k_out_of_n(n - k_fail + 1, *events)


# --------------------------------------------------------------------------
# System: probabilistic + structural analysis
# --------------------------------------------------------------------------

@dataclass
class System:
    """A structure plus each component's availability.

    Parameters
    ----------
    structure : the top _Node of the RBD / fault tree.
    availability : dict mapping every component name to its availability in [0,1].
    """

    structure: _Node
    availability: dict

    def __post_init__(self):
        comps = self.structure.components()
        missing = comps - set(self.availability)
        if missing:
            raise ValueError(f"no availability given for: {sorted(missing)}")
        for c in comps:
            a = self.availability[c]
            if not (0.0 <= a <= 1.0):
                raise ValueError(f"availability of {c} must be in [0,1], got {a}")

    def components(self) -> list:
        return sorted(self.structure.components())

    # -- probabilistic ----------------------------------------------------
    def _enumerate(self, forced: dict) -> float:
        comps = self.components()
        if len(comps) > _MAX_ENUM:
            raise ValueError(
                f"{len(comps)} components exceeds enumeration cap {_MAX_ENUM}")
        free = [c for c in comps if c not in forced]
        total = 0.0
        for bits in range(1 << len(free)):
            state = dict(forced)
            prob = 1.0
            for idx, c in enumerate(free):
                up = bool((bits >> idx) & 1)
                state[c] = up
                a = self.availability[c]
                prob *= a if up else (1.0 - a)
            if self.structure.evaluate(state):
                total += prob
        return total

    def system_availability(self) -> float:
        """Exact whole-system availability (handles shared components)."""
        return self._enumerate(forced={})

    def system_unavailability(self) -> float:
        return 1.0 - self.system_availability()

    def top_event_probability(self) -> float:
        """Fault-tree top-event probability = system unavailability."""
        return self.system_unavailability()

    # -- structural -------------------------------------------------------
    def _is_cut(self, fail_set: frozenset) -> bool:
        """True if failing exactly ``fail_set`` (others up) brings the system down."""
        comps = self.components()
        state = {c: (c not in fail_set) for c in comps}
        return not self.structure.evaluate(state)

    def minimal_cut_sets(self) -> list:
        """Minimal sets of components whose joint failure downs the system.

        A size-1 cut set is a single point of failure. Returned as a list of
        frozensets, ordered by increasing size.
        """
        comps = self.components()
        if len(comps) > _MAX_CUTSET:
            raise ValueError(
                f"{len(comps)} components exceeds cut-set cap {_MAX_CUTSET}")
        found: list = []
        for size in range(1, len(comps) + 1):
            for combo in combinations(comps, size):
                s = frozenset(combo)
                # skip supersets of an already-minimal cut set
                if any(m <= s for m in found):
                    continue
                if self._is_cut(s):
                    found.append(s)
        return found

    def single_points_of_failure(self) -> list:
        """Components that alone can bring the whole system down."""
        return sorted(next(iter(m)) for m in self.minimal_cut_sets()
                      if len(m) == 1)

    def birnbaum_importance(self, component: str) -> float:
        """Birnbaum importance: dA_system / dA_component.

        = A_system(component forced UP) - A_system(component forced DOWN).
        Higher means the system's availability is more sensitive to that
        component -- i.e. where hardening pays off most (feeds Layer 4).
        """
        if component not in self.structure.components():
            raise ValueError(f"unknown component: {component}")
        up = self._enumerate(forced={component: True})
        down = self._enumerate(forced={component: False})
        return up - down

    def importance_ranking(self) -> list:
        """Components sorted by Birnbaum importance, most critical first."""
        return sorted(((c, self.birnbaum_importance(c)) for c in self.components()),
                      key=lambda kv: kv[1], reverse=True)
