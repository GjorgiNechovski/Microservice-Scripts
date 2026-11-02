"""Plain-language rendering of the analysis -- for a non-technical reader.

Same data as ``run_all.report`` (the four analysis layers), retold without
jargon: no "nines", no "rho", no "MTTR". It is fully data-driven, so it explains
whatever system it's pointed at; nothing here is specific to one application.

    python3 -m analyzer.explain systems/finki-blogger.toml   # -> stdout
"""

from __future__ import annotations

import re
import sys

from analyzer.spec import analyze, load_or_default_measurements, load_system

_MIN_YR = 365 * 24 * 60


# ---------------------------------------------------------------------------
# phrasing helpers (numbers -> everyday language)
# ---------------------------------------------------------------------------

def _pretty(name: str) -> str:
    return name.replace("_", " ").replace("-", " ")


def _human_downtime(minutes: float) -> str:
    seconds = minutes * 60
    if seconds < 1:
        return "less than a second a year (effectively never)"
    if seconds < 90:
        return f"about {seconds:.0f} seconds a year"
    if minutes < 90:
        return f"about {minutes:.0f} minute{'s' if minutes >= 1.5 else ''} a year"
    hours = minutes / 60
    if hours < 48:
        return f"about {hours:.1f} hours a year"
    return f"about {hours / 24:.1f} days a year"


def _busy_phrase(rho: float, stable: bool) -> str:
    if not stable or rho >= 1:
        return "OVERLOADED — it cannot keep up with this much traffic"
    if rho < 0.25:
        return "very relaxed, with lots of spare capacity"
    if rho < 0.50:
        return "comfortable, with plenty of headroom"
    if rho < 0.75:
        return "working steadily"
    if rho < 0.90:
        return "getting busy — worth keeping an eye on"
    return "close to its limit"


def _recovery_phrase(seconds: float) -> str:
    if seconds < 1:
        return "under a second"
    if seconds < 90:
        return f"about {seconds:.0f} seconds"
    return f"about {seconds / 60:.1f} minutes"


# ---------------------------------------------------------------------------
# the narrative
# ---------------------------------------------------------------------------

