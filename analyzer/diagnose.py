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

A third case looks like "cheap but swamped" and is neither: the sweep levels
off while the machine still has CPU to spare. Then the limit is not work at all
but something that caps how much work runs at once -- a connection pool, a
worker or thread limit, a lock (CLAUDE.md, "saturated while the CPU idles").
Adding replicas helps only if that limit is per replica, so such services get
the verdict "hidden" instead of "scale", with the CPU evidence (``hidden_limit``).
It needs the per-level CPU the validity check measures.
"""

from __future__ import annotations

import math
import statistics
import textwrap
from dataclasses import dataclass

from analyzer.logscan import KINDS, findings
from analyzer.saturation import _rows, estimate_capacity, plateau_level
from analyzer.spec import container_target

# The machine had CPU to spare if all its containers together used at most this
# share of its cores at the level where throughput peaked. The validity check
# calls the host saturated at 85%; well below that, CPU did not stop the growth.
_HIDDEN_HOST_SHARE = 0.7


@dataclass
class Diagnosis:
    service: str
    mu_clean: float               # clean per-replica service rate (req/s)
    service_time_ms: float        # S = 1000 / mu_clean
    peak_utilization: float       # highest utilization across the sweep
    overloaded: bool
    heavy_request: bool
    verdict: str                  # "ok" | "scale" | "optimize" | "hidden"
    replicas_needed: int | None   # for a scale verdict
    gain_per_replica: float       # req/s each added replica buys (= mu_clean)
    lower_bound: bool = False     # never levelled off: limit not reached, and
                                  # mu_clean / service time are only bounds


@dataclass
class HiddenLimit:
    """The sweep levelled off while the machine had CPU to spare."""

    vus: int                      # the level where throughput peaked
    peak_rps: float               # the entry service's arrival rate there
    host_used: float              # cores all containers used at that level
    host_cores: float
    cpu: dict                     # service -> (cores used, replicas) there
    fall: tuple | None = None     # next level: (vus, rps, busiest service, its
                                  # cores), when throughput fell after the peak


@dataclass
class DiagnoseResult:
    diagnoses: dict               # name -> Diagnosis
    threshold_ms: float           # the "heavy request" line
    median_ms: float
    target: float
    hidden: HiddenLimit | None = None
    logs: object = None           # logscan.LogFindings, when levels were scanned


def _entry_rate(lv, spec) -> float:
    """The busiest service's arrival rate: the traffic entering the system."""
    return max(float(lv.measurements["services"][n]["arrival_rate"])
               for n in spec["services"])


def hidden_limit(levels, spec, caps) -> HiddenLimit | None:
    """A :class:`HiddenLimit` when the sweep levelled off (a service saturated
    on an observed plateau) while the host had spare cores, else None.

    Reads the per-level CPU the validity check measured (``LevelResult.validity``);
    without it there is no evidence either way, so None.
    """
    if not any(caps[n].saturated for n in spec["services"]):
        return None
    levels = sorted(levels, key=lambda lv: lv.vus)
    top = max(levels, key=lambda lv: _entry_rate(lv, spec))
    v = getattr(top, "validity", None)
    if v is None or not getattr(v, "host_cores", None) or not v.cpu_by_service:
        return None
    used = sum(v.cpu_by_service.values())
    if used > _HIDDEN_HOST_SHARE * v.host_cores:
        return None
    def cores(lv, n):
        return lv.validity.cpu_by_service.get(container_target(spec, n)[0], 0.0)

    cpu = {n: (cores(top, n), int(top.measurements["services"][n]["replicas"]))
           for n in spec["services"]}
    found = HiddenLimit(top.vus, _entry_rate(top, spec), used, v.host_cores, cpu)
    after = [lv for lv in levels if lv.vus > top.vus]
    if after and getattr(after[0], "validity", None) is not None \
            and after[0].validity.cpu_by_service:
        busiest = max(spec["services"], key=lambda n: cpu[n][0])
        found.fall = (after[0].vus, _entry_rate(after[0], spec), busiest,
                      cores(after[0], busiest))
    return found


