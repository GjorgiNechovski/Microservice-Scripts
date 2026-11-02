"""Generate the thesis figures for a system.

    python3 -m analyzer.figures systems/finki-blogger.toml            # -> figures/
    python3 -m analyzer.figures systems/finki-blogger.toml --out out/

Reads the spec + its measured file and renders one PNG per analysis figure.
Requires matplotlib + numpy (the only third-party deps in the project, and only
for this command).
"""

from __future__ import annotations

import argparse
import sys

from analyzer.figures.plots import generate_all
from analyzer.spec import load_or_default_measurements, load_system


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m analyzer.figures",
        description="Render analysis figures (matplotlib) for a system.")
    parser.add_argument("spec", help="structural system spec path")
    parser.add_argument("--out", default="analysis",
                        help="output directory (default: %(default)s)")
    args = parser.parse_args(argv)

    try:
        spec = load_system(args.spec)
    except (OSError, ValueError) as exc:
        print(f"error: could not load spec '{args.spec}': {exc}", file=sys.stderr)
        return 2

    measurements, source = load_or_default_measurements(args.spec, spec)
    print(f"measurements: {source}")
    paths = generate_all(spec, measurements, args.out)
    print(f"wrote {len(paths)} figures to {args.out}/:")
    for p in paths:
        print(f"  {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
