"""One command for the whole pipeline: measure -> analyze -> save report + figures.

    # FULL run (default): load test + fault injection + analyze, then WRITE the
    # report and figures to the output directory.
    python3 -m analyzer systems/finki-blogger.toml

    # Just re-analyze the already-measured data (no stress test, no outages):
    python3 -m analyzer systems/finki-blogger.toml --no-measure

    # Skip one measurement phase, or the figures:
    python3 -m analyzer systems/finki-blogger.toml --skip-chaos
    python3 -m analyzer systems/finki-blogger.toml --no-figures

If ``[load].vus`` is a LIST (e.g. ``[50, 100, 200, 500]``) the pipeline runs a
LOAD SWEEP: it measures + analyzes the system once per level (writing a full
analysis to ``analysis/load-<N>/``), fault-injects once (recovery is
load-independent), and writes a final cross-level ``scaling-report`` that names
the bottleneck, the highest safe load and the replicas each service needs.

Outputs (default dir ``analysis/``): ``report.txt`` + one PNG per figure (single
run), or per-level subdirs + ``scaling-report.txt`` (sweep). To see the analysis
you open those files; to refresh the numbers you re-run this. Phase 0 (the
observability stack + app) must already be running; if a live phase can't reach
it, it warns and the analysis still proceeds on whatever is in the measured file.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import tomllib
from datetime import datetime

from analyzer.capacity import capacity_section, capacity_target, size_services
from analyzer.collect import loadgen
from analyzer.explain import explain
from analyzer.run_all import report
from analyzer.spec import (
    analyze,
    load_or_default_measurements,
    load_system,
    measurements_path,
    unmeasured_recovery,
)


# ---------------------------------------------------------------------------
# measurement phases
# ---------------------------------------------------------------------------

def _phase1_collect(spec, spec_path, args) -> None:
    """Phase 1 -- optional load, then measure performance from Prometheus."""
    from analyzer.collect import loadgen
    from analyzer.collect.collector import measure, write_measured
    from analyzer.collect.promql import Prometheus, PrometheusError

    window = args.window
    try:
        cfg = loadgen.load_config(spec, spec_path)
    except ValueError as exc:
        cfg = None
        print(f"[collect] {exc}", file=sys.stderr)

    if cfg is not None:
        print(f"[collect] running load script '{cfg['script']}' "
              "(blocks until it finishes)...")
        run = loadgen.run_load(cfg, project_dir=os.getcwd())
        window = loadgen.measurement_window(run.seconds)
        print(f"[collect] load done in {run.seconds:.0f}s; measuring over "
              f"the last {window}")
        _save_k6_output(run, args.out)
    else:
        print("[collect] no [load] section; measuring current traffic")

    prom = Prometheus(args.prometheus, timeout=10.0)
    try:
        rows = measure(prom, spec, window=window)
        write_measured(measurements_path(spec_path), spec, rows)
        print(f"[collect] wrote performance numbers to "
              f"{measurements_path(spec_path)}")
    except PrometheusError as exc:
        print(f"[collect] SKIPPED (Prometheus unreachable): {exc}",
              file=sys.stderr)


def _phase2_chaos(spec, spec_path, args) -> None:
    """Phase 2 -- kill containers, measure repair rate. DESTRUCTIVE."""
    from analyzer.chaos.dockercli import Docker, DockerError
    from analyzer.chaos.injector import (
        component_to_service, measure_mttr, readiness_port,
        resolve_containers, write_measured,
    )

    print("[chaos] injecting faults -- single-instance components incur brief "
          "outages while they recover")
    docker = Docker()
    try:
        containers = resolve_containers(spec, docker)
    except DockerError as exc:
        print(f"[chaos] SKIPPED: {exc}", file=sys.stderr)
        return

    results = {}
    for comp in component_to_service(spec):
        print(f"[chaos] injecting {comp} ...", file=sys.stderr)
        results[comp] = measure_mttr(docker, comp, containers.get(comp, []),
                                     repetitions=args.repetitions,
                                     timeout=60.0,
                                     port=readiness_port(spec, comp))
    write_measured(measurements_path(spec_path), spec, results)
    print(f"[chaos] wrote repair rates to {measurements_path(spec_path)}")


# ---------------------------------------------------------------------------
# report / figure rendering
# ---------------------------------------------------------------------------

def _render_report(results, source, capacity_text=None, unmeasured=()) -> str:
    """Render the analysis report to a string (optionally + a capacity block)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        report(results, measured_from=source, unmeasured=unmeasured)
    text = buf.getvalue()
    if capacity_text:
        text += "\n" + capacity_text
    return text


