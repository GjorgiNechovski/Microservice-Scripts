"""Tests for measuring what extra copies really added (analyzer.scaleout).

Two synthetic sweeps of one system, modelled on a measured case: one copy each
of a gateway and an API peaked at ~922 req/s; two copies each at ~1297, so the
second copy added 375 req/s, 41% of a full copy.

  python3 -m pytest analyzer/tests/ -q
  python3 -m analyzer.tests.test_scaleout
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile

from analyzer.capacity import size_services
from analyzer.scaleout import measure_scaleout, scaleout_explain, scaleout_report
from analyzer.scaling import LevelResult, scaling_explain, scaling_report
from analyzer.spec import analyze

_TARGET = 0.7
_SPEC = {"name": "P", "services": ["gw", "api"], "infrastructure": [],
         "operations": {}}

# (vus, req/s, latency service rate): keeps pace, peaks, then collapses
_ONE = ((200, 121.0, 330.0), (800, 494.0, 211.0), (1600, 922.0, 12.0),
        (2000, 407.0, 0.4))
_TWO = ((200, 114.0, 330.0), (800, 493.0, 240.0), (1600, 946.0, 80.0),
        (2400, 1297.0, 6.0), (3200, 1288.0, 1.2))
# never levels off: the peak is only a floor
_TWO_NO_PLATEAU = ((200, 114.0, 330.0), (800, 493.0, 240.0), (1600, 946.0, 80.0))


def _levels(rows, copies):
    out = []
    for vus, lam, mu in rows:
        meas = {"services": {n: {"replicas": copies, "arrival_rate": lam,
                                 "service_rate": mu, "failure_rate": 0.01,
                                 "repair_rate": 100.0} for n in ("gw", "api")},
                "infrastructure": {}}
        out.append(LevelResult(vus, meas, analyze(_SPEC, meas),
                               size_services(_SPEC, meas, _TARGET)))
    return out


def test_the_gain_per_copy_is_measured_and_sizes_copies():
    so = measure_scaleout(_SPEC, _levels(_ONE, 1), _levels(_TWO, 2), _TARGET,
                          "one", "two")
    assert so.bound == "measured" and so.usable
    assert so.current is so.fewer and so.fewer.peak_vus == 1600
    g = so.gains["api"]
    assert abs(g.gain_per_copy - 375.0) < 1e-9
    assert abs(g.efficiency - 375.0 / 922.0) < 1e-9
    # to serve 922 req/s at 70%: 1317 needed, 922 there, +375 per copy -> 3
    # (a full copy each would say 2)
    assert so.copies_for("api", 922.0, _TARGET) == 3
    # from the larger sweep: 1297 / 0.7 = 1853 needed -> 2 + 2 = 4
    back = measure_scaleout(_SPEC, _levels(_TWO, 2), _levels(_ONE, 1), _TARGET,
                            "two", "one")
    assert back.current is back.more and back.copies_for("api", 1297.0,
                                                         _TARGET) == 4
    tech = scaleout_report(so, _SPEC, 0.55)
    assert "added 375 req/s each = 41% of a full copy (922 req/s)" in tech
    assert "55% of the host's cores" in tech
    plain = scaleout_explain(so, _SPEC)
    assert "With 1 copy of gw and api" in plain and "41%" in plain
    assert not so.notes


def test_bounds_when_a_sweep_never_levelled_off():
    so = measure_scaleout(_SPEC, _levels(_ONE, 1),
                          _levels(_TWO_NO_PLATEAU, 2), _TARGET, "one", "two")
    assert so.bound == "at least" and so.usable
    assert any("floor" in n for n in so.notes)
    assert "≥" in scaleout_report(so, _SPEC)
    unknown = measure_scaleout(_SPEC, _levels(_TWO_NO_PLATEAU, 1),
                               _levels(_TWO, 2), _TARGET, "one", "two")
    assert unknown.bound == "unknown" and not unknown.usable
    assert unknown.copies_for("api", 922.0, _TARGET) is None
    assert "can't tell what the extra copies added" in scaleout_explain(
        unknown, _SPEC)


def test_same_copies_or_different_traffic():
    assert measure_scaleout(_SPEC, _levels(_ONE, 1), _levels(_ONE, 1), _TARGET,
                            "a", "b") is None
    busier = tuple((v, lam * 1.4, mu) for v, lam, mu in _TWO)
    so = measure_scaleout(_SPEC, _levels(_ONE, 1), _levels(busier, 2), _TARGET,
                          "one", "two")
    assert any("same traffic per user" in n for n in so.notes)
    assert "Careful:" in scaleout_explain(so, _SPEC)


def test_scaling_reports_use_the_measured_gain():
    one, two = _levels(_ONE, 1), _levels(_TWO, 2)
    so = measure_scaleout(_SPEC, one, two, _TARGET, "one", "two")
    text = scaling_report(_SPEC, one, _TARGET, scaleout=so)
    assert "To serve the most it handled (1600 VUs, ~922 req/s) at target, " \
           "run: gw=3, api=3" in text
    assert "full copies would need gw=2, api=2" in text
    assert "Measured scale-out" in text
    plain = scaling_explain(_SPEC, one, _TARGET, scaleout=so)
    assert "run: 3 × gw, 3 × api." in plain
    assert "each copy as a full one it would be 2 × gw, 2 × api" in plain
    assert "DID MORE COPIES HELP AS MUCH AS EXPECTED?" in plain
    # without the other sweep: the full-copy assumption, as before
    assert "run: gw=2, api=2" in scaling_report(_SPEC, one, _TARGET)


def _save_sweep(rows, copies):
    out = tempfile.mkdtemp()
    for vus, lam, mu in rows:
        d = os.path.join(out, f"load-{vus}")
        os.makedirs(d)
        with open(os.path.join(d, "measured.toml"), "w", encoding="utf-8") as fh:
            for n in ("gw", "api"):
                fh.write(f"[services.{n}]\nreplicas = {copies}\nfailure_rate = 0.01\n"
                         f"repair_rate = 100.0\narrival_rate = {lam}\n"
                         f"service_rate = {mu}\n\n")
    return out


def test_pipeline_compares_against_another_saved_sweep():
    from analyzer.__main__ import main
    spec_dir = tempfile.mkdtemp()
    spec_path = os.path.join(spec_dir, "p.toml")
    with open(spec_path, "w", encoding="utf-8") as fh:
        fh.write('name = "P"\nservices = ["gw", "api"]\ninfrastructure = []\n'
                 '[operations]\nuse = ["gw", "api"]\n')
    one, two = _save_sweep(_ONE, 1), _save_sweep(_TWO, 2)
    with contextlib.redirect_stdout(io.StringIO()):
        rc = main([spec_path, "--no-measure", "--no-figures", "--out", one,
                   "--compare-replicas", two])
    assert rc == 0

    def read(*parts):
        with open(os.path.join(one, *parts), encoding="utf-8") as fh:
            return fh.read()

    tech = read("scaling-report.txt")
    assert "run: gw=3, api=3" in tech
    # every section counts copies the same way: no full-copy leftovers
    assert "lifts the ceiling by ~375.0 req/s (measured: 41%" in tech
    assert "sizing suggests ~3 replicas" in tech
    assert "~922.0 req/s" not in tech and "~2 replicas" not in tech
    plain = " ".join(read("scaling-report-explained.txt").split())
    assert "41% of what one copy handled" in plain
    assert "want 3 copies" in plain and "want 2 copies" not in plain
    # and so does the level at the limit
    assert "AT the sweep's limit — 3 for headroom at this load (measured gain)" \
        in read("load-1600", "report.txt")

    # from the larger sweep the yardstick is still a full copy of the smaller
    # one: every percentage reads 41%, never 375 / (1297 / 2) = 58%
    import re
    with contextlib.redirect_stdout(io.StringIO()):
        assert main([spec_path, "--no-measure", "--no-figures", "--out", two,
                     "--compare-replicas", one]) == 0
    with open(os.path.join(two, "scaling-report.txt"), encoding="utf-8") as fh:
        tech = fh.read()
    with open(os.path.join(two, "scaling-report-explained.txt"),
              encoding="utf-8") as fh:
        plain = " ".join(fh.read().split())
    pcts = set(re.findall(r"(\d+)% of (?:a full|what one copy)", tech + plain))
    assert pcts == {"41"}, pcts
    assert "run: gw=4, api=4" in tech


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