def explain(results: dict, spec: dict, measurements: dict) -> str:
    out: list = []
    w = out.append

    w(f"PLAIN-LANGUAGE SUMMARY — {results['name']}")
    w("=" * 70)
    w("")
    w("This is the same information as the technical report, explained without")
    w("jargon. It answers two everyday questions about the system:")
    w("  1) Is it FAST enough to handle its visitors?")
    w("  2) Is it RELIABLE — how often is it working, and what could take it down?")
    w("")

    # -- headline, per operation ----------------------------------------
    w("THE HEADLINE")
    w("-" * 70)
    for op, t in results["top_events"].items():
        pct = t.availability * 100
        dt = (1.0 - t.availability) * _MIN_YR
        w(f'When someone does "{_pretty(op)}", the system works '
          f"{pct:.4f}% of the time —")
        w(f"  that is {_human_downtime(dt)} of downtime.")
        if t.spofs:
            # strip the instance suffix: "db#1" / "comment[1]" -> "db" / "comment"
            weak = ", ".join(sorted({re.split(r"[#\[]", s)[0] for s in t.spofs}))
            w(f"  Most likely to cause an outage: {weak}.")
        else:
            w("  Nothing here is a single weak point — everything has a backup.")
        if t.interventions:
            label, _, saved = t.interventions[0]
            if saved > 0:
                w(f"  Best single upgrade: {_pretty(label)} "
                  f"(prevents {_human_downtime(saved)} of failure-caused "
                  f"outage).")
        w("")

    # -- speed ----------------------------------------------------------
    w("IS IT FAST ENOUGH?")
    w("-" * 70)
    w("Picture each service as a checkout counter. 'Busy-ness' is how full it")
    w("is: 0% is idle; near 100% a queue forms and customers wait.")
    w("")
    busiest = None
    overloaded = []
    for name, s in results["services"].items():
        q = s.queue
        if not q.stable or q.rho >= 1:
            w(f"  - {name}: {_busy_phrase(q.rho, q.stable)}")
            overloaded.append(name)
        else:
            w(f"  - {name}: about {q.rho * 100:.0f}% busy — "
              f"{_busy_phrase(q.rho, q.stable)}")
        if busiest is None or q.rho > busiest[1]:
            busiest = (name, q.rho)
    w("")
    if overloaded:
        w(f"  ⚠ These are overloaded at the measured traffic: {', '.join(overloaded)}.")
        w("    They need more copies, or the traffic reduced.")
    elif busiest:
        w(f"  Everything is keeping up. The busiest is {busiest[0]} at "
          f"{busiest[1] * 100:.0f}% — still within comfort.")
    w("")

    # -- reliability ----------------------------------------------------
    w("IS IT RELIABLE?")
    w("-" * 70)
    w("'Availability' is the share of time something is working.")
    w("")
    redundant = [(n, s.replicas) for n, s in results["services"].items()
                 if s.replicas > 1]
    single_svc = [n for n, s in results["services"].items() if s.replicas <= 1]
    if redundant:
        w("  Good news: these services run more than one copy, so if one stops")
        w("  the others carry on — they almost never all fail at once:")
        for name, c in redundant:
            w(f"    - {name}: {c} copies")
        w("")
    if single_svc:
        w("  These services run only ONE copy, so they are weak points — if that")
        w("  single copy stops, anything that needs it stops too:")
        for name in single_svc:
            w(f"    - {name} (only 1 copy)")
        w("")
    infra = spec.get("infrastructure", [])
    if infra:
        w("  The shared parts also exist only ONCE, and are weak points for the")
        w("  same reason:")
        for name in infra:
            r = results["infrastructure"][name]
            dt = r.unavailability * _MIN_YR
            w(f"    - {name}: on its own, down {_human_downtime(dt)}")
        w("")
    w("  Having only one of something is the risk. Running a second copy of any")
    w("  single-copy part above removes it as a point of failure.")
    w("")

    # -- recovery -------------------------------------------------------
    w("HOW FAST DOES IT RECOVER?")
    w("-" * 70)
    w("We did not only calculate this — we actually stopped each part and timed")
    w("how long it took to come back:")
    w("")
    recover = []
    for name in spec["services"]:
        rate = measurements["services"][name]["repair_rate"]
        recover.append((name, 3600.0 / rate))
    for name in infra:
        rate = measurements["infrastructure"][name]["repair_rate"]
        recover.append((name, 3600.0 / rate))
    for name, secs in sorted(recover, key=lambda kv: kv[1], reverse=True):
        w(f"  - {name}: back in {_recovery_phrase(secs)}")
    w("")
    w("  This quick recovery is WHY the system stays reliable despite the single")
    w("  shared parts: even if one fails, it returns almost immediately.")
    w("")

    # -- what to fix ----------------------------------------------------
    w("WHAT TO FIX FIRST")
    w("-" * 70)
    w("Two separate problems can both say 'add a replica' — do not confuse them:")
    w("")
    if overloaded:
        w("  1) SPEED — URGENT. These services are overloaded at the measured")
        w(f"     traffic: {', '.join(overloaded)}.")
        w("     Work arrives faster than one copy can serve it, so queues and")
        w("     waiting times grow without bound. Extra copies (or making the")
        w("     service itself faster) are REQUIRED just to keep up. This is the")
        w("     fix that matters first; see IS IT FAST ENOUGH? and the capacity")
        w("     sizing in the technical report for how many copies.")
        w("")
        w("  2) RELIABILITY — the ranking below. It answers a DIFFERENT question:")
    else:
        w("  1) SPEED: nothing is overloaded at the measured traffic, so speed")
        w("     needs no fix right now.")
        w("")
        w("  2) RELIABILITY — the ranking below answers a different question:")
    w("     if a part crashes at the rate we assumed and recovers as fast as we")
    w("     measured, how much OUTAGE TIME per year would the change prevent?")
    w("     These amounts look tiny precisely because recovery is fast — they")
    w("     say nothing about how slow responses get under load.")
    w("")
    best_global = None
    for op, t in results["top_events"].items():
        if t.interventions and t.interventions[0][2] > 0:
            label, _, saved = t.interventions[0]
            w(f'  - For "{_pretty(op)}": {_pretty(label)} — prevents '
              f"{_human_downtime(saved)} of failure-caused outage.")
            if best_global is None or saved > best_global[1]:
                best_global = (label, saved)
    w("")

    # -- one-liner ------------------------------------------------------
    w("IN ONE SENTENCE")
    w("-" * 70)
    speed = "strained" if overloaded else "fast enough for its traffic"
    weak = "has a few single weak points" if any(
        results["top_events"][o].spofs for o in results["top_events"]) \
        else "has no single weak points"
    if overloaded:
        tail = f"; the urgent fix is more capacity for {', '.join(overloaded)}"
        if best_global:
            tail += (f" (for reliability, the best upgrade is "
                     f"{_pretty(best_global[0])})")
        tail += "."
    else:
        tail = (f"; the highest-impact single change is "
                f"{_pretty(best_global[0])}." if best_global else ".")
    w(f"{results['name']} is {speed} and {weak}{tail}")
    w("")
    return "\n".join(out)


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 1:
        print("usage: python3 -m analyzer.explain <system-spec.toml>",
              file=sys.stderr)
        return 2
    spec = load_system(argv[0])
    measurements, _ = load_or_default_measurements(argv[0], spec)
    print(explain(analyze(spec, measurements), spec, measurements))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
