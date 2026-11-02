"""Phase 2 CLI: measure repair_rate / failure_rate by failure injection.

SAFE BY DEFAULT: with no ``--run`` it only PLANS (shows what it would kill).
``--run`` actually kills containers and times recovery -- brief real outages
for single-instance components.

    # See the plan (no kills):
    python3 -m analyzer.chaos systems/finki-blogger.toml

    # Inject only the redundant app services (safe: replicas survive):
    python3 -m analyzer.chaos systems/finki-blogger.toml --run \\
        --components blog-service,comment-service,like-service,user-service,email-service

    # Inject everything and write the reliability numbers:
    python3 -m analyzer.chaos systems/finki-blogger.toml --run --write
"""

from __future__ import annotations

import argparse
import sys

from analyzer.chaos.dockercli import Docker, DockerError
from analyzer.chaos.injector import (
    component_to_service,
    measure_mttr,
    plan,
    readiness_port,
    resolve_containers,
    write_measured,
)
from analyzer.spec import load_system, measurements_path


def _print_plan(plans) -> None:
    print("Failure-injection plan (no containers touched)")
    print("=" * 74)
    print(f"{'component':<16}{'compose service':<18}{'run':>3}  "
          f"{'restart':<14}{'health':<7}{'impact'}")
    print("-" * 74)
    for p in plans:
        impact = "OUTAGE on kill" if p.outage_on_kill else "redundant (safe)"
        if not p.containers:
            impact = p.note or "no containers"
        print(f"{p.component:<16}{p.service:<18}{p.running:>3}  "
              f"{p.restart_policy:<14}{'yes' if p.has_healthcheck else 'no':<7}"
              f"{impact}")
    print("-" * 74)
    print("Pass --run to inject (kills a container and times recovery).")
    print("Single-instance components incur a brief real outage while they "
          "restart.")


def _print_results(results) -> None:
    print("Measured recovery (MTTR -> repair_rate)")
    print("=" * 74)
    print(f"{'component':<16}{'MTTR(s)':>9}{'repair/h':>10}{'fail/h':>10}"
          f"{'auto':>6}{'reps':>6}")
    print("-" * 74)
    for res in results:
        mttr = f"{res.mean_seconds:.1f}" if res.mean_seconds is not None else "--"
        rep = f"{res.repair_rate_per_hour:.1f}" \
            if res.repair_rate_per_hour is not None else "--"
        fail = f"{res.observed_failure_rate:.4f}" \
            if res.observed_failure_rate is not None else "prior"
        auto = "yes" if res.auto_restarts else "no"
        print(f"{res.component:<16}{mttr:>9}{rep:>10}{fail:>10}{auto:>6}"
              f"{len(res.samples):>6}")
    print("-" * 74)
    print("MTTR = mean kill->ready seconds | repair/h = 3600/MTTR")
    print("fail/h = restarts over lifetime ('prior' = none seen, assumption kept)")
    problems = [r for r in results if r.notes]
    if problems:
        print("\nnotes:")
        for r in problems:
            for n in r.notes:
                print(f"  - {r.component}: {n}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m analyzer.chaos",
        description="Measure repair_rate/failure_rate by failure injection.")
    parser.add_argument("spec", help="structural system spec path")
    parser.add_argument("--run", action="store_true",
                        help="actually inject (default: dry-run plan only)")
    parser.add_argument("--components", default="",
                        help="comma-separated subset (default: all in spec)")
    parser.add_argument("--repetitions", type=int, default=3,
                        help="kills per component (default: %(default)s)")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="per-recovery timeout seconds (default: %(default)s)")
    parser.add_argument("--write", action="store_true",
                        help="write measured.toml (reliability fields)")
    args = parser.parse_args(argv)

    try:
        spec = load_system(args.spec)
    except (OSError, ValueError) as exc:
        print(f"error: could not load spec '{args.spec}': {exc}", file=sys.stderr)
        return 2

    docker = Docker()
    selected = {c.strip() for c in args.components.split(",") if c.strip()}

    try:
        if not args.run:
            plans = plan(spec, docker)
            if selected:
                plans = [p for p in plans if p.component in selected]
            _print_plan(plans)
            return 0

        containers = resolve_containers(spec, docker)
        order = list(component_to_service(spec))
        if selected:
            order = [c for c in order if c in selected]

        results = []
        for comp in order:
            print(f"injecting {comp} ...", file=sys.stderr)
            results.append(measure_mttr(docker, comp, containers.get(comp, []),
                                        repetitions=args.repetitions,
                                        timeout=args.timeout,
                                        port=readiness_port(spec, comp)))
        _print_results(results)

        if args.write:
            path = measurements_path(args.spec)
            write_measured(path, spec, {r.component: r for r in results})
            print(f"\nwrote {path}")
        else:
            print("\n(preview only -- re-run with --write to save reliability "
                  "numbers)")
        return 0
    except DockerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
