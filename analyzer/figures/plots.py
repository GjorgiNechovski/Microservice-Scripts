"""Thesis figures for the four analysis layers, driven by spec + measurements.

Design choices (kept consistent across every figure):
  * Colour-blind-safe categorical palette (Okabe-Ito), assigned to entities in a
    fixed order -- never cycled arbitrarily.
  * One y-axis per plot; log scales only where the data genuinely spans decades
    (queue blow-up, MTTR).
  * Model curves are drawn as thin lines; the MEASURED value is overlaid as a
    dot, so each figure shows "the model, and where this system actually sits".
  * Recessive grid/axes, direct labels on bars, legends only for >=2 series.
"""

from __future__ import annotations

import os

import matplotlib
matplotlib.use("Agg")            # headless: render straight to files
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np               # noqa: E402

from analyzer.models.ctmc import component_reliability  # noqa: E402
from analyzer.models.queueing import mmc_metrics         # noqa: E402
from analyzer.spec import analyze, failure_rate_for      # noqa: E402

# Okabe-Ito: the established colour-blind-safe qualitative palette for print.
_PALETTE = ["#0072B2", "#E69F00", "#009E73", "#D55E00", "#CC79A7",
            "#56B4E9", "#F0E442", "#000000"]
_INK = "#222222"
_MUTED = "#666666"
_GRID = "#d9d9d9"
_SERVICE_C = "#009E73"   # green
_INFRA_C = "#0072B2"     # blue
_MIN_YR = 365 * 24 * 60


def _color(i: int) -> str:
    return _PALETTE[i % len(_PALETTE)]


def _ax(w: float = 7.0, h: float = 4.2):
    fig, ax = plt.subplots(figsize=(w, h), dpi=110)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(_MUTED)
    ax.grid(True, color=_GRID, linewidth=0.6, alpha=0.9)
    ax.set_axisbelow(True)
    ax.tick_params(colors=_MUTED, labelsize=8)
    ax.title.set_color(_INK)
    ax.xaxis.label.set_color(_INK)
    ax.yaxis.label.set_color(_INK)
    return fig, ax


def _save(fig, outdir: str, name: str) -> str:
    path = os.path.join(outdir, name)
    fig.savefig(path, bbox_inches="tight", dpi=200, facecolor="white")
    plt.close(fig)
    return path


def _nines(unavail: float) -> float:
    return 15.0 if unavail <= 0 else min(15.0, -np.log10(unavail))


def _fmt_val(v: float, top: float) -> str:
    """Adaptive precision so small (sub-minute) and large values both read."""
    if top < 10:
        return f"{v:.2f}"
    if top < 100:
        return f"{v:.1f}"
    return f"{v:.0f}"


# ---------------------------------------------------------------------------
# Layer 1 -- queueing
# ---------------------------------------------------------------------------

def fig_utilization(spec, measurements, outdir) -> str:
    """Response time vs utilization (the M/M/c 'hockey stick'), per service."""
    ms = measurements["services"]
    fig, ax = _ax()
    rhos = np.linspace(0.01, 0.985, 240)
    for i, name in enumerate(spec["services"]):
        p = ms[name]
        mu, c = p.get("service_rate_effective", p["service_rate"]), int(p["replicas"])
        w_ms = [mmc_metrics(rho * c * mu, mu, c).W * 1000.0 for rho in rhos]
        col = _color(i)
        ax.plot(rhos, w_ms, color=col, lw=1.8, label=name)
        rho0 = p["arrival_rate"] / (c * mu)
        if 0 < rho0 < 1:
            w0 = mmc_metrics(p["arrival_rate"], mu, c).W * 1000.0
            ax.plot([rho0], [w0], "o", color=col, ms=6.5,
                    mec="white", mew=1.2, zorder=5)
    ax.set_yscale("log")
    ax.set_xlabel("utilization  ρ = λ / (c·μ)")
    ax.set_ylabel("mean response time  W  (ms, log scale)")
    ax.set_title("Layer 1 — M/M/c response time vs utilization", pad=12)
    leg = ax.legend(frameon=False, fontsize=8, ncol=2, labelcolor=_INK,
                    title="dots = measured operating point", title_fontsize=8)
    leg.get_title().set_color(_MUTED)
    return _save(fig, outdir, "layer1_utilization.png")


