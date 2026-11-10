"""Did the users feel the load? Each load level judged by what k6 measured.

The queueing layers judge a service by throughput and service rate. In a closed
loop with a think-time that is blind to slowness: throughput keeps pace with
the users until responses take a noticeable part of the pause (about 0.75 s at
5 s), long after a person would have noticed. So the analysis can report that
a level "kept up" while its response times tripled and requests started to
fail. This module reads what the load generator itself recorded at each level
-- response times and failed requests, end to end -- and gives two separate
verdicts, because they are different problems:

  errors   fine, or FAILING: more requests failed than ``max_error_rate``
  speed    fine, or SLOWING: the 95th-percentile response time crossed its
           line -- a fixed budget (``max_p95_ms``) when the spec sets one;
           otherwise ``p95_factor`` times the lightest level's p95, and at
           least ``p95_floor_ms``. The slowdown is reported as "N times as
           slow as the lightest level" either way.

So a level can read "no requests failed, but 4.8 times as slow". Either
verdict going wrong makes the level strained, which caps the sweep's highest
comfortable load. These are client-side numbers for the whole system: they say
THAT users felt it, not which service caused it (the per-service reports and
hotspots.txt narrow that down). Every threshold is tunable in the spec's
optional ``[responsiveness]`` table; nothing here is specific to one
application.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from analyzer.validity import parse_k6_summary

_DEFAULTS = {"p95_factor": 3.0, "p95_floor_ms": 100.0, "max_error_rate": 0.001}


@dataclass
class Feel:
    """How one load level felt to its users: errors and speed, judged apart."""

    vus: int | None
    status: str                       # "ok" | "slowing" | "failing" | "unknown"
    avg_ms: float | None = None
    p95_ms: float | None = None
    failed: float | None = None       # share of requests, 0..1
    baseline_p95_ms: float | None = None   # the lightest level's p95
    limit_p95_ms: float | None = None      # the line p95 was judged against
    max_error_rate: float = _DEFAULTS["max_error_rate"]
    slow: bool = False                # speed verdict
    failing: bool = False             # errors verdict
    reasons: list = field(default_factory=list)

    @property
    def strained(self) -> bool:
        return self.slow or self.failing

    @property
    def slowdown(self) -> float | None:
        """p95 as a multiple of the lightest level's (1.0 at that level)."""
        if self.p95_ms is None or not self.baseline_p95_ms:
            return None
        return self.p95_ms / self.baseline_p95_ms


def thresholds(spec: dict) -> dict:
    """The spec's ``[responsiveness]`` settings over the defaults."""
    cfg = dict(_DEFAULTS)
    cfg.update(spec.get("responsiveness", {}))
    return cfg


def read_k6(path: str):
    """The :class:`~analyzer.validity.K6Summary` saved at ``path``, or None."""
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as fh:
        return parse_k6_summary(fh.read())


def judge_one(spec: dict, summary, vus: int | None = None,
              baseline_p95_ms: float | None = None) -> Feel:
    """Judge one level; without a baseline only the fixed limits apply."""
    if summary is None or (summary.p95_ms is None and summary.failed is None):
        return Feel(vus, "unknown")
    cfg = thresholds(spec)
    feel = Feel(vus, "ok", summary.avg_ms, summary.p95_ms, summary.failed,
                baseline_p95_ms, max_error_rate=float(cfg["max_error_rate"]))

    if cfg.get("max_p95_ms") is not None:
        feel.limit_p95_ms = float(cfg["max_p95_ms"])
    elif baseline_p95_ms:
        feel.limit_p95_ms = max(float(cfg["p95_floor_ms"]),
                                float(cfg["p95_factor"]) * baseline_p95_ms)
    feel.slow = (feel.p95_ms is not None and feel.limit_p95_ms is not None
                 and feel.p95_ms >= feel.limit_p95_ms)
    feel.failing = feel.failed is not None and feel.failed > feel.max_error_rate

    if feel.slow:
        feel.reasons.append(_speed_tech(feel))
    if feel.failing:
        feel.reasons.append(_errors_tech(feel))
    feel.status = "failing" if feel.failing else ("slowing" if feel.slow else "ok")
    return feel