def _write(path: str, text: str) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def _figures(spec, measurements, outdir) -> int:
    try:
        from analyzer.figures.plots import generate_all
    except ImportError:
        print("[figures] SKIPPED (matplotlib/numpy not installed)",
              file=sys.stderr)
        return 0
    return len(generate_all(spec, measurements, outdir))


def _rebuild_hint() -> str:
    """How to bring a stack back up, for remediation messages.

    Generic on purpose: the analyzer does not own the system it measures, so
    it cannot know that system's own rebuild script.
    """
    return "e.g. `docker compose up -d` in your app's directory"


def _verify_recovery(spec) -> bool:
    """After chaos, confirm the stack fully recovered; refuse to measure if not."""
    from analyzer.chaos.dockercli import Docker, DockerError
    from analyzer.chaos.injector import verify_recovered

    try:
        report = verify_recovered(Docker(), spec, timeout=60.0)
    except DockerError as exc:
        print(f"[recovery] check SKIPPED (Docker unavailable): {exc}",
              file=sys.stderr)
        return True
    if report.healed:
        print(f"[recovery] restarted after chaos: {', '.join(report.healed)}")
    if report.all_ok:
        print(f"[recovery] all {len(report.recovered)} components recovered "
              "-- proceeding.")
        return True
    print("[recovery] STACK DID NOT FULLY RECOVER after chaos:", file=sys.stderr)
    for comp in report.missing:
        print(f"           MISSING (no container): {comp}", file=sys.stderr)
    for comp in report.unhealthy:
        print(f"           UNHEALTHY (not ready in time): {comp}", file=sys.stderr)
    print("           Refusing to measure a degraded stack -- the numbers would "
          "be meaningless.", file=sys.stderr)
    print(f"           Bring the stack back up ({_rebuild_hint()}) then re-run.",
          file=sys.stderr)
    return False


# ---------------------------------------------------------------------------
# load sweep
# ---------------------------------------------------------------------------

def _save_k6_output(run, outdir) -> None:
    """Keep k6's own end-of-test summary next to the analysis it fed.

    It is the only record of whether the load ran its full duration and how
    many checks passed (a run of a few seconds means setup() failed).
    """
    os.makedirs(outdir, exist_ok=True)
    path = _write(os.path.join(outdir, "k6-output.txt"), run.output)
    print(f"[load]   k6 output: {path}")


def _measure_level(spec, args, cfg, level, base_meas):
    """Run the load at one VU level, measure, and merge onto the shared rates.

    Returns ``(measurements_dict, measured_toml_text)``. The performance numbers
    are fresh for this level; failure/repair rates come from ``base_meas`` (the
    single chaos pass), so every level shares the same reliability inputs.
    """
    from analyzer.collect import loadgen
    from analyzer.collect.collector import measure, render_measured_toml
    from analyzer.collect.promql import Prometheus

    lvl_cfg = {**cfg, "vus": level}
    print(f"[sweep] load @ {level} VUs (blocks until it finishes)...")
    run = loadgen.run_load(lvl_cfg, project_dir=os.getcwd())
    if run.returncode == 99:
        print(f"[sweep]   note: k6 thresholds crossed at {level} VUs "
              "(expected when saturating) -- run completed, measuring anyway")
    elif not run.ok:
        tail = "\n".join(run.output.strip().splitlines()[-15:])
        raise SystemExit(
            f"[sweep] ABORT: k6 exited with code {run.returncode} after "
            f"{run.seconds:.0f}s at {level} VUs -- no measurement taken.\n"
            f"[sweep] k6 output (tail):\n{tail}")
    window = loadgen.measurement_window(run.seconds)
    print(f"[sweep]   done in {run.seconds:.0f}s; measuring over last {window}")
    _save_k6_output(run, os.path.join(args.out, f"load-{level}"))

    prom = Prometheus(args.prometheus, timeout=10.0)
    rows = measure(prom, spec, window=window)
    # inherit_effective=False: this pass only decides whether each service
    # saturated AT THIS LEVEL, and that must rest on the mu measured now. A
    # previous run's service_rate_effective would otherwise drive the verdict
    # and hide a regression. The clean rate is re-derived below, across levels.
    text = render_measured_toml(spec, rows, existing=base_meas,
                                inherit_effective=False)
    return tomllib.loads(text), rows, text


