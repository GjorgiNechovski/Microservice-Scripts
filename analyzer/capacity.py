"""Capacity sizing -- how many replicas each service needs (M/M/c math).

Given a service's measured arrival rate (lambda) and per-replica service rate
(mu), the smallest replica count ``c`` that keeps its M/M/c queue under a target
is a direct consequence of the Layer-1 model -- no redeploying, no trial and
error. Every M/M/c wait metric falls monotonically as ``c`` grows, so "the
smallest ``c`` that meets the target" is well defined and cheap to find.

Two targets, combinable:
  * utilization ceiling   rho = lambda/(c*mu) <= target_utilization  (default 0.70)
  * response-time budget  W(c) <= max_response                        (optional)

Because ``mu`` is measured PER REPLICA and is ~load-independent, sizing computed
at one load predicts the replicas needed at any load. Where ``mu`` sags under
load (contention), measuring per level (the load sweep) sizes with the ``mu``
seen at that level, so the recommendation self-corrects.

    python3 -m analyzer.capacity systems/finki-blogger.toml
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass

from analyzer.models.queueing import mmc_metrics
from analyzer.spec import load_or_default_measurements, load_system

_DEFAULT_TARGET_UTIL = 0.70


def min_replicas(lam: float, mu: float, *,
                 target_utilization: float = _DEFAULT_TARGET_UTIL,
                 max_response: float | None = None, cap: int = 4096) -> int:
    """Smallest replica count whose M/M/c queue meets the target(s).

    ``lam`` and ``mu`` share a time-unit; ``max_response`` is in that unit's
    reciprocal (e.g. seconds if the rates are per-second). The utilization
    ceiling has a closed form; a response-time budget is met by walking ``c``
    up from there (W is monotone decreasing in ``c``).
    """
    if mu <= 0:
        raise ValueError(f"service rate mu must be > 0, got {mu}")
    if not 0.0 < target_utilization < 1.0:
        raise ValueError(
            f"target_utilization must be in (0, 1), got {target_utilization}")
    if lam <= 0:
        return 1

    # Utilization ceiling: rho = lam/(c*mu) <= target  <=>  c >= lam/(mu*target).
    c = max(1, math.ceil(lam / (mu * target_utilization)))
    if max_response is None:
        return min(c, cap)

    # Tighten for the latency budget (only ever raises c, so rho stays satisfied).
    while c < cap:
        m = mmc_metrics(lam, mu, c)
        if m.stable and m.W <= max_response:
            return c
        c += 1
    return cap


@dataclass
class Sizing:
    """Recommended vs current replicas for one service at one load."""

    service: str
    lam: float                       # arrival rate, req/s
    mu: float                        # per-replica service rate, req/s
    current: int                     # replicas actually running
    needed: int                      # replicas the math recommends
    target_utilization: float
    max_response: float | None = None

    @property
    def rho_current(self) -> float:
        denom = self.current * self.mu
        return self.lam / denom if denom > 0 else math.inf

    @property
    def rho_sized(self) -> float:
        denom = self.needed * self.mu
        return self.lam / denom if denom > 0 else 0.0

    @property
    def delta(self) -> int:
        return self.needed - self.current

    @property
    def reliable(self) -> bool:
        """False when ``mu`` was captured while the service was overloaded.

        ``mu`` is estimated from mean latency; under saturation that latency is
        dominated by queue waiting, so the estimate collapses and ``needed`` is
        not trustworthy (it double-counts congestion). ``rho_current >= 1`` --
        the service was already overloaded at its current replica count during
        the measurement -- flags exactly that case, so the recommendation is
        suppressed rather than reported as a bogus number.
        """
        return self.mu > 0 and self.rho_current < 1.0

    @property
    def verdict(self) -> str:
        if self.delta > 0:
            return "under"          # too few replicas for this load
        if self.delta < 0:
            return "over"           # more replicas than the load needs
        return "ok"


def size_services(spec: dict, measurements: dict,
                  target_utilization: float = _DEFAULT_TARGET_UTIL,
                  max_response: float | None = None) -> dict:
    """Compute a :class:`Sizing` for every service in the spec."""
    ms = measurements["services"]
    out = {}
    for name in spec["services"]:
        p = ms[name]
        lam = float(p["arrival_rate"])
        mu = float(p.get("service_rate_effective", p["service_rate"]))
        current = int(p["replicas"])
        needed = min_replicas(lam, mu, target_utilization=target_utilization,
                              max_response=max_response)
        out[name] = Sizing(name, lam, mu, current, needed,
                           target_utilization, max_response)
    return out


def capacity_target(spec: dict, args=None) -> tuple:
    """Resolve ``(target_utilization, max_response_seconds)`` from spec + CLI.

    Reads the optional ``[capacity]`` table; ``args`` (if given) overrides it.
    ``max_response_ms`` is converted to seconds to match the per-second rates.
    """
    cap = spec.get("capacity", {})
    target_util = float(cap.get("target_utilization", _DEFAULT_TARGET_UTIL))
    ms = cap.get("max_response_ms")
    max_response = float(ms) / 1000.0 if ms else None
    if args is not None:
        if getattr(args, "target_utilization", None) is not None:
            target_util = float(args.target_utilization)
        if getattr(args, "max_response_ms", None) is not None:
            max_response = float(args.max_response_ms) / 1000.0
    return target_util, max_response


def capacity_section(spec: dict, measurements: dict,
                     target_utilization: float = _DEFAULT_TARGET_UTIL,
                     max_response: float | None = None) -> str:
    """A report block: current vs needed replicas at the measured load."""
    sizings = size_services(spec, measurements, target_utilization, max_response)
    tgt = f"utilization <= {target_utilization:.2f}"
    if max_response is not None:
        tgt += f", response <= {max_response * 1000:.0f} ms"

    lines = [
        "=" * 74,
        "Capacity sizing (M/M/c) -- replicas needed at the measured load",
        f"target: {tgt}",
        "=" * 74,
        f"{'service':<18}{'λ req/s':>10}{'μ/rep':>9}{'now':>5}{'need':>6}"
        f"{'ρ now':>8}  verdict",
    ]
    for name in spec["services"]:
        s = sizings[name]
        rho = "inf" if s.rho_current == math.inf else f"{s.rho_current:.2f}"
        if not s.reliable:
            need = "n/a"
            v = "μ unreliable — overloaded during measurement (ρ≥1)"
        elif s.verdict == "under":
            need = str(s.needed)
            v = f"UNDER by {s.delta} — add replicas"
        elif s.verdict == "over":
            need = str(s.needed)
            v = f"over by {-s.delta} — could reduce"
        else:
            need = str(s.needed)
            v = "right-sized"
        lines.append(f"{name:<18}{s.lam:>10.2f}{s.mu:>9.2f}{s.current:>5}"
                     f"{need:>6}{rho:>8}  {v}")

    reliable = [n for n in spec["services"] if sizings[n].reliable]
    unreliable = [n for n in spec["services"] if not sizings[n].reliable]
    under = [n for n in reliable if sizings[n].verdict == "under"]
    over = [n for n in reliable if sizings[n].verdict == "over"]
    lines.append("")
    if under:
        lines.append(f"  under-provisioned at this load: {', '.join(under)}")
    if over:
        lines.append(f"  over-provisioned (headroom to spare): {', '.join(over)}")
    if unreliable:
        lines.append(f"  could not size — overloaded during measurement, so μ "
                     f"(and the replica count) is unreliable: {', '.join(unreliable)}")
        lines.append("    a service saturated even here has a per-request latency "
                     "problem; more replicas")
        lines.append("    won't help until that is fixed, or measure it at a "
                     "lower load.")
    if not under and not over and not unreliable:
        lines.append("  every service is right-sized for this load.")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 1:
        print("usage: python3 -m analyzer.capacity <system-spec.toml>",
              file=sys.stderr)
        return 2
    spec = load_system(argv[0])
    measurements, _ = load_or_default_measurements(argv[0], spec)
    target_util, max_response = capacity_target(spec)
    print(capacity_section(spec, measurements, target_util, max_response))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
