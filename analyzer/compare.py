"""Compare a fresh analysis against the previous one for the same load level.

When a sweep re-measures a level that already has results on disk, the prior
``measured.toml`` IS the previous state -- re-deriving its analysis and diffing
it against the new one answers the only question that matters after a change:
did it get better, worse, or stay the same? Reported as hard numbers
(``comparison_report``) and in plain language (``comparison_explain``).

Every metric has a known "good direction"; a change smaller than a few percent
counts as "no change" so measurement noise doesn't read as real movement.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from analyzer.capacity import size_services
from analyzer.saturation import estimate_capacity
from analyzer.scaling import LevelResult
from analyzer.spec import analyze

_MIN_YR = 365 * 24 * 60
_EPS = 0.05          # relative change below this = "no change"


@dataclass
class Metric:
    name: str
    entity: str                 # service or operation
    prev: float | None
    curr: float | None
    higher_better: bool
    verdict: str = "same"       # improved | worse | same
    prec: int | None = None     # fixed decimals when the value needs them


@dataclass
class Comparison:
    vus: int
    services: list
    operations: list
    metrics: list = field(default_factory=list)
    tally: dict = field(default_factory=dict)
    overall: str = "no change"


def _score(prev, curr, higher_better, *, abs_eps=0.0, floor=None) -> str:
    """improved / worse / same, with dead-bands so noise isn't read as movement.

    ``abs_eps``  : an absolute change this small is "same" (kills near-zero noise
                   where a relative test alone is unstable, e.g. 0.0001 -> 0.0002).
    ``floor``    : if the metric is below this magnitude in BOTH runs it is "same"
                   -- a change that keeps a value far from the level that matters
                   (e.g. utilization well under target) is not a real regression.
    """
    if prev is None or curr is None:
        return "same"
    inf = math.inf
    if prev == inf and curr == inf:
        return "same"
    if prev == inf:                          # was unbounded, now finite
        return "improved" if not higher_better else "worse"
    if curr == inf:                          # was finite, now unbounded
        return "worse" if not higher_better else "improved"
    if floor is not None and max(abs(prev), abs(curr)) < floor:
        return "same"
    diff = curr - prev
    if abs(diff) <= abs_eps:
        return "same"
    denom = abs(prev) if prev != 0 else abs(curr)
    if denom == 0 or abs(diff) / denom < _EPS:
        return "same"
    return "improved" if ((curr > prev) == higher_better) else "worse"


def _m(name, entity, prev, curr, higher_better, *, prec=None,
       abs_eps=0.0, floor=None) -> Metric:
    return Metric(name, entity, prev, curr, higher_better,
                  _score(prev, curr, higher_better, abs_eps=abs_eps, floor=floor),
                  prec)


def _caps(spec, meas, vus, target):
    lr = LevelResult(vus, meas, analyze(spec, meas),
                     size_services(spec, meas, target))
    return estimate_capacity([lr], spec, target)


def compare(spec, prev_meas, curr_meas, vus, target) -> Comparison:
    ap, ac = analyze(spec, prev_meas), analyze(spec, curr_meas)
    cp, cc = _caps(spec, prev_meas, vus, target), _caps(spec, curr_meas, vus, target)

    # One metric per fact -- capacity/replica is redundant with the ceiling
    # (ceiling = replicas x capacity), and availability is redundant with
    # downtime -- so each is counted ONCE, never double-tallied.
    metrics = []
    for name in spec["services"]:
        qp, qc = ap["services"][name].queue, ac["services"][name].queue
        metrics += [
            _m("throughput ceiling (req/s)", name,
               cp[name].ceiling_throughput, cc[name].ceiling_throughput, True,
               abs_eps=0.5),
            # utilization only "moves" once it's within reach of the target;
            # a sliver of extra load on a near-idle service isn't a regression.
            _m("utilization", name, qp.rho, qc.rho, False, prec=2,
               floor=0.5 * target),
            _m("response time (ms)", name,
               qp.W * 1000 if qp.stable else math.inf,
               qc.W * 1000 if qc.stable else math.inf, False, abs_eps=1.0),
        ]

    ops = list(ap["top_events"])
    for op in ops:
        tp, tc = ap["top_events"][op], ac["top_events"][op]
        metrics.append(_m("downtime (min/yr)", op,
                          (1 - tp.availability) * _MIN_YR,
                          (1 - tc.availability) * _MIN_YR, False, abs_eps=0.5))

    tally = {"improved": 0, "worse": 0, "same": 0}
    for m in metrics:
        tally[m.verdict] += 1
    if tally["improved"] == 0 and tally["worse"] == 0:
        overall = "no change"
    elif tally["worse"] == 0:
        overall = "improved"
    elif tally["improved"] == 0:
        overall = "worse"
    else:
        overall = "mixed"

    return Comparison(vus, list(spec["services"]), ops, metrics, tally, overall)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def _fv(v, prec=None, inf_label="OVER") -> str:
    if v is None:
        return "n/a"
    if v == math.inf:
        return inf_label
    if prec is not None:
        return f"{v:.{prec}f}"
    a = abs(v)
    if a >= 100:
        return f"{v:.0f}"
    if a >= 10:
        return f"{v:.1f}"
    return f"{v:.2f}"


_SYM = {"improved": "✓ improved", "worse": "✗ worse", "same": "= no change"}


def comparison_report(comp: Comparison, prev_when: str | None = None) -> str:
    t = comp.tally
    lines = [f"\nCOMPARISON — load {comp.vus} VUs  (previous → current)"]
    if prev_when:
        lines.append(f"previous run: {prev_when}")
    lines += [
        "=" * 74,
        f"OVERALL: {comp.overall.upper()}   "
        f"({t['improved']} better, {t['worse']} worse, {t['same']} unchanged)",
    ]

    def _block(entity):
        rows = [m for m in comp.metrics if m.entity == entity]
        lines.append(f"\n{entity}")
        for m in rows:
            lines.append(f"  {m.name:<26}{_fv(m.prev, m.prec):>12}"
                         f"{_fv(m.curr, m.prec):>12}   {_SYM[m.verdict]}")

    lines.append("\n" + "-" * 74)
    lines.append("Performance (per service)")
    lines.append("-" * 74)
    for name in comp.services:
        _block(name)
    lines.append("\n" + "-" * 74)
    lines.append("Reliability (per operation)")
    lines.append("-" * 74)
    for op in comp.operations:
        _block(op)
    return "\n".join(lines) + "\n"


# every scored metric can be narrated, so a "mixed"/"worse" headline is never
# left unexplained
_PLAIN = {"throughput ceiling (req/s)", "response time (ms)",
          "downtime (min/yr)", "utilization"}


def _phrase(m: Metric) -> str:
    e, better = m.entity, m.verdict == "improved"
    if m.name.startswith("throughput ceiling"):
        dirn = "more" if better else "less"
        return (f"{e} can handle {dirn} traffic overall "
                f"(about {m.prev:.0f} → {m.curr:.0f} requests/second)")
    if m.name.startswith("response time"):
        pv = "over the limit" if m.prev == math.inf else f"{m.prev:.0f} ms"
        cv = "over the limit" if m.curr == math.inf else f"{m.curr:.0f} ms"
        return f"{e} responds {'faster' if better else 'slower'} ({pv} → {cv})"
    if m.name.startswith("downtime"):
        return (f"\"{e}\" downtime {'dropped' if better else 'rose'} "
                f"from {m.prev:.0f} to {m.curr:.0f} minutes a year")
    if m.name == "utilization":
        a = "overloaded" if m.prev >= 1 else f"{m.prev * 100:.0f}% used"
        b = "overloaded" if m.curr >= 1 else f"{m.curr * 100:.0f}% used"
        return f"{e} went from {a} to {b}"
    return f"{e}: {m.name}"


def comparison_explain(comp: Comparison, prev_when: str | None = None) -> str:
    head = {
        "improved": "YES — it improved since last time. 🎉",
        "worse": "NO — it got worse since last time.",
        "no change": "About the same as last time — no real change.",
        "mixed": "Mixed — some things improved, some got worse.",
    }[comp.overall]

    out = [f"DID IT GET BETTER? — {comp.vus} users (vs your last run)", "=" * 66]
    if prev_when:
        out.append(f"comparing against your run from {prev_when}")
    out += ["", f"Short answer: {head}", ""]

    improved = [m for m in comp.metrics
                if m.verdict == "improved" and m.name in _PLAIN]
    worse = [m for m in comp.metrics
             if m.verdict == "worse" and m.name in _PLAIN]

    if improved:
        out.append("What got better:")
        for m in improved:
            out.append(f"  • {_phrase(m)}")
        out.append("")
    if worse:
        out.append("What got worse:")
        for m in worse:
            out.append(f"  • {_phrase(m)}")
        out.append("")
    if not improved and not worse:
        out.append("Nothing moved much — this change didn't shift the numbers,")
        out.append("for better or worse.")
        out.append("")
    return "\n".join(out)