def _run_sweep(spec, spec_path, args, cfg, levels) -> int:
    from analyzer.collect.promql import PrometheusError
    from analyzer.scaling import LevelResult, scaling_explain, scaling_report

    print(f"[pipeline] LOAD SWEEP over {len(levels)} levels: "
          f"{', '.join(map(str, levels))} VUs. "
          "Recovery is measured once (load-independent).")

    if not args.skip_chaos:
        _phase2_chaos(spec, spec_path, args)          # one pass, shared by all
        if not _verify_recovery(spec):                # don't sweep a broken stack
            return 1
    base_meas, _ = load_or_default_measurements(spec_path, spec)
    target_util, max_response = capacity_target(spec, args)

    os.makedirs(args.out, exist_ok=True)

    from analyzer.collect.collector import render_measured_toml
    measured = []                       # (level, meas, rows)
    for level in levels:
        try:
            meas, rows, _text = _measure_level(spec, args, cfg, level, base_meas)
        except PrometheusError as exc:
            print(f"[sweep] level {level} SKIPPED (Prometheus unreachable): "
                  f"{exc}", file=sys.stderr)
            continue
        measured.append((level, meas, rows))

    if not measured:
        print("error: no load level produced measurements (is the stack up?)",
              file=sys.stderr)
        return 1

    from analyzer.saturation import estimate_capacity
    prelim = [LevelResult(lv, m, analyze(spec, m), {}) for lv, m, _ in measured]
    caps = estimate_capacity(prelim, spec, target_util)
    effective = {name: caps[name].mu_clean for name in spec["services"]
                 if caps[name].mu_clean > 0}
    sat = [n for n in spec["services"] if caps[n].saturated]
    if sat:
        print("[sweep] recovered clean throughput-based service rate for "
              f"saturated service(s): {', '.join(sat)}")

    level_results = []
    for level, meas, rows in measured:
        text = render_measured_toml(spec, rows, existing=base_meas,
                                    effective=effective)
        meas = tomllib.loads(text)
        results = analyze(spec, meas)
        sizings = size_services(spec, meas, target_util, max_response)
        level_results.append(LevelResult(level, meas, results, sizings))

        ldir = os.path.join(args.out, f"load-{level}")
        os.makedirs(ldir, exist_ok=True)
        mpath = os.path.join(ldir, "measured.toml")

        # capture the PREVIOUS run for this level before we overwrite it
        prev_meas, prev_when = None, None
        if os.path.exists(mpath):
            try:
                with open(mpath, "rb") as fh:
                    prev_meas = tomllib.load(fh)
                prev_when = datetime.fromtimestamp(
                    os.path.getmtime(mpath)).strftime("%Y-%m-%d %H:%M")
            except (OSError, tomllib.TOMLDecodeError):
                prev_meas = None

        _write(mpath, text)
        cap_text = capacity_section(spec, meas, target_util, max_response)
        _write(os.path.join(ldir, "report.txt"),
               _render_report(results, f"load sweep @ {level} VUs", cap_text,
                              unmeasured_recovery(spec, meas)))
        _write(os.path.join(ldir, "report-explained.txt"),
               explain(results, spec, meas))
        if not args.no_figures:
            _figures(spec, meas, ldir)

        # if this level existed before, diff previous vs current
        if prev_meas is not None:
            from analyzer.compare import (
                comparison_explain, comparison_report, compare)
            try:
                cmp = compare(spec, prev_meas, meas, level, target_util)
                _write(os.path.join(ldir, "comparison.txt"),
                       comparison_report(cmp, prev_when))
                _write(os.path.join(ldir, "comparison-explained.txt"),
                       comparison_explain(cmp, prev_when))
                print(f"[sweep]   {level} VUs vs previous run: {cmp.overall}")
            except (ValueError, KeyError) as exc:
                print(f"[sweep]   comparison skipped for {level}: {exc}",
                      file=sys.stderr)

    sr = scaling_report(spec, level_results, target_util, max_response)
    sys.stdout.write(sr)
    _write(os.path.join(args.out, "scaling-report.txt"), sr)
    _write(os.path.join(args.out, "scaling-report-explained.txt"),
           scaling_explain(spec, level_results, target_util, max_response))

    n_figs = 0
    if not args.no_figures:
        try:
            from analyzer.figures.plots import generate_scaling
            n_figs = len(generate_scaling(spec, level_results, target_util,
                                          args.out))
        except ImportError:
            print("[figures] scaling figures SKIPPED (matplotlib not installed)",
                  file=sys.stderr)

    print(f"\n[done] {len(level_results)} levels analyzed in {args.out}/load-*/  "
          f"|  scaling report: {os.path.join(args.out, 'scaling-report.txt')}"
          + (f"  |  scaling figures: {n_figs}" if n_figs else ""))
    return 0