# ---------------------------------------------------------------------------
# Layer 2 -- reliability
# ---------------------------------------------------------------------------

def fig_downtime(spec, analysis, outdir) -> str:
    """Modeled downtime per year by component (the redundant tiers ~ 0)."""
    rows = [(n, s.reliability.unavailability * _MIN_YR, _SERVICE_C)
            for n, s in analysis["services"].items()]
    rows += [(n, r.unavailability * _MIN_YR, _INFRA_C)
             for n, r in analysis["infrastructure"].items()]
    rows.sort(key=lambda r: r[1], reverse=True)
    names = [r[0] for r in rows]
    vals = [r[1] for r in rows]
    cols = [r[2] for r in rows]

    fig, ax = _ax(h=0.42 * len(names) + 1.2)
    y = np.arange(len(names))
    ax.barh(y, vals, color=cols, height=0.68)
    ax.set_yticks(y)
    ax.set_yticklabels(names, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel("modeled downtime  (minutes / year)")
    ax.set_title("Layer 2 — downtime per year by component")
    top = max(vals) if vals else 1.0
    for yi, v in zip(y, vals):
        label = "≈0" if v < top * 0.005 else _fmt_val(v, top)
        ax.text(v + top * 0.01, yi, label, va="center", fontsize=7.5,
                color=_MUTED)
    ax.set_xlim(0, top * 1.15)
    _category_legend(ax, [("service tier (redundant)", _SERVICE_C),
                          ("infrastructure (single)", _INFRA_C)])
    return _save(fig, outdir, "layer2_downtime.png")


def fig_availability_vs_mttr(spec, measurements, outdir) -> str:
    """Availability (nines) vs recovery time for the single-instance infra."""
    mi = measurements["infrastructure"]
    infra = spec.get("infrastructure", [])
    if not infra:
        return ""
    fig, ax = _ax()
    mttrs = np.logspace(0, np.log10(3600), 240)     # 1 s .. 1 h
    for i, name in enumerate(infra):
        f = failure_rate_for(spec, name, mi[name]["failure_rate"])
        nines = [_nines(component_reliability(f, 3600.0 / m).unavailability)
                 for m in mttrs]
        col = _color(i)
        ax.plot(mttrs, nines, color=col, lw=1.8, label=name)
        meas_mttr = 3600.0 / mi[name]["repair_rate"]
        y = _nines(component_reliability(f, mi[name]["repair_rate"]).unavailability)
        ax.plot([meas_mttr], [y], "o", color=col, ms=6.5, mec="white",
                mew=1.2, zorder=5)
    ax.set_xscale("log")
    ax.set_xlabel("MTTR — mean time to recover  (seconds, log scale)")
    ax.set_ylabel("availability  (nines)")
    ax.set_title("Layer 2 — availability vs recovery time (infrastructure)",
                 pad=12)
    leg = ax.legend(frameon=False, fontsize=8, labelcolor=_INK,
                    title="dots = measured MTTR", title_fontsize=8)
    leg.get_title().set_color(_MUTED)
    return _save(fig, outdir, "layer2_availability_vs_mttr.png")


def fig_mttr_measured(spec, measurements, outdir) -> str:
    """Measured recovery time (MTTR) per component -- Phase 2 data."""
    rows = [(n, 3600.0 / measurements["services"][n]["repair_rate"], _SERVICE_C)
            for n in spec["services"]]
    rows += [(n, 3600.0 / measurements["infrastructure"][n]["repair_rate"],
              _INFRA_C) for n in spec.get("infrastructure", [])]
    rows.sort(key=lambda r: r[1], reverse=True)
    names = [r[0] for r in rows]
    vals = [r[1] for r in rows]
    cols = [r[2] for r in rows]

    fig, ax = _ax(h=0.42 * len(names) + 1.2)
    y = np.arange(len(names))
    ax.barh(y, vals, color=cols, height=0.68)
    ax.set_yticks(y)
    ax.set_yticklabels(names, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel("measured MTTR  (seconds)")
    ax.set_title("Measured recovery time per component (Phase 2 injection)")
    top = max(vals) if vals else 1.0
    for yi, v in zip(y, vals):
        ax.text(v + top * 0.01, yi, f"{v:.1f}s", va="center", fontsize=7.5,
                color=_MUTED)
    ax.set_xlim(0, top * 1.15)
    _category_legend(ax, [("service", _SERVICE_C), ("infrastructure", _INFRA_C)])
    return _save(fig, outdir, "measured_mttr.png")


# ---------------------------------------------------------------------------
# Layer 3 -- structure / importance
# ---------------------------------------------------------------------------

def fig_importance(op, top, outdir) -> str:
    """Birnbaum importance per component for one operation."""
    ranked = [(c, v) for c, v in top.importance if v > 0] or top.importance
    names = [c for c, _ in ranked]
    vals = [v for _, v in ranked]
    fig, ax = _ax(h=0.42 * len(names) + 1.2)
    y = np.arange(len(names))
    ax.barh(y, vals, color=_color(0), height=0.68)
    ax.set_yticks(y)
    ax.set_yticklabels(names, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel("Birnbaum importance")
    ax.set_title(f"Layer 3 — component importance for '{op}'")
    top_v = max(vals) if vals else 1.0
    for yi, v in zip(y, vals):
        ax.text(v + top_v * 0.01, yi, f"{v:.3g}", va="center", fontsize=7.5,
                color=_MUTED)
    ax.set_xlim(0, top_v * 1.15)
    return _save(fig, outdir, f"layer3_importance_{op}.png")


# ---------------------------------------------------------------------------
# Layer 4 -- sensitivity / where to invest
# ---------------------------------------------------------------------------

def fig_where_to_invest(op, top, outdir) -> str:
    """Downtime saved per year by each candidate intervention."""
    items = [(lbl, saved) for lbl, _, saved in top.interventions if saved > 0]
    if not items:
        return ""
    items.sort(key=lambda t: t[1], reverse=True)
    names = [t[0] for t in items]
    vals = [t[1] for t in items]
    fig, ax = _ax(h=0.42 * len(names) + 1.2)
    y = np.arange(len(names))
    ax.barh(y, vals, color=_color(3), height=0.68)
    ax.set_yticks(y)
    ax.set_yticklabels(names, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel("downtime saved  (minutes / year)")
    ax.set_title(f"Layer 4 — where to invest for '{op}'")
    top_v = max(vals) if vals else 1.0
    for yi, v in zip(y, vals):
        ax.text(v + top_v * 0.01, yi, _fmt_val(v, top_v), va="center",
                fontsize=7.5, color=_MUTED)
    ax.set_xlim(0, top_v * 1.15)
    return _save(fig, outdir, f"layer4_invest_{op}.png")


def fig_failure_sensitivity(spec, measurements, op, outdir) -> str:
    """System downtime vs a global scaling of the (estimated) failure rates.

    Shows how much the conclusion depends on the failure_rate estimate: sweep
    every component's failure rate from 0.1x to 10x and re-run the full model.
    """
    components = list(spec["services"]) + list(spec.get("infrastructure", []))
    eff = {}
    for name in spec["services"]:
        eff[name] = failure_rate_for(spec, name,
                                     measurements["services"][name]["failure_rate"])
    for name in spec.get("infrastructure", []):
        eff[name] = failure_rate_for(
            spec, name, measurements["infrastructure"][name]["failure_rate"])

    ks = np.logspace(-1, 1, 41)
    downtime = []
    for k in ks:
        spec_k = dict(spec)
        spec_k["failure_rates"] = {c: eff[c] * k for c in components}
        result = analyze(spec_k, measurements)
        u = 1.0 - result["top_events"][op].availability
        downtime.append(u * _MIN_YR)

    fig, ax = _ax()
    ax.plot(ks, downtime, color=_color(0), lw=2.0)
    ax.axvline(1.0, color=_MUTED, lw=1.0, ls="--")
    ax.text(1.0, ax.get_ylim()[1], " current estimate", fontsize=8,
            color=_MUTED, va="top")
    ax.set_xscale("log")
    ax.set_xlabel("failure-rate scale  (× the estimate)")
    ax.set_ylabel("system downtime  (minutes / year)")
    ax.set_title(f"Layer 4 — sensitivity of '{op}' to the failure-rate estimate")
    return _save(fig, outdir, f"layer4_failure_sensitivity_{op}.png")


# ---------------------------------------------------------------------------
# helpers + driver
# ---------------------------------------------------------------------------

def _category_legend(ax, entries):
    handles = [plt.Line2D([0], [0], marker="s", linestyle="", markersize=8,
                          markerfacecolor=c, markeredgecolor="none", label=l)
               for l, c in entries]
    ax.legend(handles=handles, frameon=False, fontsize=8, labelcolor=_INK,
              loc="lower right")


def generate_all(spec: dict, measurements: dict, outdir: str) -> list:
    """Render every figure into ``outdir``; return the list of file paths."""
    os.makedirs(outdir, exist_ok=True)
    analysis = analyze(spec, measurements)
    paths = [
        fig_utilization(spec, measurements, outdir),
        fig_downtime(spec, analysis, outdir),
        fig_availability_vs_mttr(spec, measurements, outdir),
        fig_mttr_measured(spec, measurements, outdir),
    ]
    for op, top in analysis["top_events"].items():
        paths.append(fig_importance(op, top, outdir))
        paths.append(fig_where_to_invest(op, top, outdir))
        paths.append(fig_failure_sensitivity(spec, measurements, op, outdir))
    return [p for p in paths if p]


# ---------------------------------------------------------------------------
# Load sweep -- performance across multiple virtual-user levels
# ---------------------------------------------------------------------------
# These take the list of per-level results (analyzer.scaling.LevelResult); each
# has .vus, .measurements, .results and .sizings. Only attributes are read, so
# any object with that shape works.

def fig_scaling_throughput(spec, levels, outdir) -> str:
    """Measured arrival rate per service across load (plateau = the ceiling)."""
    levels = sorted(levels, key=lambda x: x.vus)
    xs = [lv.vus for lv in levels]
    fig, ax = _ax()
    for i, name in enumerate(spec["services"]):
        ys = [float(lv.measurements["services"][name]["arrival_rate"])
              for lv in levels]
        ax.plot(xs, ys, marker="o", ms=5, color=_color(i), lw=1.8, label=name,
                mec="white", mew=0.8)
    ax.set_xlabel("offered load  (virtual users)")
    ax.set_ylabel("measured arrival rate  λ  (req/s)")
    ax.set_title("Load sweep — throughput vs load (plateau = capacity ceiling)",
                 pad=12)
    ax.legend(frameon=False, fontsize=8, ncol=2, labelcolor=_INK)
    return _save(fig, outdir, "scaling_throughput.png")


def fig_scaling_utilization(spec, levels, target, outdir) -> str:
    """Utilization per service across load (who crosses the target first)."""
    levels = sorted(levels, key=lambda x: x.vus)
    xs = [lv.vus for lv in levels]
    fig, ax = _ax()
    for i, name in enumerate(spec["services"]):
        ys = [lv.results["services"][name].queue.rho for lv in levels]
        ax.plot(xs, ys, marker="o", ms=5, color=_color(i), lw=1.8, label=name,
                mec="white", mew=0.8)
    ax.axhline(target, ls="--", color=_MUTED, lw=1.0)
    ax.text(xs[0], target, " target", fontsize=8, color=_MUTED, va="bottom")
    ax.axhline(1.0, ls=":", color=_color(3), lw=1.1)
    ax.text(xs[0], 1.0, " overload", fontsize=8, color=_color(3), va="bottom")
    ax.set_xlabel("offered load  (virtual users)")
    ax.set_ylabel("utilization  ρ = λ / (c·μ)")
    ax.set_title("Load sweep — utilization vs load (who saturates first)", pad=12)
    ax.legend(frameon=False, fontsize=8, ncol=2, labelcolor=_INK)
    return _save(fig, outdir, "scaling_utilization.png")


def fig_scaling_response_time(spec, levels, outdir) -> str:
    """Mean response time per service across load (the M/M/c hockey stick)."""
    levels = sorted(levels, key=lambda x: x.vus)
    xs = [lv.vus for lv in levels]
    fig, ax = _ax()
    any_stable = False
    for i, name in enumerate(spec["services"]):
        ys = []
        for lv in levels:
            q = lv.results["services"][name].queue
            ys.append(q.W * 1000.0 if q.stable else np.nan)
        if np.any(np.isfinite(ys)):
            any_stable = True
        ax.plot(xs, ys, marker="o", ms=5, color=_color(i), lw=1.8, label=name,
                mec="white", mew=0.8)
    if any_stable:
        ax.set_yscale("log")
    ax.set_xlabel("offered load  (virtual users)")
    ax.set_ylabel("mean response time  W  (ms, log scale)")
    ax.set_title("Load sweep — response time vs load "
                 "(gap = overloaded / ∞ wait)", pad=12)
    ax.legend(frameon=False, fontsize=8, ncol=2, labelcolor=_INK)
    return _save(fig, outdir, "scaling_response_time.png")


def fig_scaling_replicas(spec, levels, outdir) -> str:
    """Replicas the sizing math wants per service across load, vs current."""
    levels = sorted(levels, key=lambda x: x.vus)
    xs = [lv.vus for lv in levels]
    fig, ax = _ax()
    for i, name in enumerate(spec["services"]):
        needed = [lv.sizings[name].needed for lv in levels]
        col = _color(i)
        ax.plot(xs, needed, marker="o", ms=5, color=col, lw=1.8, label=name,
                mec="white", mew=0.8)
        current = levels[-1].sizings[name].current
        ax.axhline(current, ls=":", color=col, lw=0.9, alpha=0.7)
    ax.set_xlabel("offered load  (virtual users)")
    ax.set_ylabel("replicas needed  (solid)   vs current  (dotted)")
    ax.set_title("Load sweep — replicas needed vs load (capacity sizing)", pad=12)
    ax.legend(frameon=False, fontsize=8, ncol=2, labelcolor=_INK)
    return _save(fig, outdir, "scaling_replicas.png")


def fig_diagnose_quadrant(spec, dr, outdir) -> str:
    """Scale-out vs optimize decision map: request weight (x) vs load (y)."""
    _V = {"optimize": "#D55E00", "scale": "#0072B2", "ok": "#009E73"}
    fig, ax = _ax(w=7.4, h=4.8)

    xs = []
    for name in spec["services"]:
        d = dr.diagnoses[name]
        x = d.service_time_ms
        if not np.isfinite(x) or x <= 0:
            continue
        xs.append(x)
        y = min(d.peak_utilization, 1.2)          # clamp overloaded dots near the top
        ax.scatter([x], [y], s=90, color=_V[d.verdict], edgecolor="white",
                   linewidth=1.2, zorder=5)
        ax.annotate(name, (x, y), fontsize=7.5, color=_INK,
                    xytext=(5, 4), textcoords="offset points")

    ax.set_xscale("log")
    ax.set_xlim(min(xs + [dr.threshold_ms]) * 0.6, max(xs + [dr.threshold_ms]) * 2.0)
    ax.set_ylim(-0.05, 1.30)

    ax.axhline(dr.target, ls="--", color=_MUTED, lw=1.0)
    ax.axhline(1.0, ls=":", color="#D55E00", lw=1.1)
    ax.axvline(dr.threshold_ms, ls="--", color=_MUTED, lw=1.0)

    yt = ax.get_yaxis_transform()                 # x in axes-fraction, y in data
    ax.text(0.01, dr.target, " target load", transform=yt, va="bottom",
            fontsize=7.5, color=_MUTED)
    ax.text(0.01, 1.0, " overloaded", transform=yt, va="bottom",
            fontsize=7.5, color="#D55E00")
    ax.text(0.02, 0.98, "ADD REPLICAS\n(loaded, cheap request)",
            transform=ax.transAxes, ha="left", va="top", fontsize=7.5,
            color="#0072B2")
    ax.text(0.98, 0.98, "OPTIMIZE THE REQUEST\n(loaded, heavy)",
            transform=ax.transAxes, ha="right", va="top", fontsize=7.5,
            color="#D55E00")

    ax.set_xlabel("work per request   S = 1/μ   (ms, log scale)  →  heavier")
    ax.set_ylabel("peak load  (utilization, capped at 1.2)")
    ax.set_title("Scale-out vs optimize — where each service lands", pad=12)
    return _save(fig, outdir, "diagnose_quadrant.png")


def generate_scaling(spec: dict, levels: list, target: float,
                     outdir: str) -> list:
    """Render every load-sweep figure into ``outdir``; return the paths."""
    os.makedirs(outdir, exist_ok=True)
    paths = [
        fig_scaling_throughput(spec, levels, outdir),
        fig_scaling_utilization(spec, levels, target, outdir),
        fig_scaling_response_time(spec, levels, outdir),
        fig_scaling_replicas(spec, levels, outdir),
    ]
    from analyzer.diagnose import diagnose
    paths.append(fig_diagnose_quadrant(spec, diagnose(levels, spec, target), outdir))
    return [p for p in paths if p]