@dataclass
class LevelPosition:
    """Where one load level stands against the limit its sweep hit.

    The per-level reports judge overload from the queueing model, which on its
    own reads a level PAST the limit as fine: failed requests lower the arrival
    rate it divides. Once a sweep has seen the limit, this is what they say.
    """

    vus: int
    state: str                    # "below" | "at" | "past"
    share: float                  # this level's throughput / the sweep's peak
    rps: float                    # this level's throughput (entry service)
    peak_vus: int
    peak_rps: float
    limit_vus: int | None         # first level that stopped keeping pace
    saturated: list               # services saturated on the observed plateau
    hidden: HiddenLimit | None    # the limit is not CPU (see hidden_limit)


@dataclass
class SweepPosition:
    """The limit a sweep actually hit: where throughput peaked and stopped
    keeping pace with the users. Only for a sweep with an observed plateau."""

    peak_vus: int
    peak_rps: float
    limit_vus: int | None
    saturated: list
    hidden: HiddenLimit | None

    def at(self, vus: int, measurements: dict, spec) -> LevelPosition:
        rps = max(float(measurements["services"][n]["arrival_rate"])
                  for n in spec["services"])
        if (self.limit_vus is not None and vus >= self.limit_vus) \
                or vus > self.peak_vus:
            state = "past"
        elif vus == self.peak_vus:
            state = "at"
        else:
            state = "below"
        share = rps / self.peak_rps if self.peak_rps else 0.0
        return LevelPosition(vus, state, share, rps, self.peak_vus,
                             self.peak_rps, self.limit_vus, self.saturated,
                             self.hidden)


def sweep_position(levels, spec, target) -> SweepPosition | None:
    """The :class:`SweepPosition` of a sweep, or None if it never levelled off
    (or has a single level: no trend to read)."""
    if len(levels) < 2:
        return None
    caps = estimate_capacity(levels, spec, target)
    sat = [n for n in spec["services"] if caps[n].saturated]
    if not sat:
        return None
    levels = sorted(levels, key=lambda lv: lv.vus)
    top = max(levels, key=lambda lv: _entry_rate(lv, spec))
    limits = [v for v in (plateau_level(_rows(levels, n)) for n in sat)
              if v is not None]
    return SweepPosition(top.vus, _entry_rate(top, spec),
                         min(limits) if limits else None, sat,
                         hidden_limit(levels, spec, caps))


def level_position_report(pos: LevelPosition | None) -> str:
    """Technical block for one level's report.txt: below, at or past the limit."""
    if pos is None:
        return ""
    head = {"below": f"BELOW the limit ({pos.share * 100:.0f}% of the peak)",
            "at": "AT the limit (the most the sweep served)",
            "past": "PAST the limit"}[pos.state]
    lines = ["", "=" * 74, "Where this level stands in the load sweep", "=" * 74,
             f"  Throughput peaked at ~{pos.peak_rps:.0f} req/s at {pos.peak_vus} VUs"
             + (f" and stopped keeping pace from {pos.limit_vus} VUs."
                if pos.limit_vus else "."),
             f"  This level: ~{pos.rps:.0f} req/s -> {head}."]
    if pos.state == "past":
        lines += ["  ρ and the sizing here use the rate recovered from that "
                  "peak and count failed",
                  "  requests as arrivals, so they understate the load: read "
                  "this level as overloaded."]
    if pos.hidden is not None:
        lines += ["  The limit is hidden, not CPU (the host had spare cores at "
                  "the peak): replicas",
                  "  help only if it is per replica. See the sweep's diagnosis."]
    return "\n".join(lines) + "\n"


