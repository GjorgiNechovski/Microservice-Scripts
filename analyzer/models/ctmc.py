"""Continuous-Time Markov Chain reliability model -- Thesis Layer 2.

Pure-stdlib CTMC engine for availability / MTTF / MTTR analysis. Each component
(or a replicated tier) is modelled as a CTMC whose states describe operating
condition (e.g. working / failed / recovering). From the steady-state solution
we read the **availability**; from absorbing-state (mean-time-to-absorption)
analysis we read **MTTF** (mean time to failure) and **MTTR** (mean time to
repair / recovery).

References (thesis):
  * Sahner, Trivedi & Puliafito, *Performance and Reliability Analysis of
    Computer Systems* (SHARPE), ch. on Markov reliability models.
  * Trivedi, *Probability and Statistics with Reliability, Queuing, and
    Computer Science Applications*.

Symbols
-------
    failure_rate (lambda_f) : rate a working unit fails      [1 / time-unit]
    repair_rate  (mu_r)     : rate a failed unit is repaired  [1 / time-unit]
    A  = availability  = fraction of time the system is "up"  = MTTF/(MTTF+MTTR)
    MTTF = mean up-time before entering a down state
    MTTR = mean down-time before returning to an up state

Everything is dimensionless in the caller's time-unit (be consistent, e.g. all
rates per hour). No third-party dependencies -- the small linear systems are
solved with a bundled Gauss-Jordan routine, so this runs on a bare Python
install::

    python3 -m analyzer.tests.test_ctmc
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

__all__ = [
    "CTMC",
    "ReliabilityResult",
    "component_reliability",
    "three_state_reliability",
    "replicated_reliability",
]

_MINUTES_PER_YEAR = 365.0 * 24.0 * 60.0  # for "downtime per year" convenience


class _SingularMatrix(ValueError):
    """Raised when a linear system has no unique solution."""


def _solve(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    """Solve ``matrix @ x = rhs`` via Gauss-Jordan with partial pivoting.

    Pure Python; intended for the small dense systems that arise from CTMCs
    with a handful of states. Raises ``_SingularMatrix`` if no unique solution.
    """
    n = len(matrix)
    # Work on an augmented copy so the caller's data is untouched.
    aug = [list(matrix[i]) + [rhs[i]] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-15:
            raise _SingularMatrix("matrix is singular or nearly so")
        aug[col], aug[pivot] = aug[pivot], aug[col]
        pdiv = aug[col][col]
        for r in range(n):
            if r == col:
                continue
            factor = aug[r][col] / pdiv
            if factor != 0.0:
                for k in range(col, n + 1):
                    aug[r][k] -= factor * aug[col][k]
    return [aug[i][n] / aug[i][i] for i in range(n)]


@dataclass(frozen=True)
class ReliabilityResult:
    """Availability / MTTF / MTTR of one component or replicated tier.

    Definitions (read these before interpreting the numbers):
      * ``availability`` -- fraction of time the system is up, computed EXACTLY
        from the steady-state distribution. This is the authoritative number.
      * ``mttf`` -- mean time to the FIRST failure, starting from the fully
        operational state (all replicas working).
      * ``mttr`` -- mean time to restore service, starting from the state the
        system enters when it first goes down.

    Note on the relation ``A = MTTF / (MTTF + MTTR)``: this holds EXACTLY for a
    single component (one "up" state), because the system always re-enters "up"
    in the same state it started. For a replicated tier it is only an
    APPROXIMATION -- after a repair the tier re-enters the up region with some
    replicas still failed, so the mean up-time per cycle is a little shorter
    than the cold-start ``mttf``. Trust ``availability`` (exact); treat the
    product relation as a close sanity check, not an identity, when n > 1.
    """

    availability: float          # fraction of time "up" (EXACT, from steady state)
    unavailability: float        # 1 - availability
    mttf: float                  # mean time to first failure (from all-up)
    mttr: float                  # mean time to repair (from the just-failed state)
    downtime_per_year: float     # (1-A) * minutes-per-year, in the SAME time-unit
    pi: dict                     # steady-state distribution over states

    def nines(self) -> float:
        """Number of 'nines' of availability, e.g. 0.999 -> ~3.0."""
        if self.unavailability <= 0.0:
            return math.inf
        return -math.log10(self.unavailability)

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k != "pi"}


class CTMC:
    """A continuous-time Markov chain over a finite set of labelled states.

    Parameters
    ----------
    states : iterable of unique hashable labels (strings or ints).
    transitions : iterable of ``(src, dst, rate)`` with ``rate > 0`` and
        ``src != dst``. Parallel edges are summed.
    """

    def __init__(self, states, transitions):
        self.states = list(states)
        if len(set(self.states)) != len(self.states):
            raise ValueError("state labels must be unique")
        self.index = {s: i for i, s in enumerate(self.states)}
        n = len(self.states)
        if n == 0:
            raise ValueError("CTMC needs at least one state")
        Q = [[0.0] * n for _ in range(n)]
        for src, dst, rate in transitions:
            if src not in self.index or dst not in self.index:
                raise ValueError(f"transition references unknown state: {src}->{dst}")
            if src == dst:
                raise ValueError(f"self-transition not allowed: {src}->{dst}")
            if rate < 0:
                raise ValueError(f"transition rate must be >= 0, got {rate}")
            Q[self.index[src]][self.index[dst]] += rate
        for i in range(n):
            Q[i][i] = -sum(Q[i][j] for j in range(n) if j != i)
        self.Q = Q

    # -- steady state ------------------------------------------------------
    def steady_state(self) -> dict:
        """Return the stationary distribution ``pi`` (solves ``pi Q = 0``)."""
        n = len(self.states)
        if n == 1:
            return {self.states[0]: 1.0}
        # pi Q = 0  <=>  Q^T pi = 0. Replace the last (redundant) equation with
        # the normalization sum(pi) = 1.
        a = [[self.Q[j][i] for j in range(n)] for i in range(n)]  # transpose
        a[n - 1] = [1.0] * n
        b = [0.0] * (n - 1) + [1.0]
        sol = _solve(a, b)
        # Clean negative round-off and renormalize.
        sol = [p if p > 0.0 else 0.0 for p in sol]
        total = sum(sol)
        return {self.states[i]: sol[i] / total for i in range(n)}

    def availability(self, up_states) -> float:
        up = set(up_states)
        pi = self.steady_state()
        return sum(p for s, p in pi.items() if s in up)

    # -- mean time to absorption ------------------------------------------
    def mean_time_to_absorption(self, transient, start) -> float:
        """Mean time to leave the ``transient`` set, starting from ``start``.

        States outside ``transient`` are treated as absorbing. Returns ``inf``
        if the transient set cannot be left (no path to absorption).
        """
        T = list(transient)
        tset = set(T)
        if start not in tset:
            raise ValueError("start must be one of the transient states")
        m = len(T)
        sub = [[self.Q[self.index[T[i]]][self.index[T[j]]] for j in range(m)]
               for i in range(m)]
        rhs = [-1.0] * m
        try:
            times = _solve(sub, rhs)
        except _SingularMatrix:
            return math.inf   # transient set is closed -> never absorbed
        return times[T.index(start)]

    def mttf(self, up_states, start) -> float:
        """Mean time to failure: mean sojourn in the up set from ``start``."""
        return self.mean_time_to_absorption(up_states, start)

    def mttr(self, up_states, start_down) -> float:
        """Mean time to repair: mean sojourn in the down set from ``start_down``."""
        up = set(up_states)
        down = [s for s in self.states if s not in up]
        if not down:
            return math.inf
        return self.mean_time_to_absorption(down, start_down)


# --------------------------------------------------------------------------
# Convenience builders that return a ready ReliabilityResult
# --------------------------------------------------------------------------

def _evaluate(ctmc: CTMC, up_states, start_up, start_down) -> ReliabilityResult:
    A = ctmc.availability(up_states)
    mttf = ctmc.mttf(up_states, start_up)
    down = [s for s in ctmc.states if s not in set(up_states)]
    mttr = ctmc.mttr(up_states, start_down) if down else math.inf
    return ReliabilityResult(
        availability=A,
        unavailability=1.0 - A,
        mttf=mttf,
        mttr=mttr,
        downtime_per_year=(1.0 - A) * _MINUTES_PER_YEAR,
        pi=ctmc.steady_state(),
    )


def component_reliability(failure_rate: float, repair_rate: float) -> ReliabilityResult:
    """Two-state (UP/DOWN) repairable component.

    A = mu/(lambda+mu), MTTF = 1/lambda, MTTR = 1/mu.
    """
    if failure_rate < 0 or repair_rate < 0:
        raise ValueError("rates must be >= 0")
    ctmc = CTMC(["UP", "DOWN"], [("UP", "DOWN", failure_rate),
                                 ("DOWN", "UP", repair_rate)])
    return _evaluate(ctmc, up_states=["UP"], start_up="UP", start_down="DOWN")


def three_state_reliability(failure_rate: float, detect_rate: float,
                            repair_rate: float) -> ReliabilityResult:
    """Three-state component: WORKING -> FAILED -> RECOVERING -> WORKING.

    Separates fault *detection* latency (1/detect_rate) from *repair* time
    (1/repair_rate), matching the docx's working/failed/recovering states.
    Both FAILED and RECOVERING are "down"; MTTR = 1/detect_rate + 1/repair_rate.
    """
    if min(failure_rate, detect_rate, repair_rate) < 0:
        raise ValueError("rates must be >= 0")
    ctmc = CTMC(
        ["WORKING", "FAILED", "RECOVERING"],
        [("WORKING", "FAILED", failure_rate),
         ("FAILED", "RECOVERING", detect_rate),
         ("RECOVERING", "WORKING", repair_rate)],
    )
    return _evaluate(ctmc, up_states=["WORKING"], start_up="WORKING",
                     start_down="FAILED")


def replicated_reliability(n: int, failure_rate: float, repair_rate: float,
                           k: int = 1, repair_mode: str = "independent"
                           ) -> ReliabilityResult:
    """Repairable ``k``-out-of-``n`` tier (system up iff >= k replicas work).

    Modelled as a birth-death CTMC over the *number of failed replicas*
    ``0..n``. Each working replica fails independently at ``failure_rate``.

    repair_mode:
      * ``"independent"`` -- every failed replica is repaired in parallel
        (repair rate ``i*repair_rate`` when ``i`` are down). Matches Docker
        restarting each crashed container independently.
      * ``"shared"`` -- a single repair facility (rate ``repair_rate``).
    """
    if n < 1:
        raise ValueError("n must be >= 1")
    if not (1 <= k <= n):
        raise ValueError("k must satisfy 1 <= k <= n")
    if failure_rate < 0 or repair_rate < 0:
        raise ValueError("rates must be >= 0")
    if repair_mode not in ("independent", "shared"):
        raise ValueError("repair_mode must be 'independent' or 'shared'")

    states = list(range(n + 1))          # i = number of failed replicas
    transitions = []
    for i in range(n):                   # a failure: i -> i+1
        working = n - i
        transitions.append((i, i + 1, working * failure_rate))
    for i in range(1, n + 1):            # a repair: i -> i-1
        rate = i * repair_rate if repair_mode == "independent" else repair_rate
        transitions.append((i, i - 1, rate))

    ctmc = CTMC(states, transitions)
    up_states = [i for i in states if (n - i) >= k]  # working >= k
    start_down = n - k + 1               # first state where working < k
    return _evaluate(ctmc, up_states=up_states, start_up=0, start_down=start_down)
