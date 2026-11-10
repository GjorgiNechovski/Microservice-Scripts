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


def test_no_measure_reanalyses_a_saved_sweep():
    """--no-measure on a sweep directory rebuilds the scaling report from the
    per-level measured.toml files, without touching any live system (no
    k6-output.txt here, so not even Prometheus is queried)."""
    if not os.path.exists(_SPEC):
        print(f"  (skip: {_SPEC} not found from cwd)")
        return
    import shutil
    import tempfile
    out = tempfile.mkdtemp()
    measured = _SPEC.replace(".toml", ".measured.toml")
    for vus in (50, 100):
        os.makedirs(os.path.join(out, f"load-{vus}"))
        shutil.copy(measured, os.path.join(out, f"load-{vus}", "measured.toml"))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main([_SPEC, "--no-measure", "--no-figures", "--out", out])
    assert rc == 0
    assert "re-analysing the saved sweep" in buf.getvalue()
    assert "LOAD-SWEEP SCALING REPORT" in buf.getvalue()
    assert "levels tested (virtual users): 50, 100" in buf.getvalue()
    for name in ("scaling-report.txt", "scaling-report-explained.txt"):
        assert os.path.getsize(os.path.join(out, name)) > 0, name
    assert not os.path.exists(os.path.join(out, "report.txt"))   # not a single run


def test_reanalysis_rederives_a_floor_for_a_service_that_kept_pace():
    """A sweep saved by the old rule took the top level's throughput as the
    capacity of a service that never levelled off, and so recommended one more
    replica. Re-analysing it must flag that rate as a floor and stop sizing."""
    import tempfile
    import tomllib
    out = tempfile.mkdtemp()
    spec_path = os.path.join(out, "k.toml")
    with open(spec_path, "w", encoding="utf-8") as fh:
        fh.write('name = "K"\nservices = ["api"]\ninfrastructure = []\n'
                 '[operations]\nuse = ["api"]\n')
    for vus, mu in ((200, 330.0), (800, 200.0), (1600, 40.0)):
        os.makedirs(os.path.join(out, f"load-{vus}"))
        with open(os.path.join(out, f"load-{vus}", "measured.toml"), "w",
                  encoding="utf-8") as fh:
            fh.write(f"[services.api]\nreplicas = 2\nfailure_rate = 0.02\n"
                     f"repair_rate = 12.0\narrival_rate = {vus * 0.59}\n"
                     f"service_rate = {mu}\nservice_rate_effective = 472.0\n")
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        rc = main([spec_path, "--no-measure", "--no-figures", "--out", out])
    assert rc == 0
    assert "no throughput plateau for api" in buf.getvalue()
    with open(os.path.join(out, "load-1600", "measured.toml"), "rb") as fh:
        api = tomllib.load(fh)["services"]["api"]
    assert api["service_rate_lower_bound"] is True
    assert abs(api["service_rate_effective"] - 1600 * 0.59 / 2) < 1e-3
    with open(os.path.join(out, "scaling-report.txt"), encoding="utf-8") as fh:
        report = fh.read()
    assert "Limit not reached" in report and "ADD REPLICAS" not in report
    with open(os.path.join(out, "load-1600", "report.txt"), encoding="utf-8") as fh:
        assert "kept up" in fh.read()

    # with k6's summaries saved next to each level, what the users saw is
    # judged too: the top level is felt, and that caps the comfortable load
    for vus, p95, failed in ((200, "15ms", "0.00"), (800, "35ms", "0.00"),
                             (1600, "207.64ms", "0.37")):
        with open(os.path.join(out, f"load-{vus}", "k6-output.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write(f"    http_req_duration..: avg=9ms min=1ms med=4ms max=2s "
                     f"p(90)=9ms p(95)={p95}\n"
                     f"    http_req_failed....: {failed}%  1 out of 100\n")
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        rc = main([spec_path, "--no-measure", "--no-figures", "--out", out,
                   "--prometheus", "http://127.0.0.1:1"])   # nothing to query
    assert rc == 0
    with open(os.path.join(out, "scaling-report.txt"), encoding="utf-8") as fh:
        assert "Highest comfortable load : 800 VUs (at 1600 VUs" in fh.read()
    with open(os.path.join(out, "load-1600", "report-explained.txt"),
              encoding="utf-8") as fh:
        assert "noticeably slower" in fh.read()


def test_reanalysis_marks_levels_past_the_limit_in_their_own_reports():
    """A sweep that collapsed: each level's own reports must say where it
    stood against the limit the sweep hit, not judge it alone (alone, the
    collapsed level reads as 'kept up', its traffic lowered by refusals)."""
    import tempfile
    out = tempfile.mkdtemp()
    spec_path = os.path.join(out, "k.toml")
    with open(spec_path, "w", encoding="utf-8") as fh:
        fh.write('name = "K"\nservices = ["api"]\ninfrastructure = []\n'
                 '[operations]\nuse = ["api"]\n')
    for vus, lam, mu in ((200, 118, 330.0), (800, 472, 200.0), (1600, 915, 40.0),
                         (2000, 420, 3.0)):
        os.makedirs(os.path.join(out, f"load-{vus}"))
        with open(os.path.join(out, f"load-{vus}", "measured.toml"), "w",
                  encoding="utf-8") as fh:
            fh.write(f"[services.api]\nreplicas = 1\nfailure_rate = 0.02\n"
                     f"repair_rate = 12.0\narrival_rate = {lam}\n"
                     f"service_rate = {mu}\n")
    with contextlib.redirect_stdout(io.StringIO()):
        rc = main([spec_path, "--no-measure", "--no-figures", "--out", out])
    assert rc == 0

    def read(vus, name):
        with open(os.path.join(out, f"load-{vus}", name), encoding="utf-8") as fh:
            return fh.read()

    assert "past its limit" in read(2000, "report-explained.txt")
    assert "every part kept up" not in read(2000, "report-explained.txt")
    assert "PAST the limit" in read(2000, "report.txt")
    assert "past the sweep's limit — not sized" in read(2000, "report.txt")
    assert "at its limit" in read(1600, "report-explained.txt")
    assert "kept up: about" in read(800, "report-explained.txt")


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