def diagnose(levels, spec, target) -> DiagnoseResult:
    """Classify every service as ok / scale / optimize / hidden from a sweep."""
    cfg = spec.get("diagnose", {})
    factor = float(cfg.get("heavy_factor", 2.0))
    floor_ms = float(cfg.get("heavy_floor_ms", 40.0))

    caps = estimate_capacity(levels, spec, target)
    hidden = hidden_limit(levels, spec, caps)
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
        # a service that kept pace at every level is not overloaded: its rho
        # is computed against a floor for mu, so it only bounds the real one
        overloaded = (cp.saturated or peak_rho[n] >= target) and not cp.lower_bound
        heavy = s >= threshold
        if not overloaded:
            verdict = "ok"
        elif hidden is not None and cp.saturated:
            verdict = "hidden"
        elif heavy:
            verdict = "optimize"
        else:
            verdict = "scale"
        diags[n] = Diagnosis(n, cp.mu_clean, s, peak_rho[n], overloaded, heavy,
                             verdict, cp.replicas_for_peak, cp.mu_clean,
                             cp.lower_bound)
    return DiagnoseResult(diags, threshold, median_ms, target, hidden,
                          findings(levels))


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def copy_gain(x: Diagnosis, dr: DiagnoseResult, scaleout=None):
    """``(req/s an added copy adds, copies to run the peak at target, measured,
    share of a full copy)`` for one service: from another sweep's measured gain
    per copy when one is given and usable (scaleout.py), else a full copy each
    (``mu_clean``). A full copy is what one copy served in the smaller sweep,
    the same yardstick every report uses."""
    if scaleout is not None and scaleout.usable and x.service in scaleout.gains:
        g = scaleout.gains[x.service]
        return (g.gain_per_copy,
                scaleout.copies_for(x.service, scaleout.current.peak_rps,
                                    dr.target), True, g.efficiency)
    return x.gain_per_replica, x.replicas_needed, False, 1.0


def diagnosis_report(dr: DiagnoseResult, spec, felt=None, scaleout=None) -> str:
    """``felt``: the first load level the users felt (responsiveness.Feel),
    so "nothing to fix" is not said of a sweep whose users noticed.
    ``scaleout``: what extra copies really added (scaleout.py), used for the
    gains and counts instead of a full copy each."""
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
               "hidden": "HIDDEN LIMIT — not CPU",
               "ok": "ok"}[x.verdict]
        s = "∞" if x.service_time_ms == math.inf else f"{x.service_time_ms:.0f}"
        if x.lower_bound:
            load, fix, s = "≤" + load, "ok — limit not reached", "≤" + s
        lines.append(f"{n:<18}{s:>12}{load:>12}   {fix}")

    opt = [n for n in spec["services"] if d[n].verdict == "optimize"]
    scale = [n for n in spec["services"] if d[n].verdict == "scale"]
    hidden = [n for n in spec["services"] if d[n].verdict == "hidden"]
    lines.append("")
    if hidden:
        lines += _hidden_report(dr, spec, scaleout)
    for n in opt:
        x = d[n]
        gain, _, measured, _ = copy_gain(x, dr, scaleout)
        lines.append(f"  {n}: overloaded AND heavy (~{x.service_time_ms:.0f} "
                     "ms/request). Optimize the request")
        lines.append("    (pagination / caching / fewer rows / an index). Adding a "
                     "replica buys only")
        lines.append(f"    ~{gain:.0f} req/s{' (measured)' if measured else ''}, "
                     "so scaling is the wrong lever here.")
    for n in scale:
        x = d[n]
        gain, count, measured, eff = copy_gain(x, dr, scaleout)
        need = f"~{count} replicas" if count else "more replicas"
        lines.append(f"  {n}: overloaded but cheap (~{x.service_time_ms:.0f} "
                     "ms/request). Add replicas —")
        if measured:
            lines.append(f"    each added replica adds ~{gain:.0f} req/s "
                         f"(measured, {eff * 100:.0f}% of a full "
                         f"one); sizing suggests {need}.")
        else:
            lines.append(f"    scaling is efficient (~{gain:.0f} req/s "
                         f"each); sizing suggests {need}.")
    if not opt and not scale and not hidden and felt is not None:
        lines.append("  No overloaded services, so no scale-or-optimize verdict; "
                     f"but users felt {felt.vus} VUs")
        lines.append("    (see 'Highest comfortable load'). No service is full, "
                     "so more replicas")
        lines.append("    are not the obvious fix: find where the time goes first "
                     "(hotspots).")
    elif not opt and not scale and not hidden:
        lines.append("  No overloaded services — nothing to scale or optimize "
                     "right now.")
    unreached = [n for n in spec["services"] if d[n].lower_bound]
    if unreached:
        lines.append(f"  ≤ = upper bound: {', '.join(unreached)} kept pace with "
                     "the users at every level,")
        lines.append("    so the sweep never reached the limit (see 'Supported "
                     "load').")
    return "\n".join(lines) + "\n"