def judge_levels(spec: dict, summaries: dict) -> dict:
    """``{vus: Feel}`` for a sweep's ``{vus: K6Summary or None}``.

    The baseline is the p95 of the lightest level that has one: the system
    at its least loaded, as close to its unloaded speed as the sweep shows.
    """
    base = next((summaries[v].p95_ms for v in sorted(summaries)
                 if summaries[v] is not None and summaries[v].p95_ms), None)
    return {v: judge_one(spec, summaries[v], v, base) for v in sorted(summaries)}


def judge_sweep_dir(spec: dict, out: str, levels) -> dict:
    """:func:`judge_levels` over the ``load-<N>/k6-output.txt`` files in ``out``."""
    return judge_levels(spec, {v: read_k6(os.path.join(out, f"load-{v}",
                                                       "k6-output.txt"))
                               for v in levels})


def comfortable(feels: dict) -> int | None:
    """Highest level at which users noticed nothing, counting up from the
    lightest and stopping at the first strained one. None if even the lightest
    was strained, or if no level could be judged."""
    best = None
    for v in sorted(feels):
        f = feels[v]
        if f.status == "unknown":
            continue
        if f.strained:
            return best
        best = v
    return best


def first_strained(feels: dict) -> Feel | None:
    return next((feels[v] for v in sorted(feels) if feels[v].strained), None)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def _ms(v):
    return "--" if v is None else f"{v:.0f}" if v >= 10 else f"{v:.1f}"


def _pct(share):
    return f"{share * 100:.2f}%"


def _speed_tech(f: Feel) -> str:
    if f.slowdown is not None:
        return (f"{f.slowdown:.1f}x slower (p95 {_ms(f.p95_ms)} ms vs "
                f"{_ms(f.baseline_p95_ms)} ms at the lightest level; limit "
                f"{_ms(f.limit_p95_ms)} ms)")
    return f"p95 {_ms(f.p95_ms)} ms (limit {_ms(f.limit_p95_ms)} ms)"


def _errors_tech(f: Feel) -> str:
    return f"{_pct(f.failed)} of requests failed (limit {_pct(f.max_error_rate)})"


def tech_summary(f: Feel) -> str:
    """Both verdicts in one line: what went wrong, and that the other held."""
    parts = list(f.reasons)
    if f.slow and not f.failing and f.failed is not None:
        parts.append("no failed requests" if f.failed == 0
                     else f"errors fine ({_pct(f.failed)})")
    if f.failing and not f.slow and f.p95_ms is not None:
        rel = (f", {f.slowdown:.1f}x the lightest level"
               if f.slowdown is not None else "")
        lim = (f"; limit {_ms(f.limit_p95_ms)} ms" if f.limit_p95_ms is not None
               else "")
        parts.append(f"speed fine (p95 {_ms(f.p95_ms)} ms{rel}{lim})")
    return "; ".join(parts)


def plain_strain(f: Feel) -> str:
    """The strain in plain words, saying which part held, e.g. 'responses
    were 4.8 times as slow as at the lightest test (...), though no requests
    failed'."""
    speed = None
    if f.p95_ms is not None:
        if f.slowdown is not None:
            speed = (f"{f.slowdown:.1f} times as slow as at the lightest test "
                     f"(95 of every 100 responses within {_ms(f.p95_ms)} ms, "
                     f"against {_ms(f.baseline_p95_ms)} ms)")
        else:
            speed = f"95 of every 100 responses within {_ms(f.p95_ms)} ms"
    errors = None
    if f.failed is not None:
        errors = ("no requests failed" if f.failed == 0
                  else f"{_pct(f.failed)} of requests failed")
    if f.slow and f.failing:
        return f"responses were {speed} and {errors}"
    if f.slow:
        return f"responses were {speed}" + (f", though {errors}" if errors else "")
    if f.failing:
        if speed is None:
            return errors
        line = (f"; the line is {_ms(f.limit_p95_ms)} ms"
                if f.limit_p95_ms is not None else "")
        return f"{errors}, though the speed stayed fine ({speed}{line})"
    return f"responses were {speed}" if speed else "nothing stood out"


