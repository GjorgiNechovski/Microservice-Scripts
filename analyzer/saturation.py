"""Recover a clean service rate from a saturated sweep, and report supported load.

The load-sweep estimates mu as ``1/mean-latency``, which COLLAPSES once a service
starts queueing (the case ``capacity.Sizing.reliable`` flags and suppresses). But
the same saturated run still carries a clean signal: under saturation every
replica is always busy, so the *completed-request throughput* plateaus at the
true capacity ::

    X_peak = c * mu_true          (departures are NOT inflated by queue waiting)

so we recover the clean per-replica service rate ``mu_true = X_peak / c`` and,
through the M/M/c model we already trust, the load the current replica count can
actually sustain ::

    ceiling      = c * mu_true              (req/s at rho = 1; never run here)
    sustainable  = target * c * mu_true     (req/s at the utilization target)

The virtual-user count is read straight off the sweep's own (VUs -> throughput)
samples: at a healthy (un-saturated) level each VU contributes ``lam/VUs`` req/s,
so ``supported_VUs = sustainable / (lam/VUs)``.

For a service that never saturated, its latency-based mu is already valid (it was
not queueing), so that value is used directly. This is the throughput-plateau
method (approach #3). A CPU utilization-law cross-check (``mu = throughput / cpu``)
is the natural validation to add once per-replica CPU is persisted in the
measured file.

Crucially this does NOT contradict the reliability guardrail: that rejected mu
taken from *latency* under load; here mu comes from *throughput* under the very
same load -- valid precisely because saturation contaminates latency but reveals
throughput. So the poisoned run is exactly the data this needs, and the ``n/a``
sizing gets a real, throughput-based number back.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from analyzer.capacity import min_replicas


@dataclass
class LoadCapacity:
    service: str
    replicas: int
    mu_clean: float               # clean per-replica service rate (req/s)
    method: str                   # how mu_clean was obtained
    peak_throughput: float        # highest arrival rate observed (req/s)
    ceiling_throughput: float     # c * mu_clean  (req/s at rho = 1)
    sustainable_throughput: float # target * c * mu_clean (req/s at the target)
    per_vu_throughput: float | None  # req/s each VU drives, at a healthy level
    supported_vus: int | None     # sustainable / per_vu_throughput
    replicas_for_peak: int | None # replicas to run the observed peak at target
    saturated: bool
    notes: list = field(default_factory=list)


def _rows(levels, name):
    rows = []
    for lv in sorted(levels, key=lambda x: x.vus):
        p = lv.measurements["services"][name]
        q = lv.results["services"][name].queue
        rows.append((lv.vus, float(p["arrival_rate"]),
                     float(p["service_rate"]), q.rho, q.stable))
    return rows


def estimate_capacity(levels, spec, target_utilization) -> dict:
    """Per-service :class:`LoadCapacity` recovered from the load sweep."""
    out = {}
    for name in spec["services"]:
        rows = _rows(levels, name)
        c = int(levels[0].measurements["services"][name]["replicas"])
        lam = [r[1] for r in rows]
        peak = max(lam) if lam else 0.0
        saturated = any((not stable) or rho >= 1.0 for _, _, _, rho, stable in rows)
        notes = []

        if saturated:
            mu_clean = peak / c if c > 0 else 0.0
            method = "throughput-plateau"
        else:
            mu_clean = max((r[2] for r in rows), default=0.0)   # least-queued mu
            method = "latency (unsaturated)"
            notes.append("never saturated in the tested range — the ceiling is a "
                         "model extrapolation, not an observed plateau")
        if peak == 0.0:
            notes.append("no traffic reached this service in the sweep")

        ceiling = c * mu_clean
        sustainable = target_utilization * c * mu_clean

        # per-VU throughput from the healthiest sample (lowest rho, traffic present)
        healthy = None
        for vus, l, _mu, rho, _s in rows:
            if l > 0 and rho < target_utilization and (
                    healthy is None or rho < healthy[3]):
                healthy = (vus, l, _mu, rho, _s)
        per_vu = (healthy[1] / healthy[0]) if (healthy and healthy[0] > 0) else None
        supported_vus = int(sustainable / per_vu) if (per_vu and per_vu > 0) else None
        if per_vu is None and saturated:
            notes.append("no un-saturated sample — cannot convert to a user count; "
                         "re-run with lower levels to pin the user threshold")

        replicas_for_peak = (min_replicas(peak, mu_clean,
                                          target_utilization=target_utilization)
                             if mu_clean > 0 and peak > 0 else None)

        out[name] = LoadCapacity(name, c, mu_clean, method, peak, ceiling,
                                 sustainable, per_vu, supported_vus,
                                 replicas_for_peak, saturated, notes)
    return out


def system_supported(caps: dict):
    """(vus, limiting_service) the whole system supports, or (None, bottleneck).

    The system is limited by whichever service runs out of headroom first. If
    that service could be converted to a VU count, return it; otherwise return
    ``None`` with the service that blocks the estimate (typically one saturated
    below the lowest tested level).
    """
    known = {n: c.supported_vus for n, c in caps.items()
             if c.supported_vus is not None and c.peak_throughput > 0}
    blocking = [n for n, c in caps.items()
                if c.saturated and c.supported_vus is None]
    if blocking:
        return None, blocking[0]
    if not known:
        return None, None
    who = min(known, key=known.get)
    return known[who], who


def capacity_ceiling_report(levels, spec, target_utilization) -> str:
    """Technical 'supported load' section for the scaling report."""
    caps = estimate_capacity(levels, spec, target_utilization)
    lines = [
        "", "=" * 74,
        "Supported load — capacity ceiling (throughput-plateau method)",
        f"target: run at ρ ≤ {target_utilization:.2f}",
        "=" * 74,
        f"{'service':<18}{'now':>4}{'μ/rep*':>9}{'ceiling':>10}"
        f"{'safe max':>10}{'max VUs':>9}",
    ]
    # a per-service VU figure extrapolated far past the tested range (a barely
    # loaded service) is not a real capacity statement -> cap the display.
    cap_display = 10 * max(lv.vus for lv in levels)
    for name in spec["services"]:
        cp = caps[name]
        if cp.supported_vus is None:
            vus = "n/a"
        elif cp.supported_vus > cap_display:
            vus = f">{cap_display}"
        else:
            vus = str(cp.supported_vus)
        lines.append(f"{name:<18}{cp.replicas:>4}{cp.mu_clean:>9.2f}"
                     f"{cp.ceiling_throughput:>10.1f}"
                     f"{cp.sustainable_throughput:>10.1f}{vus:>9}")
    lines += [
        "  * μ/rep for a saturated service is recovered from its throughput",
        "    plateau (clean); for the rest it is the measured value (also clean).",
        "  ceiling = req/s at 100% (ρ=1); safe max = req/s at the target; both are",
        "  per second across all current replicas.",
    ]

    sys_vus, who = system_supported(caps)
    lines.append("")
    if sys_vus is not None:
        lines.append(f"  System supports about {sys_vus} concurrent users "
                     f"(limited by {who}).")
    elif who is not None:
        lines.append(f"  System limit is set by {who}, which was overloaded even at "
                     "the lowest tested")
        lines.append("  load — re-run with lower levels to pin the exact user "
                     "threshold.")

    sat = [n for n in spec["services"] if caps[n].saturated
           and caps[n].peak_throughput > 0]
    if sat:
        lines.append("")
        lines.append("  Un-poisoned sizing (throughput-based) for the overloaded "
                     "service(s):")
        for n in sat:
            cp = caps[n]
            lines.append(
                f"    {n}: maxed out at {cp.replicas} replicas (~"
                f"{cp.peak_throughput:.0f} req/s). To run that load at the target "
                f"use {cp.replicas_for_peak} replicas;")
            lines.append(
                f"      each added replica lifts the ceiling by ~{cp.mu_clean:.1f} "
                "req/s.")
    return "\n".join(lines) + "\n"


def capacity_ceiling_explain(levels, spec, target_utilization) -> str:
    """Plain-language 'how much traffic it can take' section (no jargon)."""
    caps = estimate_capacity(levels, spec, target_utilization)
    out = ["HOW MUCH TRAFFIC IT CAN TAKE", "-" * 70]

    sys_vus, who = system_supported(caps)
    if sys_vus is not None:
        out.append(f"  As a whole, the setup comfortably serves about {sys_vus} "
                   f"users at once")
        out.append(f"  (the first to run out of room is {who}).")
    elif who is not None:
        out.append(f"  We can't put a user number on the whole system yet: {who} "
                   "was")
        out.append("  overwhelmed even at the lightest test, so it caps everything.")
        out.append("  Re-run with fewer users (say 5, 10, 20) to find its limit.")
    out.append("")

    sat = [n for n in spec["services"] if caps[n].saturated
           and caps[n].peak_throughput > 0]
    for n in sat:
        cp = caps[n]
        copies = f"{cp.replicas} cop{'y' if cp.replicas == 1 else 'ies'}"
        out.append(f"  {n} is maxed out: its {copies} together top out at about")
        out.append(f"  {cp.peak_throughput:.0f} requests a second. To run that with "
                   f"breathing room you'd")
        out.append(f"  want {cp.replicas_for_peak} copies, and every extra copy adds "
                   f"roughly {cp.mu_clean:.0f} more")
        out.append("  requests a second of headroom.")
        out.append("")
    return "\n".join(out)
