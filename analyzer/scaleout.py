"""What extra copies really added -- measured from two sweeps of one system.

Every replica count elsewhere assumes each copy adds a full copy's capacity: a
service that peaked at P req/s on c copies gets P/c more per copy. Copies on
one host share its CPU, memory and disk, and copies behind one database share
that database, so each new copy usually adds less. Two load sweeps of the same
system that differ only in copy counts measure what it really added: the most
each sweep served (its throughput peak, diagnose.sweep_position) ::

    gain per copy = (peak_more - peak_fewer) / copies added
    efficiency    = gain per copy / (peak_fewer / copies in the smaller run)

and the recipe then counts copies with the measured gain instead of a full
copy's. Nothing here is app-specific.

A peak is a capacity only if its sweep levelled off. If the larger run never
did, its peak is a floor, so the gain is "at least" that and the copies needed
"at most" what follows; if the smaller run never did, nothing can be measured.
The two sweeps must drive the same traffic per user (same script, think-time
and data; CLAUDE.md "Comparing two runs"), which is checked at their lightest
shared level.
"""

from __future__ import annotations

import glob
import math
import os
import statistics
import textwrap
import tomllib
from dataclasses import dataclass, field

from analyzer.diagnose import _entry_rate, sweep_position

# Requests per user at the lightest shared level may differ this much between
# the two sweeps before they count as different traffic (healthy levels agreed
# within 2-6% across separate runs in testing).
_SAME_LOAD = 0.15


@dataclass
class SweepPeak:
    """The most one sweep served, and on how many copies."""

    label: str
    replicas: dict                # service -> copies
    peak_vus: int
    peak_rps: float               # the entry service's arrival rate at the peak
    levelled_off: bool            # a plateau was seen; else peak_rps is a floor
    per_user: dict                # vus -> req/s per user, every level


@dataclass
class CopyGain:
    """What one scaled service's extra copies added."""

    service: str
    fewer: int
    more: int
    gain_per_copy: float          # req/s each added copy added
    full_copy: float              # req/s one copy served in the smaller run

    @property
    def efficiency(self) -> float:
        return self.gain_per_copy / self.full_copy if self.full_copy > 0 else 0.0


@dataclass
class ScaleOut:
    fewer: SweepPeak
    more: SweepPeak
    current: SweepPeak            # the sweep this report is about
    gains: dict                   # service -> CopyGain, for services that changed
    bound: str                    # "measured" | "at least" | "unknown"
    notes: list = field(default_factory=list)

    @property
    def gain_rps(self) -> float:
        return self.more.peak_rps - self.fewer.peak_rps

    @property
    def usable(self) -> bool:
        """The gain can size copies: it is measured (or a floor) and positive."""
        return self.bound != "unknown" and bool(self.gains) and self.gain_rps > 0

    def copies_for(self, service: str, load_rps: float, target: float):
        """Copies of ``service`` to serve ``load_rps`` at ``target`` utilization,
        from the measured gain per copy; None when it can't say (not scaled
        between the sweeps, or no usable gain). With a gain that is only a
        floor the count is an upper bound (see :attr:`bound`)."""
        g = self.gains.get(service)
        if g is None or not self.usable or g.gain_per_copy <= 0:
            return None
        have = self.current.replicas[service]
        missing = load_rps / target - self.current.peak_rps
        if missing <= 0:
            return have
        return have + math.ceil(missing / g.gain_per_copy - 1e-9)


# ---------------------------------------------------------------------------
# building
# ---------------------------------------------------------------------------

def load_sweep(spec, out_dir):
    """A saved sweep's levels as minimal scaling.LevelResults (measurements and
    analysis only), from every ``load-<N>/measured.toml`` under ``out_dir``."""
    from analyzer.scaling import LevelResult
    from analyzer.spec import analyze

    levels = []
    for mpath in glob.glob(os.path.join(out_dir, "load-*", "measured.toml")):
        name = os.path.basename(os.path.dirname(mpath))[len("load-"):]
        if not name.isdigit():
            continue
        with open(mpath, "rb") as fh:
            meas = tomllib.load(fh)
        levels.append(LevelResult(int(name), meas, analyze(spec, meas), {}))
    if len(levels) < 2:
        raise ValueError(f"{out_dir}: needs a saved sweep of two or more levels "
                         "(load-<N>/measured.toml)")
    return sorted(levels, key=lambda lv: lv.vus)


