"""Smoke test for the single-command pipeline (analyzer.__main__).

Exercises the analyze-only path with --no-measure (NEVER the destructive
default, which would stress-test + kill live containers): load the spec, run all
four analysis layers, render the report. The live --measure path needs a running
system and isn't unit-tested here.

  python3 -m pytest analyzer/tests/ -q
  python3 -m analyzer.tests.test_pipeline
"""

from __future__ import annotations

import contextlib
import io
import os

from analyzer.__main__ import main

_SPEC = "systems/finki-blogger.toml"


def test_pipeline_default_reports_all_layers():
    if not os.path.exists(_SPEC):
        print(f"  (skip: {_SPEC} not found from cwd)")
        return
    import tempfile
    out = tempfile.mkdtemp()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main([_SPEC, "--no-measure", "--no-figures", "--out", out])
    text = buf.getvalue()
    assert rc == 0
    assert "ANALYSIS REPORT" in text
    assert "Layer 1" in text and "Layer 2" in text
    assert "where to invest" in text
    # the report is also written to a file
    assert os.path.exists(os.path.join(out, "report.txt"))


def test_pipeline_bad_spec_returns_2():
    with contextlib.redirect_stderr(io.StringIO()):
        rc = main(["does-not-exist.toml", "--no-measure", "--no-figures"])
    assert rc == 2


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
