"""M/M/c queueing model -- Thesis Layer 1 (performance analysis).

Pure-stdlib implementation of the M/M/c (Erlang-C) multi-server queue. Each
service tier of a microservice system is modelled as one M/M/c queue where
``c`` is the number of running replicas of that service.

The formulas follow the standard references used in the thesis:
  * Trivedi, *Probability and Statistics with Reliability, Queuing, and
    Computer Science Applications* (2nd ed.), ch. on M/M/c queues.
  * Bolch et al., *Queueing Networks and Markov Chains*.

Symbols
-------
    lam (lambda) : mean arrival rate            [requests / time-unit]
    mu  (mu)     : mean service rate PER SERVER  = 1 / mean-service-time
    c            : number of servers (= service replicas), integer >= 1
    a  = lam/mu          : offered load ("Erlangs")
    rho = lam/(c*mu)     : per-server utilization; stable iff rho < 1

Everything is dimensionless in the time-unit of ``lam`` and ``mu`` (they must
share the same unit, e.g. both per-second).

Modelling assumptions (M/M/c)
-----------------------------
The "M/M/c" name encodes the model's assumptions; they are exact for the model
but only approximate for a real service, so keep them in mind when comparing
against measurements:
  * Poisson (memoryless) arrivals and exponential (memoryless) service times.
    Real microservice traffic is often bursty and service times heavy-tailed;
    departures from these bias Wq/Lq (see Kingman / Allen-Cunneen corrections).
  * A single shared FIFO queue feeding ``c`` identical replicas, each serving
    one request at a time. If replicas have their own queues or serve requests
    concurrently (thread pools), ``c`` should be interpreted accordingly.
  * An unbounded queue: at overload (rho >= 1) the model reports infinite
    waiting. A real service with a bounded queue / timeouts sheds load instead
    (that is an M/M/c/K model); here overload is flagged via ``stable=False``.

This module has NO third-party dependencies so it runs on a bare Python
install::

    python3 -m analyzer.tests.test_queueing
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import inf

__all__ = ["QueueMetrics", "mmc_metrics", "erlang_c"]

# A system whose utilization is within this tolerance of 1.0 is treated as
# unstable. Without it, floating-point rounding can make a mathematically
# critical system (rho == 1) evaluate to 0.999...9 and be reported "stable"
# with astronomically large but finite (nonsense) queue metrics.
_STABILITY_EPS = 1e-12


@dataclass(frozen=True)
class QueueMetrics:
    """Steady-state metrics of a single M/M/c queue.

    All rate quantities are in the caller's time-unit; times are its reciprocal.
    """

    lam: float          # arrival rate  lambda
    mu: float           # per-server service rate  mu
    c: int              # number of servers / replicas
    a: float            # offered load  lam/mu
    rho: float          # utilization   lam/(c*mu)
    stable: bool        # True iff rho < 1 (beyond _STABILITY_EPS)
    p0: float           # P(0 jobs in the system)
    p_wait: float       # Erlang-C: P(an arriving job has to queue)
    Lq: float           # mean number of jobs waiting in queue
    L: float            # mean number of jobs in the system (queue + service)
    Wq: float           # mean waiting time in queue
    W: float            # mean response time (Wq + service time)
    throughput: float   # completed jobs / time-unit

    def as_dict(self) -> dict:
        return asdict(self)


def mmc_metrics(lam: float, mu: float, c: int) -> QueueMetrics:
    """Compute steady-state M/M/c metrics.

    Parameters
    ----------
    lam : mean arrival rate (>= 0).
    mu  : mean service rate per server (> 0).
    c   : number of servers / replicas (integer >= 1).

    Returns
    -------
    QueueMetrics
        If the queue is unstable (rho >= 1) the waiting metrics are ``inf`` and
        ``stable`` is ``False``; the servers saturate so throughput = c*mu.

    Notes
    -----
    The Erlang-C waiting probability is computed via the Erlang-B recurrence
    ``B(n) = a*B(n-1) / (n + a*B(n-1))``, which keeps every intermediate value
    in ``[0, 1]`` and therefore never overflows -- even for a huge offered load
    where the literal ``a**c / c!`` would exceed the floating-point range.
    """
    # --- input validation -------------------------------------------------
    if lam < 0:
        raise ValueError(f"arrival rate lam must be >= 0, got {lam}")
    if mu <= 0:
        raise ValueError(f"service rate mu must be > 0, got {mu}")
    if c != int(c) or c < 1:
        raise ValueError(f"c (servers/replicas) must be an integer >= 1, got {c}")
    c = int(c)

    a = lam / mu
    rho = a / c

    # --- degenerate: no traffic ------------------------------------------
    if lam == 0.0:
        return QueueMetrics(
            lam=lam, mu=mu, c=c, a=a, rho=0.0, stable=True,
            p0=1.0, p_wait=0.0, Lq=0.0, L=0.0, Wq=0.0, W=1.0 / mu, throughput=0.0,
        )

    # --- unstable: offered load meets/exceeds capacity -------------------
    # Use a tolerance so a mathematically critical system (rho == 1) is not
    # misclassified as stable due to floating-point rounding.
    if rho >= 1.0 - _STABILITY_EPS:
        return QueueMetrics(
            lam=lam, mu=mu, c=c, a=a, rho=rho, stable=False,
            p0=0.0, p_wait=1.0, Lq=inf, L=inf, Wq=inf, W=inf,
            throughput=c * mu,   # servers are always busy -> saturated output
        )

    # --- stable M/M/c -----------------------------------------------------
    # Erlang-B recurrence (overflow-safe): B_0 = 1, B_n = a*B_{n-1}/(n + a*B_{n-1}).
    erlang_b = 1.0
    for n in range(1, c + 1):
        erlang_b = a * erlang_b / (n + a * erlang_b)

    # Erlang-C (probability an arrival must wait) expressed via Erlang-B.
    p_wait = c * erlang_b / (c - a * (1.0 - erlang_b))

    # P(0 in system): normalized factorial terms. For an extreme offered load
    # the terms overflow to +inf and 1/inf -> 0.0, which is the correct limit
    # (p0 -> 0) and, crucially, involves no inf*0 -> NaN because p_wait above is
    # computed independently.
    terms = [1.0]
    for n in range(1, c + 1):
        terms.append(terms[-1] * a / n)
    denom = sum(terms[:c]) + terms[c] / (1.0 - rho)
    p0 = 1.0 / denom

    Lq = p_wait * rho / (1.0 - rho)
    L = Lq + a                         # Little's law: L = Lq + lam/mu
    Wq = Lq / lam
    W = Wq + 1.0 / mu
    throughput = lam                   # stable => output rate equals input rate

    return QueueMetrics(
        lam=lam, mu=mu, c=c, a=a, rho=rho, stable=True,
        p0=p0, p_wait=p_wait, Lq=Lq, L=L, Wq=Wq, W=W, throughput=throughput,
    )


def erlang_c(a: float, c: int) -> float:
    """Erlang-C probability of waiting for offered load ``a`` and ``c`` servers.

    Convenience wrapper: the waiting probability depends only on ``a`` and
    ``c`` (not on ``lam`` and ``mu`` separately), so we evaluate M/M/c with
    ``mu = 1`` and ``lam = a``.

    For ``a >= c`` (rho >= 1) the queue is unstable and this returns the
    limiting value ``1.0`` (an arrival is certain to wait).
    """
    return mmc_metrics(lam=a, mu=1.0, c=c).p_wait
