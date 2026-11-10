"""Cross-level scaling analysis -- compare the system across a load sweep.

The load sweep runs the whole pipeline once per virtual-user level and hands the
per-level results here. This module reads the *performance* story across levels
(reliability -- Layers 2-4 -- is load-independent, so it is identical at every
level and lives in each per-level ``report.txt``):

  * throughput vs load     -- where the achieved arrival rate plateaus = ceiling
  * utilization vs load     -- which service crosses the target first = bottleneck
  * response time vs load   -- as measured at each service (not the model's)
  * required replicas       -- the capacity.py sizing at each level

and turns them into a verdict: the bottleneck, the highest safe load, which
services are over- or under-provisioned, and the replica counts to serve the top
level. Nothing here is app-specific -- it reads whatever services the spec names.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass

from analyzer.diagnose import (
    diagnose, diagnosis_explain, diagnosis_report, sweep_position)
from analyzer.hotspots import hotspots_explain, hotspots_report
from analyzer.logscan import findings_explain, findings_report
from analyzer.responsiveness import (
    comfortable, first_strained, plain_strain, sweep_table, tech_summary)
from analyzer.saturation import capacity_ceiling_explain, capacity_ceiling_report
from analyzer.scaleout import scaleout_explain, scaleout_report
from analyzer.spec import fmt_ms
from analyzer.validity import validity_summary

_MIN_YR = 365 * 24 * 60


@dataclass
class LevelResult:
    """One rung of the load sweep."""

    vus: int
    measurements: dict       # the merged measured data used for this level
    results: dict            # analyze() output
    sizings: dict            # name -> capacity.Sizing
    validity: object = None  # validity.Validity of this level's run, if checked
    hotspots: object = None  # hotspots.Hotspots measured at this level, if any
    feel: object = None      # responsiveness.Feel: what k6 measured, judged
    logs: object = None      # logscan record (log-signals.json) of this level


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


def _floor_mu(lv, name):
    """True when this level's mu is only a floor: the sweep never saw the
    service level off (saturation.py), so its rho is an upper bound that proves
    no overload."""
    return bool(lv.measurements["services"][name].get("service_rate_lower_bound"))


def _saturated(lv, name, target):
    if _floor_mu(lv, name):
        return False
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
        if all(_floor_mu(lv, n) or (_stable(lv, n) and _rho(lv, n) <= target)
               for n in spec["services"]):
            safe = lv.vus
    return safe


def provisioning(spec, levels):
    """Split services into under- / over- / right- / unreliable across the sweep.

    Sizing is judged only on levels where it is trustworthy (the service was not
    overloaded when ``mu`` was measured -- see ``capacity.Sizing.reliable``). A
    service under-provisioned at ANY reliable level is "under"; one over at EVERY
    reliable level is "over"; else "right". A service with NO reliable level
    (overloaded throughout) can't be sized at all -> "unreliable". A service
    whose limit the sweep never reached (:func:`unreached`) is in none of them.
    """
    under, over, ok, unreliable = [], [], [], []
    skip = set(unreached(spec, levels))
    for name in spec["services"]:
        if name in skip:
            continue
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


def _comfortable_by_feel(levels, safe):
    """(highest comfortable VUs, first strained Feel) once what the users saw
    (responsiveness.py) is weighed in with the model's ``safe`` level.

    The strained Feel comes back only when it is what sets the limit; when
    the model's own limit comes first, its answer stands: ``(safe, None)``.
    """
    feels = {lv.vus: lv.feel for lv in levels if lv.feel is not None}
    strained = first_strained(feels)
    if strained is None:
        return safe, None
    felt_ok = comfortable(feels)
    if felt_ok is None:                  # users felt even the lightest level
        return None, strained
    if safe is None or safe <= felt_ok:
        return safe, None
    return felt_ok, strained


def _past_peak(levels, spec, target):
    """The sweep's position when its top level is past the limit it hit, else
    None: then the top level's arrival rate counts refused requests."""
    pos = sweep_position(levels, spec, target)
    if pos is None:
        return None
    top = max(levels, key=lambda lv: lv.vus)
    return pos if pos.at(top.vus, top.measurements, spec).state == "past" else None