def level_report(f: Feel | None) -> str:
    """Technical block for one level's report.txt: errors and speed apart."""
    if f is None or f.status == "unknown":
        return ""
    lines = ["", "=" * 74,
             "Responsiveness -- what the load generator measured (end to end)",
             "=" * 74]
    if f.failed is not None:
        lines.append(f"  errors   {_pct(f.failed):>8} failed  (limit "
                     f"{_pct(f.max_error_rate)})   -> "
                     f"{'FAILING' if f.failing else 'fine'}")
    if f.p95_ms is not None:
        rel = (f", {f.slowdown:.1f}x the lightest level's {_ms(f.baseline_p95_ms)} ms"
               if f.slowdown is not None else "")
        lim = f"limit {_ms(f.limit_p95_ms)} ms" if f.limit_p95_ms else "no limit"
        lines.append(f"  speed    p95 {_ms(f.p95_ms)} ms{rel}  ({lim})   -> "
                     f"{'SLOWING' if f.slow else 'fine'}")
    if f.avg_ms is not None:
        lines.append(f"  (mean response {_ms(f.avg_ms)} ms)")
    return "\n".join(lines) + "\n"


def level_explain(f: Feel | None, overloaded=None) -> list:
    """Plain-language verdicts for one level's report-explained.txt: whether
    a part is overloaded (``overloaded``: the names from the capacity math, or
    a finished answer as a string; None to leave the line out), whether
    requests failed, and how slow the responses were -- three separate
    questions, three answers."""
    if f is None or f.status == "unknown":
        return []
    pad = " " * 22
    out = ["  In short, at this load (the last two measured by the load test):"]
    if isinstance(overloaded, str):
        out.append(f"    Overloaded?       {overloaded}")
    elif overloaded is not None:
        out.append("    Overloaded?       "
                   + (f"YES: {', '.join(overloaded)}" if overloaded
                      else "no, every part kept up with the traffic"))
    if f.failed is not None:
        if f.failing:
            out.append(f"    Failed requests?  {_pct(f.failed)}: more than the "
                       f"{_pct(f.max_error_rate)} that counts as fine")
        elif f.failed == 0:
            out.append("    Failed requests?  none, so that part is fine")
        else:
            out.append(f"    Failed requests?  {_pct(f.failed)}: within the "
                       f"{_pct(f.max_error_rate)} that counts as fine")
    if f.p95_ms is not None:
        within = (f"95 of every 100 responses came back within "
                  f"{_ms(f.p95_ms)} ms")
        if f.slowdown is not None and f.baseline_p95_ms == f.p95_ms:
            out.append(f"    Speed?            {within};")
            out.append(f"{pad}this lightest test is the yardstick for the rest")
        elif f.slowdown is not None:
            out.append(f"    Speed?            {f.slowdown:.1f} times as slow as at "
                       "the lightest test:")
            out.append(f"{pad}{within}, against "
                       f"{_ms(f.baseline_p95_ms)} ms")
        else:
            out.append(f"    Speed?            {within}")
        if f.slow:
            out.append(f"{pad}Too slow: the line is {_ms(f.limit_p95_ms)} ms.")
        elif f.limit_p95_ms is not None and f.baseline_p95_ms != f.p95_ms:
            out.append(f"{pad}Still fine: the line is {_ms(f.limit_p95_ms)} ms.")
    return out


def sweep_table(spec, levels) -> list:
    """Technical table for the scaling report; empty if no level was judged."""
    feels = {lv.vus: lv.feel for lv in levels if getattr(lv, "feel", None)}
    if not any(f.status != "unknown" for f in feels.values()):
        return []
    cfg = thresholds(spec)
    rule = (f"p95 >= {float(cfg['max_p95_ms']):.0f} ms" if cfg.get("max_p95_ms")
            is not None else f"p95 >= max({float(cfg['p95_floor_ms']):.0f} ms, "
            f"{float(cfg['p95_factor']):g}x the lightest level's)")
    lines = [f"{'VUs':<8}{'mean ms':>9}{'p95 ms':>9}{'x lightest':>12}"
             f"{'failed':>9}{'errors':>9}{'speed':>9}"]
    for v in sorted(feels):
        f = feels[v]
        if f.status == "unknown":
            lines.append(f"{v:<8}{'(no k6 summary)':>30}")
            continue
        x = "--" if f.slowdown is None else f"{f.slowdown:.1f}"
        fail = "--" if f.failed is None else _pct(f.failed)
        lines.append(f"{v:<8}{_ms(f.avg_ms):>9}{_ms(f.p95_ms):>9}{x:>12}"
                     f"{fail:>9}{'FAILING' if f.failing else 'fine':>9}"
                     f"{'SLOWING' if f.slow else 'fine':>9}")
    lines.append(f"  (speed: SLOWING when {rule}; errors: FAILING when more "
                 f"than {float(cfg['max_error_rate']) * 100:.2f}% failed)")
    return lines
