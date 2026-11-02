"""Cross-level scaling analysis -- compare the system across a load sweep.

The load sweep runs the whole pipeline once per virtual-user level and hands the
per-level results here. This module reads the *performance* story across levels
(reliability -- Layers 2-4 -- is load-independent, so it is identical at every
level and lives in each per-level ``report.txt``):

  * throughput vs load     -- where the achieved arrival rate plateaus = ceiling
  * utilization vs load     -- which service crosses the target first = bottleneck
  * response time vs load   -- the M/M/c "hockey stick"
  * required replicas       -- the capacity.py sizing at each level

and turns them into a verdict: the bottleneck, the highest safe load, which
services are over- or under-provisioned, and the replica counts to serve the top
level. Nothing here is app-specific -- it reads whatever services the spec names.
"""

from __future__ import annotations

from dataclasses import dataclass

from analyzer.diagnose import diagnose, diagnosis_explain, diagnosis_report
from analyzer.saturation import capacity_ceiling_explain, capacity_ceiling_report

_MIN_YR = 365 * 24 * 60


@dataclass
class LevelResult:
    """One rung of the load sweep."""

    vus: int
    measurements: dict       # the merged measured data used for this level
    results: dict            # analyze() output
    sizings: dict            # name -> capacity.Sizing


# ---------------------------------------------------------------------------
# per-level accessors
# ---------------------------------------------------------------------------

def _q(lv: LevelResult, name: str):
    return lv.results["services"][name].queue


def _rho(lv, name):
    return _q(lv, name).rho


def _stable(lv, name):
    return _q(lv, name).stable


def _lam(lv, name):
    return float(lv.measurements["services"][name]["arrival_rate"])


def _saturated(lv, name, target):
    return (not _stable(lv, name)) or _rho(lv, name) >= target


# ---------------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------------

def first_saturating_level(levels, name, target):
    """Lowest VU level at which a service reaches the target/overloads, or None."""
    for lv in sorted(levels, key=lambda x: x.vus):
        if _saturated(lv, name, target):
            return lv.vus
    return None


def bottleneck(spec, levels, target):
    """(service, vus) of whatever saturates at the lowest load, or None."""
    best = None
    for name in spec["services"]:
        v = first_saturating_level(levels, name, target)
        if v is not None and (best is None or v < best[1]):
            best = (name, v)
    return best


def max_safe_load(spec, levels, target):
    """Highest VU level at which every service stays stable and <= target."""
    safe = None
    for lv in sorted(levels, key=lambda x: x.vus):
        if all(_stable(lv, n) and _rho(lv, n) <= target for n in spec["services"]):
            safe = lv.vus
    return safe


def provisioning(spec, levels):
    """Split services into under- / over- / right- / unreliable across the sweep.

    Sizing is judged only on levels where it is trustworthy (the service was not
    overloaded when ``mu`` was measured -- see ``capacity.Sizing.reliable``). A
    service under-provisioned at ANY reliable level is "under"; one over at EVERY
    reliable level is "over"; else "right". A service with NO reliable level
    (overloaded throughout) can't be sized at all -> "unreliable".
    """
    under, over, ok, unreliable = [], [], [], []
    for name in spec["services"]:
        rel = [lv for lv in levels if lv.sizings[name].reliable]
        if not rel:
            unreliable.append(name)
        elif any(lv.sizings[name].delta > 0 for lv in rel):
            under.append(name)
        elif all(lv.sizings[name].delta < 0 for lv in rel):
            over.append(name)
        else:
            ok.append(name)
    return under, over, ok, unreliable


# ---------------------------------------------------------------------------
# technical report
# ---------------------------------------------------------------------------

def _short(name: str, w: int = 12) -> str:
    # keep the distinctive prefix ("comment-serv"), not the shared "-service" tail
    return name if len(name) <= w else name[:w]


def _table(spec, levels, cell, header, w: int = 13) -> list:
    names = spec["services"]
    head = f"{header:<10}" + "".join(f"{_short(n, w - 1):>{w}}" for n in names)
    rows = [head]
    for lv in sorted(levels, key=lambda x: x.vus):
        rows.append(f"{lv.vus:<10}" + "".join(f"{cell(lv, n):>{w}}" for n in names))
    return rows


