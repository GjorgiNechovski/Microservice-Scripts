"""Smoke test for the optional figures module (analyzer.figures).

Skips cleanly if matplotlib isn't installed, so the core test suite keeps its
zero-dependency guarantee. When matplotlib IS present, it renders every figure
into a temp dir and checks non-empty files come out -- exercising all figure
functions and the analyze() glue end to end.

  python3 -m pytest analyzer/tests/ -q
  python3 -m analyzer.tests.test_figures
"""

from __future__ import annotations

import os
import tempfile

try:
    import matplotlib  # noqa: F401
    _HAVE_MPL = True
except ImportError:
    _HAVE_MPL = False


def test_generate_all_smoke():
    if not _HAVE_MPL:
        print("  (matplotlib not installed -- skipping figures smoke test)")
        return
    from analyzer.figures.plots import generate_all

    spec = {
        "name": "smoke", "services": ["svc-a", "svc-b"],
        "infrastructure": ["db", "gw"],
        "operations": {"op": ["gw", "svc-a", "db"]},
        "failure_rates": {"default": 0.001},
    }
    meas = {
        "services": {n: {"replicas": 3, "failure_rate": 0.001,
                         "repair_rate": 100.0, "arrival_rate": 5.0,
                         "service_rate": 20.0} for n in ("svc-a", "svc-b")},
        "infrastructure": {n: {"failure_rate": 0.001, "repair_rate": 100.0}
                           for n in ("db", "gw")},
    }
    with tempfile.TemporaryDirectory() as outdir:
        paths = generate_all(spec, meas, outdir)
        assert len(paths) >= 5
        for p in paths:
            assert os.path.exists(p) and os.path.getsize(p) > 0


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed"
          + (" -- ALL GREEN" if failed == 0 else f" -- {failed} FAILED"))
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    sys.exit(_run_all())
