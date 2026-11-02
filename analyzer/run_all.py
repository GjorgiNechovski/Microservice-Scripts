"""Generic analysis runner: one command, all four layers, any system.

Usage::

    python3 -m analyzer.run_all systems/finki-blogger.toml

Reads a system spec (see analyzer/spec.py) and prints a full report covering
Layer 1 (performance), Layer 2 (reliability), Layer 3 (structure / SPOFs) and
Layer 4 (where to invest). Point it at a different spec file to analyze a
different application -- the engine is unchanged.
"""

from __future__ import annotations

import math
import sys

from analyzer.spec import analyze, load_or_default_measurements, load_system

_MIN_YR = 365 * 24 * 60


def _nines(unavail: float) -> float:
    return math.inf if unavail <= 0 else -math.log10(unavail)


def _fmt_nines(n: float) -> str:
    # A redundant tier can be so available its unavailability underflows to 0;
    # cap the display rather than printing "inf".
    return ">15" if (n == math.inf or n > 15) else f"{n:.2f}"


def _fmt_mttf(hours: float) -> str:
    if hours == math.inf or hours > 1e6:
        return ">1e6"
    return f"{hours:.1f}"


def _rule(title: str) -> None:
    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)


def report(results: dict, measured_from: str) -> None:
    print(f"\nANALYSIS REPORT -- {results['name']}")
    print(f"measurements: {measured_from}")

    # ---- Layer 1: performance -------------------------------------------
    _rule("Layer 1 -- Performance (M/M/c per service)")
    print(f"{'service':<18}{'c':>3}{'rho':>8}{'W (s)':>10}{'P(wait)':>10}{'stable':>8}")
    for name, s in results["services"].items():
        q = s.queue
        w = "inf" if not q.stable else f"{q.W:.3f}"
        print(f"{name:<18}{s.replicas:>3}{q.rho:>8.3f}{w:>10}"
              f"{q.p_wait:>10.3f}{str(q.stable):>8}")

    # ---- Layer 2: reliability -------------------------------------------
    _rule("Layer 2 -- Reliability (CTMC)")
    print(f"{'component':<18}{'kind':>8}{'availability':>14}{'nines':>7}"
          f"{'MTTF(h)':>12}{'MTTR(h)':>9}")
    for name, s in results["services"].items():
        r = s.reliability
        print(f"{name:<18}{'tier':>8}{r.availability:>14.7f}"
              f"{_fmt_nines(r.nines()):>7}{_fmt_mttf(r.mttf):>12}"
              f"{r.mttr:>9.3f}")
    counts = results.get("infra_instances", {})
    for name, r in results["infrastructure"].items():
        n = counts.get(name, 1)
        kind = "single" if n <= 1 else f"x{n}"
        print(f"{name:<18}{kind:>8}{r.availability:>14.7f}"
              f"{_fmt_nines(r.nines()):>7}{_fmt_mttf(r.mttf):>12}"
              f"{r.mttr:>9.3f}")
    if any(n > 1 for n in counts.values()):
        print("  (availability shown is per instance; the operation-level "
              "figures below use the instance count)")

    # ---- Layer 3 & 4: per top event -------------------------------------
    for ev, t in results["top_events"].items():
        _rule(f"Top event: {ev}   (requires: {', '.join(t.requires)})")
        U = 1.0 - t.availability
        print(f"System availability : {t.availability:.7f}  (~{_fmt_nines(_nines(U))} nines)")
        print(f"Downtime per year   : {U * _MIN_YR:.0f} min")
        print(f"Single points of failure: {t.spofs or 'none'}")

        print("\nMinimal cut sets:")
        for m in t.cut_sets:
            tag = "SPOF" if len(m) == 1 else f"all {len(m)}"
            print(f"  {sorted(m)}  <- {tag}")

        print("\nLayer 4 -- where to invest (downtime saved / year):")
        maxsave = max((s for _, _, s in t.interventions), default=0.0) or 1.0
        for label, newA, saved in t.interventions:
            bar = "█" * int(round(min(saved / maxsave, 1.0) * 24))
            print(f"  {label:<26}{_fmt_nines(_nines(1 - newA)):>6} nines "
                  f"{saved:>8.0f} min  {bar}")


def main(argv: list) -> int:
    if len(argv) != 1:
        print("usage: python3 -m analyzer.run_all <system-spec.toml>",
              file=sys.stderr)
        return 2
    system_path = argv[0]
    spec = load_system(system_path)
    measurements, source = load_or_default_measurements(system_path, spec)
    if source == "built-in placeholder defaults":
        print(f"[warning] no measurements file found next to {system_path}.\n"
              f"          Using placeholder defaults so the analysis can run.\n"
              f"          The measurement phase will generate real numbers.",
              file=sys.stderr)
    report(analyze(spec, measurements), measured_from=source)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