def _first_felt(levels):
    """The lightest level the users felt (a responsiveness.Feel), or None."""
    return first_strained({lv.vus: lv.feel for lv in levels
                           if lv.feel is not None})


def _all_felt_ok(levels):
    """Every level was judged by what the users saw, and none was strained."""
    feels = [lv.feel for lv in levels
             if lv.feel is not None and lv.feel.status != "unknown"]
    return bool(feels) and not any(f.strained for f in feels)


def unreached(spec, levels):
    """Services that kept pace with the users at every level: their limit lies
    above the sweep, so neither a bottleneck nor a replica count can be read."""
    return [n for n in spec["services"]
            if levels and all(_floor_mu(lv, n) for lv in levels)]


def _counts(spec, scaleout, load_rps, target, linear, skip=()):
    """Copies per service to serve ``load_rps`` at target, as display strings,
    and whether any came from the measured gain per copy (scaleout.py) rather
    than ``linear`` (each copy a full copy; None = can't size). Services in
    ``skip`` (limit not reached) are not sized from the gain."""
    out, measured = {}, False
    for n in spec["services"]:
        c = (scaleout.copies_for(n, load_rps, target)
             if scaleout is not None and n not in skip else None)
        if c is not None:
            out[n] = ("≤" if scaleout.bound == "at least" else "") + str(c)
            measured = True
        else:
            out[n] = str(linear[n]) if linear[n] else "n/a"
    return out, measured


def _host_share_at_peak(spec, levels):
    """Share of the host's cores all containers used at the busiest level,
    from the validity check's CPU numbers; None if not measured."""
    from analyzer.diagnose import _entry_rate
    top = max(levels, key=lambda lv: _entry_rate(lv, spec))
    v = top.validity
    if v is None or not getattr(v, "host_cores", None) or not v.cpu_by_service:
        return None
    return sum(v.cpu_by_service.values()) / v.host_cores


def _hidden_caveat(dr, scaleout, plain=False) -> str:
    """What a hidden limit means for adding copies: measured by another sweep
    (scaleout.py), or still an assumption."""
    if dr.hidden is None:
        return ""
    if scaleout is not None and scaleout.usable:
        g = next(iter(scaleout.gains.values()))
        pct = f"{g.efficiency * 100:.0f}%"
        if plain:
            return (f"the other test measured it: each extra copy added {pct} "
                    "of a full one")
        return f"measured: each added copy adds {pct} of a full one"
    if plain:
        return ("that holds only if each copy brings its own share of the "
                "hidden limit")
    return "only if the hidden limit is per replica"


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