def diagnosis_explain(dr: DiagnoseResult, spec, felt=None, scaleout=None) -> str:
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
    hidden = [n for n in spec["services"] if d[n].verdict == "hidden"]
    if hidden:
        out += _hidden_explain(dr, scaleout)
    for n in opt:
        x = d[n]
        gain, _, _, _ = copy_gain(x, dr, scaleout)
        out.append(f"  • {n}: each request is heavy (~{x.service_time_ms:.0f} ms "
                   "of work).")
        out.append(f"    More copies barely help (~{gain:.0f} more "
                   "requests/second each).")
        out.append("    Make the request lighter first — fewer rows (pagination), "
                   "caching, or an index.")
        out.append("")
    for n in scale:
        x = d[n]
        _, count, measured, _ = copy_gain(x, dr, scaleout)
        need = f"about {count}" if count else "a few more"
        out.append(f"  • {n}: each request is quick (~{x.service_time_ms:.0f} ms) "
                   "but it's swamped.")
        out.append(f"    Just add copies ({need}) and it'll keep up"
                   + (", counting what an extra copy really added." if measured
                      else "."))
        out.append("")
    if not opt and not scale and not hidden and felt is not None:
        out.append("  No part is full, so by these numbers more servers are not "
                   "the obvious")
        out.append(f"  fix; yet the people using it noticed at {felt.vus} users "
                   "(see above).")
        out.append("  Find where the time goes first: the busiest requests are "
                   "below.")
    elif not opt and not scale and not hidden:
        out.append("  Nothing is overloaded — no servers to add, no code to fix "
                   "right now.")
    elif any(d[n].verdict == "ok" for n in spec["services"]):
        out.append("  Everything else has room to spare.")
    unreached = [n for n in spec["services"] if d[n].lower_bound]
    if unreached and felt is None:
        out.append(f"  ({', '.join(unreached)} never ran out of room in these "
                   "tests, so there is")
        out.append("  nothing to add yet; a test with more users would show "
                   "where it stops.)")
    out.append("")
    return "\n".join(out)