def sweep_peak(spec, levels, target, label) -> SweepPeak:
    """The :class:`SweepPeak` of one sweep."""
    pos = sweep_position(levels, spec, target)
    top = max(levels, key=lambda lv: _entry_rate(lv, spec))
    # the copy count every level agrees on (one missed cAdvisor scrape can
    # misread a single level)
    replicas = {n: statistics.mode(int(lv.measurements["services"][n]["replicas"])
                                   for lv in levels)
                for n in spec["services"]}
    per_user = {lv.vus: _entry_rate(lv, spec) / lv.vus
                for lv in levels if lv.vus > 0}
    return SweepPeak(label, replicas, top.vus, _entry_rate(top, spec),
                     pos is not None, per_user)


def measure_scaleout(spec, current_levels, other_levels, target,
                     current_label, other_label) -> ScaleOut | None:
    """Compare this sweep with another of the same system on different copy
    counts. None when no service's copy count differs between them."""
    cur = sweep_peak(spec, current_levels, target, current_label)
    oth = sweep_peak(spec, other_levels, target, other_label)
    changed = {n: (cur.replicas[n], oth.replicas[n]) for n in spec["services"]
               if cur.replicas[n] != oth.replicas[n]}
    if not changed:
        return None
    up = [n for n, (a, b) in changed.items() if b > a]
    down = [n for n in changed if n not in up]
    notes = []
    if up and down:
        notes.append(f"copies went up for {', '.join(up)} but down for "
                     f"{', '.join(down)}: the gain can't be credited to either")
        fewer, more = cur, oth
        return ScaleOut(fewer, more, cur, {}, "unknown", notes)
    fewer, more = (cur, oth) if up else (oth, cur)

    if not fewer.levelled_off:
        bound = "unknown"
        notes.append(f"{fewer.label} never levelled off, so its peak is only a "
                     "floor and the gain can't be measured; re-run it with "
                     "more users")
    elif not more.levelled_off:
        bound = "at least"
        notes.append(f"{more.label} never levelled off: its peak is a floor, so "
                     "the gain is at least this and the copy counts are at most")
    else:
        bound = "measured"

    gains = {}
    for n in changed:
        a, b = fewer.replicas[n], more.replicas[n]
        gains[n] = CopyGain(n, a, b, (more.peak_rps - fewer.peak_rps) / (b - a),
                            fewer.peak_rps / a)
    if len({round(g.efficiency, 6) for g in gains.values()}) > 1:
        notes.append("the services changed by different numbers of copies; "
                     "the gain is the whole system's, so each one's share is "
                     "a guess")
    if bound != "unknown" and more.peak_rps <= fewer.peak_rps:
        notes.append("the run with more copies served no more: the limit is "
                     "not per copy, so adding copies does not help")
    elif any(g.efficiency > 1.05 for g in gains.values()):
        notes.append("each copy added more than a full copy: noise, or the "
                     "smaller run was held back by something else")

    shared = sorted(set(fewer.per_user) & set(more.per_user))
    if shared:
        v = shared[0]
        a, b = fewer.per_user[v], more.per_user[v]
        if max(a, b) > 0 and abs(a - b) / max(a, b) > _SAME_LOAD:
            notes.append(f"the two sweeps did not drive the same traffic per "
                         f"user ({a:.3f} vs {b:.3f} req/s per user at {v} "
                         "VUs): a different script, think-time or data makes "
                         "this comparison unreliable")
    else:
        notes.append("the two sweeps share no load level, so it can't be "
                     "checked that they drove the same traffic per user")
    return ScaleOut(fewer, more, cur, gains, bound, notes)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def _copies(peak: SweepPeak, names) -> str:
    return ", ".join(f"{n}={peak.replicas[n]}" for n in names)


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def scaleout_report(so: ScaleOut, spec, host_share=None) -> str:
    """Technical section for the scaling report. ``host_share``: the share of
    the host's cores the larger run used at its peak, when known."""
    names = list(spec["services"])
    lines = ["", "=" * 74,
             "Measured scale-out -- what the extra copies really added", "=" * 74]
    for tag, p in (("fewer copies", so.fewer), ("more copies", so.more)):
        here = "  (this sweep)" if p is so.current else ""
        peak = (f"peak ~{p.peak_rps:.0f} req/s at {p.peak_vus} VUs"
                if p.levelled_off else
                f"no plateau: ≥{p.peak_rps:.0f} req/s at {p.peak_vus} VUs")
        lines.append(f"  {tag:<13}: {p.label}{here}")
        lines.append(f"  {'':<13}  {_copies(p, names)}; {peak}")
    if so.gains and so.bound != "unknown":
        rel = "≥" if so.bound == "at least" else ""
        for g in so.gains.values():
            lines.append(f"  {g.service}: +{g.more - g.fewer} cop"
                         f"{'y' if g.more - g.fewer == 1 else 'ies'} added "
                         f"{rel}{g.gain_per_copy:.0f} req/s each = "
                         f"{rel}{_pct(g.efficiency)} of a full copy "
                         f"({g.full_copy:.0f} req/s)")
        if so.usable:
            lines.append("  The copy counts in this report use the measured gain "
                         "per added copy, not a")
            lines.append("  full copy's, and assume further copies add as much "
                         "as these did.")
            if host_share is not None:
                lines.append("  On one host they add less once its CPU fills; "
                             "at its peak this sweep used")
                lines.append(f"  {_pct(host_share)} of the host's cores.")
    for note in so.notes:
        lines.append(textwrap.fill(f"NOTE: {note}.", 74, initial_indent="  ",
                                   subsequent_indent="        "))
    return "\n".join(lines) + "\n"


