"""One command for the whole pipeline: measure -> analyze -> save report + figures.

    # FULL run (default): load test + fault injection + analyze, then WRITE the
    # report and figures to the output directory.
    python3 -m analyzer systems/finki-blogger.toml

    # Just re-analyze the already-measured data (no stress test, no outages):
    python3 -m analyzer systems/finki-blogger.toml --no-measure

    # ...or a finished SWEEP, from its per-level files (also re-checks each
    # level's validity and endpoint hotspots in Prometheus, read-only):
    python3 -m analyzer systems/finki-blogger.toml --no-measure --out analysis

    # ...and measure what extra copies really added, against another saved
    # sweep of the same system run with different replica counts:
    python3 -m analyzer systems/finki-blogger.toml --no-measure --out analysis \\
        --compare-replicas analysis-2-replicas

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
import glob
import io
import os
import sys
import time
import tomllib
from datetime import datetime

from analyzer.capacity import capacity_section, capacity_target, size_services
from analyzer.collect import loadgen
from analyzer.diagnose import sweep_position
from analyzer.explain import explain
from analyzer.responsiveness import (
    judge_one, judge_sweep_dir, level_report, read_k6)
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

    run = None
    if cfg is not None:
        print(f"[collect] running load script '{cfg['script']}' "
              "(blocks until it finishes)...")
        started = time.time()
        run = loadgen.run_load(cfg, project_dir=os.getcwd())
        window = loadgen.measurement_window(run.seconds)
        print(f"[collect] load done in {run.seconds:.0f}s; measuring over "
              f"the last {window}")
        _save_k6_output(run, args.out)
        _scan_logs(spec, started, time.time(), args.out)
    else:
        print("[collect] no [load] section; measuring current traffic")

    prom = Prometheus(args.prometheus, timeout=10.0)
    try:
        rows = measure(prom, spec, window=window)
        write_measured(measurements_path(spec_path), spec, rows)
        print(f"[collect] wrote performance numbers to "
              f"{measurements_path(spec_path)}")
        if run is not None:
            validity = _check_validity(spec, prom, rows, run, cfg, window,
                                       "single run", args.out)
            print(f"[collect] validity: {validity.verdict} "
                  f"(see {os.path.join(args.out, 'validity.txt')})")
        _write_hotspots(spec, prom, window, args.out)
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

def _render_report(results, source, capacity_text=None, unmeasured=(),
                   feel=None, position=None, logs=None) -> str:
    """Render the analysis report to a string, plus where the level stood in
    its sweep (``position``), what the load generator saw (``feel``), the
    known error messages in its logs (``logs``) and a capacity block, when
    given."""
    from analyzer.diagnose import level_position_report
    from analyzer.logscan import level_report as logs_report

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        report(results, measured_from=source, unmeasured=unmeasured)
    text = (buf.getvalue() + level_position_report(position) + level_report(feel)
            + logs_report(logs))
    if capacity_text:
        text += "\n" + capacity_text
    return text


def _write_level_reports(spec, args, ldir, lr, target_util, max_response,
                         pos, so=None) -> None:
    """report.txt, report-explained.txt and figures for one sweep level.

    Written once the whole sweep is known: ``pos`` (diagnose.SweepPosition, or
    None if the sweep never levelled off) says whether this level was below,
    at or past the limit the sweep hit, which the per-level math cannot see.
    """
    position = pos.at(lr.vus, lr.measurements, spec) if pos is not None else None
    meas = lr.measurements
    _write(os.path.join(ldir, "report.txt"),
           _render_report(lr.results, f"load sweep @ {lr.vus} VUs",
                          capacity_section(spec, meas, target_util, max_response,
                                           position, so),
                          unmeasured_recovery(spec, meas), lr.feel, position,
                          lr.logs))
    _write(os.path.join(ldir, "report-explained.txt"),
           explain(lr.results, spec, meas, lr.feel, position, lr.logs))
    if not args.no_figures:
        _figures(spec, meas, ldir)


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


def _check_validity(spec, prom, rows, run, cfg, window, label, outdir):
    """Run the CLAUDE.md validity checklist on a finished load run, save it."""
    from analyzer.validity import (
        check_run, parse_k6_summary, planned_seconds, validity_explain,
        validity_report)

    validity = check_run(spec, prom, rows, parse_k6_summary(run.output),
                         planned_seconds(cfg), window, label)
    os.makedirs(outdir, exist_ok=True)
    _write(os.path.join(outdir, "validity.txt"), validity_report(validity))
    _write(os.path.join(outdir, "validity-explained.txt"),
           validity_explain(validity))
    return validity


def _write_hotspots(spec, prom, window, outdir):
    """Per-endpoint busy-time breakdown over the same window, saved."""
    from analyzer.hotspots import (
        endpoint_hotspots, hotspots_explain, hotspots_report)

    hotspots = endpoint_hotspots(prom, spec, window)
    os.makedirs(outdir, exist_ok=True)
    _write(os.path.join(outdir, "hotspots.txt"), hotspots_report(spec, hotspots))
    _write(os.path.join(outdir, "hotspots-explained.txt"),
           hotspots_explain(spec, hotspots))
    return hotspots


def _scan_logs(spec, since, until, outdir):
    """Count known error messages in every component's docker logs over a
    load run's window (read-only), save them next to its reports; None when
    the spec turns scanning off or nothing could be read."""
    from analyzer import logscan

    if not logscan.enabled(spec):
        return None
    try:
        record = logscan.scan_level(spec, since, until)
    except ValueError as exc:                    # a bad [logs] extra entry
        print(f"[logs]   SKIPPED: {exc}", file=sys.stderr)
        return None
    os.makedirs(outdir, exist_ok=True)
    logscan.save(record, outdir)
    print(f"[logs]   {logscan.one_line(record)}")
    return record


def _measure_level(spec, args, cfg, level, base_meas):
    """Run the load at one VU level, measure, and merge onto the shared rates.

    Returns ``(measurements, rows, measured_toml_text, validity, hotspots,
    logs)``. The performance numbers are fresh for this level; failure/repair
    rates come from ``base_meas`` (the single chaos pass), so every level
    shares the same reliability inputs.
    """
    from analyzer.collect import loadgen
    from analyzer.collect.collector import measure, render_measured_toml
    from analyzer.collect.promql import Prometheus

    lvl_cfg = {**cfg, "vus": level}
    print(f"[sweep] load @ {level} VUs (blocks until it finishes)...")
    started = time.time()
    run = loadgen.run_load(lvl_cfg, project_dir=os.getcwd())
    ended = time.time()
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
    ldir = os.path.join(args.out, f"load-{level}")
    validity = _check_validity(spec, prom, rows, run, lvl_cfg, window,
                               f"load @ {level} VUs", ldir)
    print(f"[sweep]   validity: {validity.verdict}")
    hotspots = _write_hotspots(spec, prom, window, ldir)
    logs = _scan_logs(spec, started, ended, ldir)
    # inherit_effective=False: this pass only decides whether each service
    # saturated AT THIS LEVEL, and that must rest on the mu measured now. A
    # previous run's service_rate_effective would otherwise drive the verdict
    # and hide a regression. The clean rate is re-derived below, across levels.
    text = render_measured_toml(spec, rows, existing=base_meas,
                                inherit_effective=False)
    return tomllib.loads(text), rows, text, validity, hotspots, logs


def _derive_effective(spec, measured, target_util):
    """Clean per-replica rates across a sweep's ``(vus, measurements)`` levels.

    Returns ``({service: mu}, {services whose mu is only a floor})``: a service
    that never levelled off gets its peak throughput per replica as a FLOOR
    (saturation.py), flagged so no report treats it as a capacity.
    """
    from analyzer.saturation import estimate_capacity
    from analyzer.scaling import LevelResult

    prelim = [LevelResult(lv, m, analyze(spec, m), {}) for lv, m in measured]
    caps = estimate_capacity(prelim, spec, target_util)
    effective = {name: caps[name].mu_clean for name in spec["services"]
                 if caps[name].mu_clean > 0}
    floors = {n for n in spec["services"] if caps[n].lower_bound}
    sat = [n for n in spec["services"] if caps[n].saturated]
    if sat:
        print("[sweep] recovered clean throughput-based service rate for "
              f"saturated service(s): {', '.join(sat)}")
    if floors:
        names = ", ".join(n for n in spec["services"] if n in floors)
        print(f"[sweep] no throughput plateau for {names}: kept pace with the "
              "users at every level, so the limit lies above the tested load "
              "(its rate is recorded as a floor, not sized)")
    return effective, floors


def _set_derived(text, effective, floors) -> str:
    """A saved measured.toml with its derived service-rate keys replaced."""
    from analyzer.collect.collector import _fmt

    out, section = [], None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            section = s[len("[services."):-1] if s.startswith("[services.") else None
        if section and s.startswith(("service_rate_effective",
                                     "service_rate_lower_bound")):
            continue
        out.append(line)
        if section in effective and s.startswith("service_rate ="):
            out.append(f"service_rate_effective = {_fmt(effective[section])}")
            if section in floors:
                out.append("service_rate_lower_bound = true")
    return "\n".join(out) + "\n"


def _run_sweep(spec, spec_path, args, cfg, levels) -> int:
    from analyzer.collect.promql import PrometheusError
    from analyzer.scaling import LevelResult

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
    measured = []               # (level, meas, rows, validity, hotspots, logs)
    for level in levels:
        try:
            meas, rows, _text, validity, hotspots, logs = _measure_level(
                spec, args, cfg, level, base_meas)
        except PrometheusError as exc:
            print(f"[sweep] level {level} SKIPPED (Prometheus unreachable): "
                  f"{exc}", file=sys.stderr)
            continue
        measured.append((level, meas, rows, validity, hotspots, logs))

    if not measured:
        print("error: no load level produced measurements (is the stack up?)",
              file=sys.stderr)
        return 1

    effective, floors = _derive_effective(
        spec, [(lv, m) for lv, m, *_ in measured], target_util)
    feels = judge_sweep_dir(spec, args.out, [lv for lv, *_ in measured])

    level_results, texts = [], {}
    for level, meas, rows, validity, hotspots, logs in measured:
        texts[level] = render_measured_toml(spec, rows, existing=base_meas,
                                            effective=effective,
                                            lower_bound=floors)
        meas = tomllib.loads(texts[level])
        results = analyze(spec, meas)
        sizings = size_services(spec, meas, target_util, max_response)
        level_results.append(LevelResult(level, meas, results, sizings,
                                         validity, hotspots, feels[level],
                                         logs))
    pos = sweep_position(level_results, spec, target_util)
    so = _scaleout(spec, args, level_results, target_util)

    for lr in level_results:
        level, meas, text = lr.vus, lr.measurements, texts[lr.vus]
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
        _write_level_reports(spec, args, ldir, lr, target_util, max_response,
                             pos, so)

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

    return _finish_sweep(spec, args, level_results, target_util, max_response,
                         so)


def _scaleout(spec, args, level_results, target_util):
    """What extra copies really added, against ``--compare-replicas`` (another
    saved sweep of this system on different copy counts); None without it."""
    from analyzer.scaleout import load_sweep, measure_scaleout

    other = getattr(args, "compare_replicas", None)
    if not other:
        return None
    try:
        other_levels = load_sweep(spec, other)
    except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError) as exc:
        print(f"[scale-out] SKIPPED: {exc}", file=sys.stderr)
        return None
    so = measure_scaleout(spec, level_results, other_levels, target_util,
                          os.path.basename(os.path.normpath(args.out)),
                          os.path.basename(os.path.normpath(other)))
    if so is None:
        print(f"[scale-out] SKIPPED: {other} ran the same copies of every "
              "service, so there is no gain to measure", file=sys.stderr)
    else:
        print(f"[scale-out] compared with {other}: {so.bound}"
              + "".join(f"; {g.service} +{g.more - g.fewer} cop"
                        f"{'y' if g.more - g.fewer == 1 else 'ies'} = "
                        f"{g.efficiency * 100:.0f}% of a full copy"
                        for g in so.gains.values()))
    return so


def _finish_sweep(spec, args, level_results, target_util, max_response,
                  so=None) -> int:
    """Cross-level scaling report + figures, shared by a live and a saved sweep.
    ``so``: what extra copies really added (``_scaleout``), or None."""
    from analyzer.scaling import scaling_explain, scaling_report

    sr = scaling_report(spec, level_results, target_util, max_response, so)
    # files first: a terminal that cannot print the report must never lose it
    _write(os.path.join(args.out, "scaling-report.txt"), sr)
    _write(os.path.join(args.out, "scaling-report-explained.txt"),
           scaling_explain(spec, level_results, target_util, max_response, so))
    sys.stdout.write(sr)

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


def _saved_sweep_levels(out) -> list:
    """(vus, dir) of every load-<N>/ under ``out`` that has a measured.toml."""
    found = []
    for mpath in glob.glob(os.path.join(out, "load-*", "measured.toml")):
        ldir = os.path.dirname(mpath)
        name = os.path.basename(ldir)
        if name[len("load-"):].isdigit():
            found.append((int(name[len("load-"):]), ldir))
    return sorted(found)


def _reanalyse_sweep(spec, spec_path, args, saved) -> int:
    """Rebuild a finished sweep's reports from its saved per-level files.

    No load and no fault injection: each level's measured.toml is analysed
    again, and the read-only checks -- validity and per-endpoint hotspots --
    are re-run against Prometheus at the moment that level's load ended. The
    clean service rates are derived again from the measured ones, as the live
    sweep does, so a sweep saved by an older version gets the current rules;
    each level's measured.toml and report files are rewritten to match. A
    level's saved log scan (log-signals.json, see logscan.py) is read back.
    """
    from analyzer import logscan
    from analyzer.collect.promql import Prometheus, PrometheusError
    from analyzer.hotspots import (
        endpoint_hotspots, hotspots_explain, hotspots_report)
    from analyzer.scaling import LevelResult
    from analyzer.validity import recheck_saved_run

    print(f"[pipeline] re-analysing the saved sweep in {args.out} "
          f"({', '.join(str(v) for v, _ in saved)} VUs) -- nothing is run "
          "against the system")
    try:
        cfg = loadgen.load_config(spec, spec_path)
    except (ValueError, FileNotFoundError):
        cfg = None
    target_util, max_response = capacity_target(spec, args)
    prom = Prometheus(args.prometheus, timeout=10.0)

    derived = ("service_rate_effective", "service_rate_lower_bound")
    raw = {}
    for vus, ldir in saved:
        with open(os.path.join(ldir, "measured.toml"), encoding="utf-8") as fh:
            text = fh.read()
        meas = tomllib.loads(text)
        meas["services"] = {n: {k: v for k, v in p.items() if k not in derived}
                            for n, p in meas.get("services", {}).items()}
        raw[vus] = (text, meas)
    effective, floors = _derive_effective(
        spec, [(vus, raw[vus][1]) for vus, _ in saved], target_util)
    feels = judge_sweep_dir(spec, args.out, [vus for vus, _ in saved])

    level_results = []
    for vus, ldir in saved:
        text = _set_derived(raw[vus][0], effective, floors)
        meas = tomllib.loads(text)
        _write(os.path.join(ldir, "measured.toml"), text)
        results = analyze(spec, meas)
        validity = hotspots = None
        if os.path.isfile(os.path.join(ldir, "k6-output.txt")):
            try:
                validity, window, at = recheck_saved_run(
                    spec, prom, cfg, f"load @ {vus} VUs", ldir, vus)
                hotspots = endpoint_hotspots(prom, spec, window, at=at)
                _write(os.path.join(ldir, "hotspots.txt"),
                       hotspots_report(spec, hotspots))
                _write(os.path.join(ldir, "hotspots-explained.txt"),
                       hotspots_explain(spec, hotspots))
                print(f"[sweep]   {vus} VUs: validity {validity.verdict}")
            except PrometheusError as exc:
                print(f"[sweep]   {vus} VUs: validity/hotspots SKIPPED "
                      f"(Prometheus unreachable): {exc}", file=sys.stderr)
        level_results.append(LevelResult(
            vus, meas, results,
            size_services(spec, meas, target_util, max_response),
            validity, hotspots, feels[vus], logscan.load(ldir)))
    pos = sweep_position(level_results, spec, target_util)
    so = _scaleout(spec, args, level_results, target_util)
    for (vus, ldir), lr in zip(saved, level_results):
        _write_level_reports(spec, args, ldir, lr, target_util, max_response,
                             pos, so)
    return _finish_sweep(spec, args, level_results, target_util, max_response,
                         so)


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
    # a single level has no lighter one to compare with, so only the fixed
    # limits apply (max_error_rate, max_p95_ms); and only a load run of THIS
    # invocation has a k6 summary that belongs to these measurements
    feel = (judge_one(spec, read_k6(os.path.join(args.out, "k6-output.txt")))
            if do_load else None)
    from analyzer import logscan
    logs = (logscan.load(args.out) if do_load and logscan.enabled(spec)
            else None)
    text = _render_report(results, source, cap_text,
                          unmeasured_recovery(spec, measurements), feel,
                          logs=logs)
    report_path = _write(os.path.join(args.out, "report.txt"), text)
    sys.stdout.write(text)

    explained_path = _write(os.path.join(args.out, "report-explained.txt"),
                            explain(results, spec, measurements, feel,
                                    logs=logs))

    n_figs = 0 if args.no_figures else _figures(spec, measurements, args.out)
    print(f"\n[done] report: {report_path}  |  plain-language: {explained_path}"
          + (f"  |  figures: {n_figs} in {args.out}/" if n_figs else ""))
    return 0


def main(argv=None) -> int:
    from analyzer import utf8_stdio

    utf8_stdio()
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
    parser.add_argument("--compare-replicas", metavar="DIR", default=None,
                        help="another saved sweep of this system run with "
                             "different replica counts: measures what the "
                             "extra copies really added and sizes with that "
                             "(sweeps only; read-only)")
    args = parser.parse_args(argv)

    try:
        spec = load_system(args.spec)
    except (OSError, ValueError) as exc:
        print(f"error: could not load spec '{args.spec}': {exc}", file=sys.stderr)
        return 2

    do_load = not args.no_measure and not args.skip_load
    do_chaos = not args.no_measure and not args.skip_chaos

    # --no-measure on a directory that holds a finished sweep re-analyses it.
    if args.no_measure:
        saved = _saved_sweep_levels(args.out)
        if saved:
            return _reanalyse_sweep(spec, args.spec, args, saved)

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