def _rule(title: str) -> list:
    return ["", "=" * 74, title, "=" * 74]


def scaling_report(spec, levels, target_utilization, max_response=None) -> str:
    """The technical cross-level report."""
    levels = sorted(levels, key=lambda x: x.vus)
    vus = [lv.vus for lv in levels]
    out = [f"\nLOAD-SWEEP SCALING REPORT -- {spec.get('name', 'system')}",
           f"levels tested (virtual users): {', '.join(map(str, vus))}",
           f"utilization target: ρ ≤ {target_utilization:.2f}"
           + (f"   response target: {max_response * 1000:.0f} ms"
              if max_response else "")]

    out += _rule("Throughput -- measured arrival rate λ (req/s)")
    out += _table(spec, levels, lambda lv, n: f"{_lam(lv, n):.1f}", "VUs")

    out += _rule("Utilization ρ   (* = above target, ! = overloaded)")
    def _rho_cell(lv, n):
        r = _rho(lv, n)
        if not _stable(lv, n) or r >= 1.0:
            return f"{r:.2f}!"
        if r >= target_utilization:
            return f"{r:.2f}*"
        return f"{r:.2f}"
    out += _table(spec, levels, _rho_cell, "VUs")

    out += _rule("Mean response time W   (ms, 'OVER' = unstable)")
    def _w_cell(lv, n):
        q = _q(lv, n)
        return "OVER" if not q.stable else f"{q.W * 1000:.0f}"
    out += _table(spec, levels, _w_cell, "VUs")

    out += _rule("Replicas needed (M/M/c sizing)   [current in brackets]")
    current = {n: levels[-1].sizings[n].current for n in spec["services"]}
    out.append(f"{'current':<10}"
               + "".join(f"{('[' + str(current[n]) + ']'):>13}"
                         for n in spec["services"]))
    def _need_cell(lv, n):
        s = lv.sizings[n]
        if not s.reliable:                 # μ captured under saturation -> don't guess
            return "n/a"
        mark = "▲" if s.delta > 0 else ("▽" if s.delta < 0 else "=")
        return f"{s.needed}{mark}"
    out += _table(spec, levels, _need_cell, "VUs")
    out.append("  (n/a = overloaded during measurement -> μ unreliable, not sized)")

    # ---- verdict --------------------------------------------------------
    out += _rule("Verdict -- what scales, what doesn't")
    bn = bottleneck(spec, levels, target_utilization)
    safe = max_safe_load(spec, levels, target_utilization)
    under, over, ok, unreliable = provisioning(spec, levels)

    if safe is not None:
        out.append(f"  Highest comfortable load : {safe} VUs "
                   f"(all services within target)")
    else:
        out.append(f"  Highest comfortable load : below {vus[0]} VUs "
                   f"(something is already at/over target at the lowest level)")
    if bn:
        out.append(f"  Bottleneck               : {bn[0]} "
                   f"(first to saturate, at {bn[1]} VUs)")
    else:
        out.append("  Bottleneck               : none within the tested range")

    if under:
        out.append(f"  Under-provisioned        : {', '.join(under)} "
                   "(needs more replicas at some tested load)")
    if over:
        out.append(f"  Over-provisioned         : {', '.join(over)} "
                   "(more replicas than any tested load needs)")
    if ok:
        out.append(f"  Right-sized              : {', '.join(ok)}")
    if unreliable:
        out.append(f"  Could not size (latency)  : {', '.join(unreliable)} "
                   "(overloaded during measurement -> latency-μ unreliable; see "
                   "'Supported load' below for a throughput-based estimate)")

    top = levels[-1]
    parts = [f"{n}={top.sizings[n].needed}" if top.sizings[n].reliable
             else f"{n}=n/a" for n in spec["services"]]
    out.append(f"\n  To serve {top.vus} VUs at target, run: {', '.join(parts)}")
    if any(not top.sizings[n].reliable for n in spec["services"]):
        out.append("  (n/a = overloaded during measurement; a throughput-based "
                   "estimate is in 'Supported load' below.)")

    # throughput-plateau capacity (recovers a clean μ where latency-μ failed)
    out.append(capacity_ceiling_report(levels, spec, target_utilization).rstrip())

    # scale-out vs optimize routing per bottleneck
    out.append(diagnosis_report(
        diagnose(levels, spec, target_utilization), spec).rstrip())

    out.append("\n  (Reliability -- Layers 2-4 -- does not change with load; see "
               "each per-level report.txt.)")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# plain-language report