def scaling_report(spec, levels, target_utilization, max_response=None,
                   scaleout=None) -> str:
    """The technical cross-level report. ``scaleout`` (scaleout.ScaleOut):
    what extra copies really added, measured against another sweep; copy
    counts then use it instead of assuming each copy adds a full copy."""
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
        if _floor_mu(lv, n):
            return f"≤{r:.2f}"
        if not _stable(lv, n) or r >= 1.0:
            return f"{r:.2f}!"
        if r >= target_utilization:
            return f"{r:.2f}*"
        return f"{r:.2f}"
    out += _table(spec, levels, _rho_cell, "VUs")

    out += _rule("Mean response time -- MEASURED at each service (ms)")
    out += _table(spec, levels,
                  lambda lv, n: fmt_ms(lv.results["services"][n].latency_ms), "VUs")
    out.append("  (each service's own mean from its request-duration histogram, "
               "queueing included;")
    out.append("   a caller's includes its callees'. The M/M/c model's response "
               "time is not shown:")
    out.append("   it assumes a replica serves one request at a time, so for a "
               "concurrent server")
    out.append("   it can be off by orders of magnitude. — = not measured.)")

    felt = sweep_table(spec, levels)
    if felt:
        out += _rule("Responsiveness -- what the users saw (k6, end to end)")
        out += felt

    out += _rule("Replicas needed (M/M/c sizing)   [current in brackets]")
    current = {n: levels[-1].sizings[n].current for n in spec["services"]}
    out.append(f"{'current':<10}"
               + "".join(f"{('[' + str(current[n]) + ']'):>13}"
                         for n in spec["services"]))
    def _need_cell(lv, n):
        s = lv.sizings[n]
        if _floor_mu(lv, n):               # limit never reached -> nothing to size
            return "—"
        if not s.reliable:                 # μ captured under saturation -> don't guess
            return "n/a"
        mark = "▲" if s.delta > 0 else ("▽" if s.delta < 0 else "=")
        return f"{s.needed}{mark}"
    out += _table(spec, levels, _need_cell, "VUs")
    out.append("  (n/a = overloaded during measurement -> μ unreliable, not sized)")
    if any(_floor_mu(lv, n) for lv in levels for n in spec["services"]):
        out.append("  (≤ / — = no plateau: the service kept pace with the users, "
                   "so μ is a floor,")
        out.append("   ρ is an upper bound, and no replica count follows)")

    # ---- verdict --------------------------------------------------------
    out += _rule("Verdict -- what scales, what doesn't")
    bn = bottleneck(spec, levels, target_utilization)
    safe = max_safe_load(spec, levels, target_utilization)
    under, over, ok, unreliable = provisioning(spec, levels)
    not_reached = unreached(spec, levels)
    comfy, strained = _comfortable_by_feel(levels, safe)

    if strained is not None and comfy is None:
        out.append(f"  Highest comfortable load : below {vus[0]} VUs (users felt "
                   f"even the lightest level: {tech_summary(strained)})")
    elif strained is not None:
        out.append(f"  Highest comfortable load : {comfy} VUs (at {strained.vus} "
                   f"VUs users felt it: {tech_summary(strained)})")
    elif safe is not None and not_reached:
        out.append(f"  Highest comfortable load : {safe} VUs "
                   f"(kept up; {', '.join(not_reached)} not at a measurable "
                   "limit, so utilization unknown"
                   + ("; users noticed nothing" if _all_felt_ok(levels) else "")
                   + ")")
    elif safe is not None:
        out.append(f"  Highest comfortable load : {safe} VUs "
                   f"(all services within target)")
    else:
        out.append(f"  Highest comfortable load : below {vus[0]} VUs "
                   f"(something is already at/over target at the lowest level)")
    dr = diagnose(levels, spec, target_utilization)
    if bn and dr.hidden is not None:
        out.append(f"  Bottleneck               : hidden, not CPU ({bn[0]} saturated "
                   f"first, at {bn[1]} VUs, but the host had spare cores when "
                   "throughput levelled off; see the diagnosis below)")
    elif bn:
        out.append(f"  Bottleneck               : {bn[0]} "
                   f"(first to saturate, at {bn[1]} VUs)")
    else:
        out.append("  Bottleneck               : none within the tested range")

    caveat = _hidden_caveat(dr, scaleout)
    if under:
        out.append(f"  Under-provisioned        : {', '.join(under)} "
                   "(needs more replicas at some tested load"
                   + (f"; {caveat}, see the diagnosis)" if caveat else ")"))
    if over:
        out.append(f"  Over-provisioned         : {', '.join(over)} "
                   "(more replicas than any tested load needs)")
    if ok:
        out.append(f"  Right-sized              : {', '.join(ok)}")
    if unreliable:
        out.append(f"  Could not size (latency)  : {', '.join(unreliable)} "
                   "(overloaded during measurement -> latency-μ unreliable; see "
                   "'Supported load' below for a throughput-based estimate)")
    if not_reached:
        out.append(f"  Limit not reached        : {', '.join(not_reached)} "
                   "(throughput kept pace with the users at every level; "
                   "capacity above the highest tested load)")

    top = levels[-1]
    kept = ("kept up, but users felt it" if top.feel is not None
            and top.feel.strained else "kept up")
    past_top = _past_peak(levels, spec, target_utilization)
    if past_top is not None:
        # beyond the peak the arrival rate counts refused requests: size for
        # the most the system actually served, from the plateau
        linear = {n: dr.diagnoses[n].replicas_needed for n in spec["services"]}
        counts, measured = _counts(spec, scaleout, past_top.peak_rps,
                                   target_utilization, linear)
        out.append(f"\n  To serve the most it handled ({past_top.peak_vus} VUs, "
                   f"~{past_top.peak_rps:.0f} req/s) at target, run: "
                   + ", ".join(f"{n}={c}" for n, c in counts.items())
                   + (f" -- {caveat}" if caveat and not measured else ""))
        if measured:
            out.append("  (counting the measured gain per added copy; full "
                       "copies would need "
                       + ", ".join(f"{n}={linear[n] or 'n/a'}"
                                   for n in spec["services"]) + ")")
        out.append(f"  ({top.vus} VUs is past the limit: more users there only "
                   "meant refused requests.)")
    else:
        from analyzer.diagnose import _entry_rate
        linear = {n: top.sizings[n].needed if top.sizings[n].reliable else None
                  for n in spec["services"]}
        counts, measured = _counts(spec, scaleout, _entry_rate(top, spec),
                                   target_utilization, linear, skip=not_reached)
        parts = [f"{n}={top.sizings[n].current} ({kept})" if n in not_reached
                 else f"{n}={counts[n]}" for n in spec["services"]]
        out.append(f"\n  To serve {top.vus} VUs at target, run: {', '.join(parts)}")
        if measured:
            out.append("  (counting the measured gain per added copy; full "
                       "copies would need "
                       + ", ".join(f"{n}={linear[n] or 'n/a'}"
                                   for n in spec["services"]
                                   if n not in not_reached) + ")")
    if past_top is None and any(not top.sizings[n].reliable
                                and n not in not_reached
                                for n in spec["services"]):
        out.append("  (n/a = overloaded during measurement; a throughput-based "
                   "estimate is in 'Supported load' below.)")

    if scaleout is not None:
        out.append(scaleout_report(scaleout, spec,
                                   _host_share_at_peak(spec, levels)).rstrip())

    # throughput-plateau capacity (recovers a clean μ where latency-μ failed)
    out.append(capacity_ceiling_report(levels, spec, target_utilization,
                                       scaleout).rstrip())

    # scale-out vs optimize routing per bottleneck
    out.append(diagnosis_report(dr, spec, _first_felt(levels), scaleout).rstrip())
    if dr.logs is not None:
        out.append(findings_report(dr.logs).rstrip())

    # which endpoints the time goes to, at the heaviest level measured
    if top.hotspots is not None and top.hotspots.by_service:
        out.append(hotspots_report(spec, top.hotspots, top=5).rstrip()
                   .replace("Where each service spends its time",
                            f"At {top.vus} VUs: where each service spends its time", 1))

    out.append(validity_summary(levels).rstrip())

    out.append("\n  (Reliability -- Layers 2-4 -- does not change with load; see "
               "each per-level report.txt.)")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# plain-language report