def _hidden_report(dr: DiagnoseResult, spec, scaleout=None) -> list:
    h = dr.hidden
    share = h.host_used / h.host_cores
    per = ", ".join(f"{n} {c:.2f} ({c / max(r, 1):.2f} per replica)"
                    for n, (c, r) in sorted(h.cpu.items(), key=lambda kv: -kv[1][0]))
    hid = [x for x in dr.diagnoses.values() if x.verdict == "hidden"]
    sizing = sorted({x.replicas_needed for x in hid if x.replicas_needed})
    measured = [copy_gain(x, dr, scaleout) for x in hid]
    measured = [m for m in measured if m[2]]
    lines = [
        f"  Throughput stopped growing at {h.vus} VUs (~{h.peak_rps:.0f} req/s) "
        f"while the host used",
        f"  {h.host_used:.1f} of {h.host_cores:.0f} cores ({share * 100:.0f}%): "
        "the limit is not CPU. Something caps how",
        "  much work runs at once: a connection pool (to a database or another "
        "service),",
        "  a worker or thread limit, or a lock.",
        f"  CPU at {h.vus} VUs: {per}.",
    ]
    if h.fall is not None:
        vus, rps, busiest, cores = h.fall
        line = f"  At {vus} VUs throughput fell to ~{rps:.0f} req/s"
        if cores < h.cpu[busiest][0]:
            line += (f" while {busiest}'s CPU fell to {cores:.2f} cores: work "
                     "was refused, not done.")
        else:
            line += "."
        lines.append(line)
    logs = dr.logs
    if logs is not None and logs.suspect is not None:
        s = logs.suspect
        clean = logs.clean_below
        lines += [
            f"  The logs name it: {KINDS[s.kind][0]}.",
            f"  {s.total} lines of {s.key} in {s.component}'s logs, first at "
            f"{s.first_vus} VUs"
            + (" (none at lighter levels)." if clean else "."),
        ]
    elif logs is not None:
        lines += [
            "  Every service levels off together in a closed loop, so this does "
            "not say which",
            "  one holds the limit, and the logs read show no known \"ran out\" "
            "message ("
            + ("only timeouts or refused connections" if logs.signals
               else "none at all") + "):",
            "  find where requests wait or fail once responses degrade (the "
            "slowest calls, or its own errors)"
            + (f" (not read: {', '.join(logs.unread)})." if logs.unread
               else "."),
        ]
    else:
        lines += [
            "  Every service levels off together in a closed loop, so this does "
            "not say which",
            "  one holds the limit; the logs of the service whose errors start "
            "there usually",
            "  name it (`python3 -m analyzer.logscan` reads them for known "
            "messages).",
        ]
    if measured:
        gain, _, _, eff = measured[0]
        counts = sorted({c for _, c, _, _ in measured if c})
        lines += [
            "  Replicas help only if the limit is per replica (each brings its "
            "own pool or",
            f"  workers). Measured against the other sweep: each added replica "
            f"added ~{gain:.0f} req/s,",
            f"  {eff * 100:.0f}% of a full one, so it is partly per "
            "replica"
            + (f"; sizing with that: ~{', '.join(map(str, counts))} replicas."
               if counts else "."),
            "",
        ]
        return lines
    lines += [
        "  Replicas help only if the limit is per replica (each brings its own "
        "pool or",
        "  workers)" + (f"; the plateau sizing (~{', '.join(map(str, sizing))} "
                        "replicas) assumes exactly that." if sizing else "."),
        "",
    ]
    return lines


def _hidden_explain(dr: DiagnoseResult, scaleout=None) -> list:
    h = dr.hidden
    out = [
        f"  • The system stopped taking on more work at about {h.vus} users, but "
        "not because",
        f"    the machine ran out of power: it used {h.host_used:.0f} of its "
        f"{h.host_cores:.0f} cores. Something inside",
        "    limits how much runs at once: usually a pool of database connections, "
        "a cap on",
    ]
    s = dr.logs.suspect if dr.logs is not None else None
    if s is not None:
        out += ["    workers or threads, or a lock. Here the logs say which: "
                f"{KINDS[s.kind][0]}",
                f"    ({s.component}, from {s.first_vus} users on)."]
        out[-2:] = textwrap.wrap(" ".join(p.strip() for p in out[-2:]), 82,
                                 initial_indent="    ", subsequent_indent="    ")
    elif dr.logs is not None:
        # scanned, nothing found: it may make requests wait rather than fail
        out += ["    workers or threads, or a lock. The logs show none of the usual "
                "\"ran out\"",
                "    errors, so it doesn't announce itself: look at where requests "
                "wait once",
                "    responses slow down."]
    else:
        out += ["    workers or threads, or a lock. The logs of the part that "
                "starts failing usually",
                "    name it."]
    if h.fall is not None:
        vus, rps, busiest, cores = h.fall
        if cores < h.cpu[busiest][0]:
            out += [f"    At {vus} users it got worse: it handled fewer requests "
                    f"(about {rps:.0f} a second)",
                    "    while using less of the machine, so it was turning work "
                    "away, not doing it."]
    hid = [x for x in dr.diagnoses.values() if x.verdict == "hidden"]
    measured = [m for m in (copy_gain(x, dr, scaleout) for x in hid) if m[2]]
    if measured:
        pct = measured[0][3] * 100
        out += [
            "    More copies help only if each brings its own share of that "
            "limit. The other test",
            f"    shows they partly do: each extra copy added {pct:.0f}% of what "
            "one copy handled.",
            "    Fixing the limit itself may still be cheaper.",
            "",
        ]
        return out
    out += [
        "    More copies help only if each copy brings its own share of that "
        "limit (for",
        "    example its own pool of connections); fixing the limit itself may "
        "be cheaper.",
        "",
    ]
    return out