# ---------------------------------------------------------------------------

def scaling_explain(spec, levels, target_utilization, max_response=None) -> str:
    """The non-technical version of the scaling verdict."""
    levels = sorted(levels, key=lambda x: x.vus)
    vus = [lv.vus for lv in levels]
    bn = bottleneck(spec, levels, target_utilization)
    safe = max_safe_load(spec, levels, target_utilization)
    under, over, ok, unreliable = provisioning(spec, levels)
    top = levels[-1]

    out = [f"PLAIN-LANGUAGE SCALING SUMMARY -- {spec.get('name', 'system')}",
           "=" * 70, "",
           "We didn't test at just one traffic level -- we tried several and",
           f"watched how the system coped: {', '.join(map(str, vus))} "
           "simultaneous users.", ""]

    out.append(capacity_ceiling_explain(levels, spec, target_utilization))

    out.append("HOW FAR IT COMFORTABLY STRETCHES")
    out.append("-" * 70)
    if safe is not None and safe >= top.vus:
        out.append(f"  It handled the whole range comfortably, up to {safe} users.")
    elif safe is not None:
        out.append(f"  It stayed comfortable up to about {safe} users. Past that,")
        out.append("  at least one part started to struggle.")
    else:
        out.append(f"  Even at the lightest test ({vus[0]} users) something was")
        out.append("  already near its limit -- it is under-powered for this traffic.")
    out.append("")

    out.append("THE FIRST THING TO RUN OUT OF ROOM (THE BOTTLENECK)")
    out.append("-" * 70)
    if bn:
        out.append(f"  '{bn[0]}' is the weakest link: it filled up first, at "
                   f"around {bn[1]} users,")
        out.append("  while the others still had room. Give it more copies and the")
        out.append("  whole system stretches further.")
    else:
        out.append("  Nothing hit its limit in the range we tested -- there is")
        out.append("  headroom above the top level.")
    out.append("")

    out.append("WHAT YOU GOT RIGHT, AND WHAT TO ADJUST")
    out.append("-" * 70)
    if ok:
        out.append(f"  Right-sized for this traffic: {', '.join(ok)}.")
    if over:
        out.append(f"  More copies than needed (you could save some): "
                   f"{', '.join(over)}.")
    if under:
        out.append(f"  Too few copies for the heavier loads (add some): "
                   f"{', '.join(under)}.")
    if unreliable:
        out.append(f"  Couldn't tell how many copies these need -- they were "
                   f"overloaded the")
        out.append(f"  whole time, which usually means slow requests, not just "
                   f"too few copies:")
        out.append(f"  {', '.join(unreliable)}. Speed each request up first.")
    if not (ok or over or under or unreliable):
        out.append("  Everything is balanced for the loads we tested.")
    out.append("")

    out.append("THE RECIPE FOR YOUR BUSIEST TEST")
    out.append("-" * 70)
    ready = [n for n in spec["services"] if top.sizings[n].reliable]
    not_ready = [n for n in spec["services"] if not top.sizings[n].reliable]
    if ready:
        recipe = ", ".join(f"{top.sizings[n].needed} × {n}" for n in ready)
        out.append(f"  To serve {top.vus} users smoothly, run: {recipe}.")
    else:
        out.append(f"  We can't give a copy count for {top.vus} users yet.")
    if not_ready:
        out.append(f"  Left out: {', '.join(not_ready)} -- overloaded even here, "
                   "so any copy")
        out.append("  count would be a guess. Make each request faster first; "
                   "adding copies")
        out.append("  won't help until then.")
    out.append("")

    out.append(diagnosis_explain(
        diagnose(levels, spec, target_utilization), spec))
    return "\n".join(out)