# ---------------------------------------------------------------------------

def scaling_explain(spec, levels, target_utilization, max_response=None,
                    scaleout=None) -> str:
    """The non-technical version of the scaling verdict."""
    levels = sorted(levels, key=lambda x: x.vus)
    vus = [lv.vus for lv in levels]
    bn = bottleneck(spec, levels, target_utilization)
    safe = max_safe_load(spec, levels, target_utilization)
    under, over, ok, unreliable = provisioning(spec, levels)
    not_reached = unreached(spec, levels)
    top = levels[-1]

    out = [f"PLAIN-LANGUAGE SCALING SUMMARY -- {spec.get('name', 'system')}",
           "=" * 70, "",
           "We didn't test at just one traffic level -- we tried several and",
           f"watched how the system coped: {', '.join(map(str, vus))} "
           "simultaneous users.", ""]

    out.append(capacity_ceiling_explain(levels, spec, target_utilization,
                                        scaleout))

    out.append("HOW FAR IT COMFORTABLY STRETCHES")
    out.append("-" * 70)
    comfy, strained = _comfortable_by_feel(levels, safe)
    if strained is not None and comfy is None:
        out.append(f"  Even at the lightest test ({vus[0]} users) the people "
                   "using it would have")
        out.append("  noticed:")
        out.append(f"    {plain_strain(strained)}.")
    elif strained is not None:
        out.append(f"  It stayed comfortable up to about {comfy} users. At "
                   f"{strained.vus} users the people")
        out.append("  using it would have noticed:")
        out.append(f"    {plain_strain(strained)}.")
        if not_reached:
            out.append("  It still served everyone at the pace they asked, so this "
                       "is slowness, not")
            out.append("  a part running out of room: the load test sees it before "
                       "the capacity math does.")
    elif safe is not None and safe >= top.vus and not_reached:
        out.append(f"  It kept up across the whole range, up to {safe} users. How "
                   "close the top")
        if _all_felt_ok(levels):
            out.append("  was to its limit we can't tell: nothing levelled off, "
                       "and the load test")
            out.append("  saw no slowdown or failed requests either.")
        else:
            out.append("  was to its limit we can't tell: nothing levelled off. "
                       "Check the response")
            out.append("  times in each load-<N>/k6-output.txt for how it felt to "
                       "users.")
    elif safe is not None and safe >= top.vus:
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
    dr = diagnose(levels, spec, target_utilization)
    if bn and dr.hidden is not None:
        out.append(f"  By the numbers '{bn[0]}' filled up first, at around "
                   f"{bn[1]} users, but the machine")
        out.append("  still had power to spare when everything levelled off: "
                   "the real limit is")
        out.append("  hidden inside (see SHOULD YOU ADD SERVERS, OR FIX THE "
                   "CODE? below).")
    elif bn:
        out.append(f"  '{bn[0]}' is the weakest link: it filled up first, at "
                   f"around {bn[1]} users,")
        out.append("  while the others still had room. Give it more copies and the")
        out.append("  whole system stretches further.")
    elif strained is not None:
        out.append("  No single part filled up in the range we tested, but the "
                   "people using it")
        out.append(f"  noticed from {strained.vus} users (see above): the limit "
                   "is close even so.")
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
    if under and dr.hidden is not None and scaleout is not None \
            and scaleout.usable:
        out.append(f"  Too few copies for the heavier loads: {', '.join(under)}.")
        out.append("  The limit is hidden (see SHOULD YOU ADD SERVERS, OR FIX "
                   "THE CODE?), but")
        out.append(f"  {_hidden_caveat(dr, scaleout, plain=True)} (see DID MORE "
                   "COPIES HELP AS MUCH AS EXPECTED?).")
    elif under and dr.hidden is not None:
        out.append(f"  Too few copies for the heavier loads, by the numbers: "
                   f"{', '.join(under)}.")
        out.append("  But the limit is hidden (see SHOULD YOU ADD SERVERS, OR FIX "
                   "THE CODE?): more")
        out.append("  copies help only if each brings its own share of it.")
    elif under:
        out.append(f"  Too few copies for the heavier loads (add some): "
                   f"{', '.join(under)}.")
    if unreliable:
        out.append(f"  Couldn't tell how many copies these need -- they were "
                   f"overloaded the")
        out.append(f"  whole time, which usually means slow requests, not just "
                   f"too few copies:")
        out.append(f"  {', '.join(unreliable)}. Speed each request up first.")
    if not_reached:
        out.append(f"  Never ran out of room, so their limit is above these tests: "
                   f"{', '.join(not_reached)}.")
        out.append("  The copies you have kept up; whether fewer would do, or "
                   "how many")
        out.append("  more users they can take, needs a test with more users.")
    if not (ok or over or under or unreliable or not_reached):
        out.append("  Everything is balanced for the loads we tested.")
    out.append("")

    if scaleout is not None:
        out.append(scaleout_explain(scaleout, spec))

    out.append("THE RECIPE FOR YOUR BUSIEST TEST")
    out.append("-" * 70)
    ready = [n for n in spec["services"]
             if top.sizings[n].reliable and n not in not_reached]
    not_ready = [n for n in spec["services"]
                 if not top.sizings[n].reliable and n not in not_reached]
    past_top = _past_peak(levels, spec, target_utilization)

    def _full_copies(linear, names):
        return ", ".join(f"{linear[n]} × {n}" for n in names if linear[n])

    if past_top is not None:
        linear = {n: dr.diagnoses[n].replicas_needed for n in spec["services"]}
        counts, measured = _counts(spec, scaleout, past_top.peak_rps,
                                   target_utilization, linear)
        recipe = ", ".join(f"{counts[n]} × {n}" for n in spec["services"]
                           if counts[n] != "n/a")
        out.append(f"  The most it handled was at {past_top.peak_vus} users; past "
                   "that, more users only")
        out.append("  meant refused requests. To serve that load with room to "
                   f"spare, run: {recipe}.")
        if measured:
            out.append("  That counts what an extra copy really added in the "
                       "other test; counting")
            out.append("  each copy as a full one it would be "
                       f"{_full_copies(linear, spec['services'])}.")
        elif dr.hidden is not None:
            out.append("  That holds only if each copy brings its own share of the "
                       "hidden limit")
            out.append("  (see SHOULD YOU ADD SERVERS, OR FIX THE CODE?).")
    elif ready or not_reached:
        from analyzer.diagnose import _entry_rate
        linear = {n: top.sizings[n].needed for n in ready}
        counts, measured = _counts(spec, scaleout, _entry_rate(top, spec),
                                   target_utilization,
                                   {n: linear.get(n) for n in spec["services"]},
                                   skip=not_reached)
        recipe = ", ".join([f"{counts[n]} × {n}" for n in ready]
                           + [f"{top.sizings[n].current} × {n} (what you ran; "
                              "it kept up)" for n in not_reached])
        if measured:
            recipe += (" (counting what an extra copy really added; as full "
                       f"copies: {_full_copies(linear, ready)})")
        if top.feel is not None and top.feel.strained:
            out.append(f"  At {top.vus} users you ran {recipe}. Everyone was "
                       "served, but not")
            out.append("  smoothly (see HOW FAR IT COMFORTABLY STRETCHES).")
        else:
            out.append(f"  To serve {top.vus} users smoothly, run: {recipe}.")
    else:
        out.append(f"  We can't give a copy count for {top.vus} users yet.")
    if not_ready and past_top is None:
        out.append(f"  Left out: {', '.join(not_ready)} -- overloaded even here, "
                   "so any copy")
        out.append("  count would be a guess. Make each request faster first; "
                   "adding copies")
        out.append("  won't help until then.")
    out.append("")

    out.append(diagnosis_explain(dr, spec, _first_felt(levels), scaleout))
    if dr.logs is not None:
        out.append(findings_explain(dr.logs))

    if top.hotspots is not None and top.hotspots.by_service:
        out.append(hotspots_explain(spec, top.hotspots))

    checked = [lv for lv in levels if lv.validity is not None]
    if checked:
        out.append("CAN THESE NUMBERS BE TRUSTED?")
        out.append("-" * 70)
        bad = [lv.vus for lv in checked if not lv.validity.valid]
        over = [lv for lv in checked if lv.validity.overloaded]
        if bad:
            out.append(f"  Not all of them: the tests at {', '.join(map(str, bad))} "
                       "users went wrong, so")
            out.append("  their numbers should be ignored (see load-<N>/validity-"
                       "explained.txt).")
        else:
            out.append(f"  Yes: every test level ({len(checked)}) worked as "
                       "planned.")
        if over:
            said = "; ".join(f"{lv.vus} users: {lv.validity.failed_share}"
                             if lv.validity.failed_share else f"{lv.vus} users"
                             for lv in over)
            out.append(textwrap.fill(
                f"At the heaviest loads the system couldn't keep up ({said}). "
                "That is a result, not a broken test: the failure rates stand, "
                "and only the estimates of how much it can take leave those "
                "loads out.", 72, initial_indent="  ", subsequent_indent="  "))
            if over[0].vus == min(lv.vus for lv in checked):
                out.append(textwrap.fill(
                    "Even the lightest test failed requests, though: before "
                    "reading this as too much traffic, make sure the test "
                    "itself works (an expired login, a wrong address).", 72,
                    initial_indent="  ", subsequent_indent="  "))
        out.append("")
    return "\n".join(out)