def scaleout_explain(so: ScaleOut, spec) -> str:
    """Plain-language section for the scaling summary."""
    out = ["DID MORE COPIES HELP AS MUCH AS EXPECTED?", "-" * 70]
    scaled = list(so.gains) or [n for n in spec["services"]
                                if so.fewer.replicas[n] != so.more.replicas[n]]
    who = " and ".join(scaled) if len(scaled) <= 2 else ", ".join(scaled)
    if not so.gains or so.bound == "unknown":
        out.append(textwrap.fill(
            "We compared this test with another one that ran different "
            f"numbers of copies of {who}, but it can't tell what the extra "
            "copies added:", 72, initial_indent="  ", subsequent_indent="  "))
        for note in so.notes:
            out.append(textwrap.fill(f"{note}.", 72, initial_indent="    ",
                                     subsequent_indent="    "))
        out.append("")
        return "\n".join(out)
    g = next(iter(so.gains.values()))
    less = "at least " if so.bound == "at least" else ""
    counts = {(so.fewer.replicas[n], so.more.replicas[n]) for n in scaled}
    if len(counts) == 1:
        a, b = next(iter(counts))
        fewer_txt, more_txt = f"{a} cop{'y' if a == 1 else 'ies'}", f"{b}"
    else:
        fewer_txt, more_txt = "fewer copies", "more"
    text = (f"With {fewer_txt} of {who} the system served at most about "
            f"{so.fewer.peak_rps:.0f} requests a second; with {more_txt}, "
            f"{less}about {so.more.peak_rps:.0f}. ")
    if so.gain_rps <= 0:
        text += ("The extra copies added nothing: whatever limits it is shared "
                 "by all copies, so more copies won't help. Find that limit "
                 "instead.")
    elif g.efficiency >= 0.9:
        text += (f"Each extra copy added {less}about {g.gain_per_copy:.0f}, "
                 "close to a full copy: copies scale well.")
    else:
        text += (f"Each extra copy added {less}about {g.gain_per_copy:.0f}, "
                 f"{_pct(g.efficiency)} of what one copy handled on its own. "
                 "The copies share this machine and whatever else they all use "
                 "(like one database), so each new copy adds less than the "
                 "first.")
    if so.usable:
        text += " The copy counts in this summary count what an extra copy "
        text += "really added."
    out.append(textwrap.fill(text, 72, initial_indent="  ",
                             subsequent_indent="  "))
    for note in so.notes:
        if "different" in note or "share no load level" in note:
            out.append(textwrap.fill(f"Careful: {note}.", 72,
                                     initial_indent="  ",
                                     subsequent_indent="  "))
    out.append("")
    return "\n".join(out)