# ---------------------------------------------------------------------------
# single run
# ---------------------------------------------------------------------------

def _run_single(spec, spec_path, args, do_load, do_chaos) -> int:
    if do_load or do_chaos:
        phases = " + ".join(p for p, on in
                            (("load test", do_load), ("fault injection", do_chaos))
                            if on)
        print(f"[pipeline] FULL run: re-measuring the live system ({phases}). "
              "Use --no-measure to analyze saved data instead.")
    if do_load:
        _phase1_collect(spec, spec_path, args)
    if do_chaos:
        _phase2_chaos(spec, spec_path, args)

    os.makedirs(args.out, exist_ok=True)
    measurements, source = load_or_default_measurements(spec_path, spec)
    results = analyze(spec, measurements)

    if not measurements.get("services", {}) or any(
            not results["services"][n].queue.stable for n in spec["services"]):
        print("[note] single run: a service looks overloaded. Latency-based μ "
              "cannot tell a genuinely saturated service from a highly "
              "concurrent one measured under load. Run a load SWEEP "
              "([load] vus = a LIST) to recover the clean throughput-based μ "
              "and get a trustworthy utilization.", file=sys.stderr)

    target_util, max_response = capacity_target(spec, args)
    cap_text = capacity_section(spec, measurements, target_util, max_response)
    text = _render_report(results, source, cap_text,
                          unmeasured_recovery(spec, measurements))
    sys.stdout.write(text)
    report_path = _write(os.path.join(args.out, "report.txt"), text)

    explained_path = _write(os.path.join(args.out, "report-explained.txt"),
                            explain(results, spec, measurements))

    n_figs = 0 if args.no_figures else _figures(spec, measurements, args.out)
    print(f"\n[done] report: {report_path}  |  plain-language: {explained_path}"
          + (f"  |  figures: {n_figs} in {args.out}/" if n_figs else ""))
    return 0


def main(argv=None) -> int:
    loadgen.load_dotenv()

    parser = argparse.ArgumentParser(
        prog="python3 -m analyzer",
        description="Run the whole pipeline and save the report + figures. By "
                    "default this RE-MEASURES the live system (load test + "
                    "fault injection); use --no-measure to analyze saved data.")
    parser.add_argument("spec", help="structural system spec path")
    parser.add_argument("--no-measure", action="store_true",
                        help="do not re-measure; analyze the saved measured "
                             "file (no stress test, no outages)")
    parser.add_argument("--skip-load", action="store_true",
                        help="skip Phase 1 (load + performance) only")
    parser.add_argument("--skip-chaos", action="store_true",
                        help="skip Phase 2 (fault injection) only")
    parser.add_argument("--no-figures", action="store_true",
                        help="do not render figures")
    parser.add_argument("--out", default="analysis",
                        help="output directory for report.txt + figures "
                             "(default: %(default)s)")
    parser.add_argument("--prometheus", default="http://localhost:9090",
                        help="Prometheus URL (default: %(default)s)")
    parser.add_argument("--window", default="5m",
                        help="PromQL rate window (default: %(default)s)")
    parser.add_argument("--repetitions", type=int, default=3,
                        help="kills per component for chaos (default: %(default)s)")
    parser.add_argument("--target-utilization", type=float, default=None,
                        help="capacity sizing target ρ (overrides [capacity])")
    parser.add_argument("--max-response-ms", type=float, default=None,
                        help="capacity sizing response-time budget in ms "
                             "(overrides [capacity])")
    args = parser.parse_args(argv)

    try:
        spec = load_system(args.spec)
    except (OSError, ValueError) as exc:
        print(f"error: could not load spec '{args.spec}': {exc}", file=sys.stderr)
        return 2

    do_load = not args.no_measure and not args.skip_load
    do_chaos = not args.no_measure and not args.skip_chaos

    # A load sweep needs the live load phase; fall back to a single run otherwise.
    if do_load:
        try:
            cfg = loadgen.load_config(spec, args.spec)
        except ValueError:
            cfg = None
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        levels = loadgen.sweep_levels(cfg)
        if levels:
            return _run_sweep(spec, args.spec, args, cfg, levels)

    return _run_single(spec, args.spec, args, do_load, do_chaos)


if __name__ == "__main__":
    raise SystemExit(main())
