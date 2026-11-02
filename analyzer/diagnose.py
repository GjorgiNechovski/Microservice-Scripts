"""Scale-out vs optimize -- which fix does a bottleneck actually need?

Two very different problems both present as "the service is slow": a service
doing heavy WORK per request, and a service doing light work but swamped by
VOLUME. They need opposite fixes -- optimize the request vs add replicas -- and
the queueing math already tells them apart.

The discriminator is the per-request service time ::

    S = 1 / mu_true      (ms of work to serve one request)

recovered from the clean per-replica service rate (saturation.py). If an
overloaded service has a HEAVY request (large S), adding a replica only buys
``mu_true`` req/s -- a poor return -- so the real lever is making the request
cheaper (pagination / caching / an index). If its request is CHEAP (small S),
capacity ``c * mu`` scales well with ``c``, so more replicas is the right,
efficient fix.

"Heavy" is judged generically: a request is heavy when its service time is an
outlier versus the app's own services (``heavy_factor`` x the median) and above
a small floor (``heavy_floor_ms``), both tunable via the spec's ``[diagnose]``
table. Nothing here is specific to one application.

The tool routes you to the KIND of fix; the specific cause (a big payload, a
missing index, blocking I/O) still comes from the code or the traces.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from analyzer.saturation import estimate_capacity


@dataclass
class Diagnosis:
    service: str
    mu_clean: float               # clean per-replica service rate (req/s)
    service_time_ms: float        # S = 1000 / mu_clean
    peak_utilization: float       # highest utilization across the sweep
    overloaded: bool
    heavy_request: bool
    verdict: str                  # "ok" | "scale" | "optimize"
    replicas_needed: int | None   # for a scale verdict
    gain_per_replica: float       # req/s each added replica buys (= mu_clean)


@dataclass
class DiagnoseResult:
    diagnoses: dict               # name -> Diagnosis
    threshold_ms: float           # the "heavy request" line
    median_ms: float
    target: float


def diagnose(levels, spec, target) -> DiagnoseResult:
    """Classify every service as ok / scale / optimize from a load sweep."""
    cfg = spec.get("diagnose", {})
    factor = float(cfg.get("heavy_factor", 2.0))
    floor_ms = float(cfg.get("heavy_floor_ms", 40.0))

    caps = estimate_capacity(levels, spec, target)
    peak_rho = {n: max(lv.results["services"][n].queue.rho for lv in levels)
                for n in spec["services"]}
    service_time = {}
    for n in spec["services"]:
        mu = caps[n].mu_clean
        service_time[n] = (1000.0 / mu) if mu > 0 else math.inf

    finite = [v for v in service_time.values() if v != math.inf]
    median_ms = statistics.median(finite) if finite else 0.0
    threshold = max(floor_ms, factor * median_ms)

    diags = {}
    for n in spec["services"]:
        cp = caps[n]
        s = service_time[n]
        overloaded = cp.saturated or peak_rho[n] >= target
        heavy = s >= threshold
        if not overloaded:
            verdict = "ok"
        elif heavy:
            verdict = "optimize"
        else:
            verdict = "scale"
        diags[n] = Diagnosis(n, cp.mu_clean, s, peak_rho[n], overloaded, heavy,
                             verdict, cp.replicas_for_peak, cp.mu_clean)
    return DiagnoseResult(diags, threshold, median_ms, target)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def diagnosis_report(dr: DiagnoseResult, spec) -> str:
    d = dr.diagnoses
    lines = [
        "", "=" * 74,
        "Bottleneck diagnosis — scale out, or optimize the code?",
        "=" * 74,
        "Per-request service time S = 1/μ separates the two fixes: an overloaded",
        "service with a HEAVY request needs optimizing; one with a CHEAP request",
        f"just needs more replicas. (heavy = S ≥ {dr.threshold_ms:.0f} ms here)",
        "",
        f"{'service':<18}{'S (ms/req)':>12}{'peak load':>12}   fix",
    ]
    for n in spec["services"]:
        x = d[n]
        load = "overloaded" if x.overloaded else f"{min(x.peak_utilization, 9.99) * 100:.0f}%"
        fix = {"optimize": "OPTIMIZE — heavy request",
               "scale": "ADD REPLICAS — cheap request",
               "ok": "ok"}[x.verdict]
        s = "∞" if x.service_time_ms == math.inf else f"{x.service_time_ms:.0f}"
        lines.append(f"{n:<18}{s:>12}{load:>12}   {fix}")

    opt = [n for n in spec["services"] if d[n].verdict == "optimize"]
    scale = [n for n in spec["services"] if d[n].verdict == "scale"]
    lines.append("")
    for n in opt:
        x = d[n]
        lines.append(f"  {n}: overloaded AND heavy (~{x.service_time_ms:.0f} "
                     "ms/request). Optimize the request")
        lines.append("    (pagination / caching / fewer rows / an index). Adding a "
                     "replica buys only")
        lines.append(f"    ~{x.gain_per_replica:.0f} req/s, so scaling is the wrong "
                     "lever here.")
    for n in scale:
        x = d[n]
        need = f"~{x.replicas_needed} replicas" if x.replicas_needed else "more replicas"
        lines.append(f"  {n}: overloaded but cheap (~{x.service_time_ms:.0f} "
                     "ms/request). Add replicas —")
        lines.append(f"    scaling is efficient (~{x.gain_per_replica:.0f} req/s "
                     f"each); sizing suggests {need}.")
    if not opt and not scale:
        lines.append("  No overloaded services — nothing to scale or optimize "
                     "right now.")
    return "\n".join(lines) + "\n"


def diagnosis_explain(dr: DiagnoseResult, spec) -> str:
    d = dr.diagnoses
    out = [
        "SHOULD YOU ADD SERVERS, OR FIX THE CODE?", "-" * 70,
        "Two different problems both look like \"it's slow\", but need opposite",
        "fixes:",
        "  - heavy work per request  ->  make the work lighter (change the code)",
        "  - light work but swamped   ->  give it more copies (add servers)",
        "",
    ]
    opt = [n for n in spec["services"] if d[n].verdict == "optimize"]
    scale = [n for n in spec["services"] if d[n].verdict == "scale"]
    for n in opt:
        x = d[n]
        out.append(f"  • {n}: each request is heavy (~{x.service_time_ms:.0f} ms "
                   "of work).")
        out.append(f"    More copies barely help (~{x.gain_per_replica:.0f} more "
                   "requests/second each).")
        out.append("    Make the request lighter first — fewer rows (pagination), "
                   "caching, or an index.")
        out.append("")
    for n in scale:
        x = d[n]
        need = f"about {x.replicas_needed}" if x.replicas_needed else "a few more"
        out.append(f"  • {n}: each request is quick (~{x.service_time_ms:.0f} ms) "
                   "but it's swamped.")
        out.append(f"    Just add copies ({need}) and it'll keep up.")
        out.append("")
    if not opt and not scale:
        out.append("  Nothing is overloaded — no servers to add, no code to fix "
                   "right now.")
    else:
        out.append("  Everything else has room to spare.")
    out.append("")
    return "\n".join(out)
