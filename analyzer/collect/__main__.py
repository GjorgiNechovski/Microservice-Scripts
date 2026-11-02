"""Phase 1 CLI: measure Layer-1 inputs from Prometheus.

    # See what CAN be measured (run this first):
    python3 -m analyzer.collect systems/finki-blogger.toml --discover

    # Measure and preview (does not write):
    python3 -m analyzer.collect systems/finki-blogger.toml

    # Measure and write systems/finki-blogger.measured.toml:
    python3 -m analyzer.collect systems/finki-blogger.toml --write
"""

from __future__ import annotations

import argparse
import math
import os
import sys

from analyzer.collect import loadgen
from analyzer.collect.collector import (
    discover,
    measure,
    measure_failure_rates,
    write_measured,
)
from analyzer.collect.promql import Prometheus, PrometheusError
from analyzer.spec import load_system, measurements_path


def _fmt(value, unit="", nd=2) -> str:
    if value is None:
        return "  --  "
    return f"{value:.{nd}f}{unit}"


def _print_discovery(disc) -> None:
    print("Prometheus discovery")
    print("=" * 60)
    print(f"metrics known to Prometheus : {disc.metric_count}")
    print(f"request-count metric        : {disc.request_metric or 'NOT FOUND'}")
    if disc.latency:
        sel = f" (type filter: {disc.latency.type_selector})" \
            if disc.latency.type_selector else ""
        print(f"service-time metric         : {disc.latency.sum_metric}{sel}")
    else:
        print("service-time metric         : NOT FOUND")
    print(f"cAdvisor present            : {'yes' if disc.has_cadvisor else 'no'}")
    print(f"service label on metrics     : {disc.service_label}")
    print(f"services seen with traffic   : {disc.traffic_services or '(none)'}")
    print(f"services seen by cAdvisor    : {disc.compose_services or '(none)'}")
    if disc.notes:
        print("\nnotes:")
        for note in disc.notes:
            print(f"  - {note}")
    ready = disc.request_metric and disc.latency and disc.has_cadvisor
    print("\n" + ("Ready to measure. Re-run without --discover (add --write to "
                  "save)." if ready else
                  "Not all sources are available yet -- see notes above."))


def _print_measurements(rows, window) -> None:
    print(f"Measured Layer-1 inputs  (rate window: {window})")
    print("=" * 78)
    header = f"{'service':<18}{'c':>3}  {'lambda':>9}  {'mu/repl':>9}  " \
             f"{'rho':>6}  {'cpu/repl':>9}"
    print(header)
    print("-" * 78)
    for m in rows:
        c = str(m.replicas) if m.replicas is not None else "--"
        print(f"{m.name:<18}{c:>3}  {_fmt(m.arrival_rate, ' r/s'):>9}  "
              f"{_fmt(m.service_rate, ' r/s'):>9}  {_fmt(m.utilization):>6}  "
              f"{_fmt(m.per_replica_cpu, ' cr', 3):>9}")
    print("-" * 78)
    print("c = replicas | lambda = arrival rate | mu = service rate per replica")
    print("rho = lambda / (c * mu) (utilization) | cpu/repl = cores per replica")
    problems = [m for m in rows if not m.fully_measured or
                (m.utilization is not None and m.utilization >= 1.0)]
    if problems:
        print("\nattention:")
        for m in problems:
            for note in m.notes:
                print(f"  - {m.name}: {note}")


def main(argv=None) -> int:
    loadgen.load_dotenv()

    parser = argparse.ArgumentParser(
        prog="python3 -m analyzer.collect",
        description="Measure Layer-1 (M/M/c) inputs from Prometheus and write "
                    "the system's .measured.toml.")
    parser.add_argument("spec", help="path to the structural system spec "
                                      "(e.g. systems/finki-blogger.toml)")
    parser.add_argument("--prometheus", default="http://localhost:9090",
                        help="Prometheus base URL (default: %(default)s)")
    parser.add_argument("--window", default="5m",
                        help="PromQL rate window (default: %(default)s)")
    parser.add_argument("--timeout", type=float, default=10.0,
                        help="HTTP timeout seconds (default: %(default)s)")
    parser.add_argument("--discover", action="store_true",
                        help="only report what can be measured, then exit")
    parser.add_argument("--failures", action="store_true",
                        help="report observed failure_rate/h (restart+OOM "
                             "history) per component, then exit")
    parser.add_argument("--failure-window", default="7d",
                        help="observation window for --failures (default: "
                             "%(default)s)")
    parser.add_argument("--run-load", action="store_true",
                        help="run the spec's [load] k6 script first, then "
                             "measure over a window covering that run")
    parser.add_argument("--write", action="store_true",
                        help="write the .measured.toml (default: preview only)")
    args = parser.parse_args(argv)

    try:
        spec = load_system(args.spec)
    except (OSError, ValueError) as exc:
        print(f"error: could not load spec '{args.spec}': {exc}", file=sys.stderr)
        return 2

    prom = Prometheus(args.prometheus, timeout=args.timeout)

    try:
        if args.discover:
            _print_discovery(discover(prom, spec))
            return 0

        if args.failures:
            rates = measure_failure_rates(prom, spec, window=args.failure_window)
            print(f"Observed failure_rate/h from restart+OOM history "
                  f"(window: {args.failure_window})")
            print("=" * 60)
            for comp, rate in rates.items():
                shown = f"{rate:.5f}" if rate is not None else "unmeasured"
                print(f"  {comp:<18} {shown}")
            print("-" * 60)
            print("unmeasured = no restart/OOM history in the window (system "
                  "hasn't failed on its own yet).")
            print("NOTE: deliberate chaos (Phase 2) also counts as restarts -- "
                  "measure over a chaos-free window for a true failure rate.")
            return 0

        window = args.window
        if args.run_load:
            try:
                cfg = loadgen.load_config(spec, args.spec)
            except (ValueError, FileNotFoundError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            if cfg is None:
                print("error: --run-load needs a [load] section in the spec "
                      "(pointing at your own k6 script).", file=sys.stderr)
                return 2
            print(f"[load] running k6 script '{cfg['script']}' -- this blocks "
                  "until the load finishes...")
            run = loadgen.run_load(cfg, project_dir=os.getcwd())
            window = f"{max(1, math.ceil(run.seconds / 60))}m"
            print(f"[load] k6 finished in {run.seconds:.0f}s "
                  f"({'ok' if run.ok else f'exit {run.returncode}'}); "
                  f"measuring over the last {window}.")
            if not run.ok:
                print("[load] note: k6 reported errors -- expected under heavy "
                      "overload; requests still counted at the gateway.")

        rows = measure(prom, spec, window=window)
        _print_measurements(rows, window)

        if args.write:
            path = measurements_path(args.spec)
            write_measured(path, spec, rows)
            print(f"\nwrote {path}")
        else:
            print("\n(preview only -- re-run with --write to save the "
                  ".measured.toml)")
        return 0
    except PrometheusError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
