"""Tests for the plain-language report (analyzer.explain).

Checks the narrative has all its sections, leaks no jargon, and never prints a
nonsensical >100% "busy" number for an overloaded service.

  python3 -m pytest analyzer/tests/ -q
  python3 -m analyzer.tests.test_explain
"""

from __future__ import annotations

import re

from analyzer.explain import _busy_phrase, _human_downtime, explain
from analyzer.spec import analyze


def _analyze(replicas=3, arrival=5.0, service=20.0):
    spec = {"name": "T", "services": ["svc"], "infrastructure": ["db"],
            "operations": {"do a thing": ["svc", "db"]},
            "failure_rates": {"default": 0.001}}
    meas = {"services": {"svc": {"replicas": replicas, "failure_rate": 0.001,
                                 "repair_rate": 100.0, "arrival_rate": arrival,
                                 "service_rate": service}},
            "infrastructure": {"db": {"failure_rate": 0.001,
                                      "repair_rate": 100.0}}}
    return spec, meas, analyze(spec, meas)


def test_all_sections_present():
    spec, meas, res = _analyze()
    text = explain(res, spec, meas)
    for section in ("PLAIN-LANGUAGE SUMMARY", "THE HEADLINE",
                    "IS IT FAST ENOUGH", "IS IT RELIABLE",
                    "HOW FAST DOES IT RECOVER", "WHAT TO FIX FIRST",
                    "IN ONE SENTENCE"):
        assert section in text


def test_no_jargon_leaks():
    spec, meas, res = _analyze()
    text = explain(res, spec, meas)
    for jargon in ("rho", "nines", "MTTR", "MTTF", "M/M/c", "Birnbaum",
                   "utilization", "Erlang"):
        assert jargon not in text, f"jargon leaked: {jargon}"


def test_overloaded_has_no_absurd_percentage():
    # single replica, arrival >> capacity -> overloaded
    spec, meas, res = _analyze(replicas=1, arrival=100.0, service=5.0)
    text = explain(res, spec, meas)
    assert "OVERLOADED" in text
    # no "busy" percentage above 100 anywhere
    for pct in re.findall(r"about (\d+)% busy", text):
        assert int(pct) <= 100


def test_mixed_and_single_replica_services():
    spec = {"name": "M", "services": ["blog", "comment"],
            "infrastructure": ["db"],
            "operations": {"op": ["blog", "comment", "db"]},
            "failure_rates": {"default": 0.01}}
    meas = {"services": {
                "blog": {"replicas": 3, "failure_rate": 0.01,
                         "repair_rate": 100.0, "arrival_rate": 5.0,
                         "service_rate": 20.0},
                "comment": {"replicas": 1, "failure_rate": 0.01,
                            "repair_rate": 100.0, "arrival_rate": 5.0,
                            "service_rate": 20.0}},
            "infrastructure": {"db": {"failure_rate": 0.01,
                                      "repair_rate": 100.0}}}
    text = explain(analyze(spec, meas), spec, meas)
    assert "blog: 3 copies" in text                 # per-service count, not uniform
    assert "comment (only 1 copy)" in text          # single-copy flagged as weak
    assert "each service runs" not in text          # buggy uniform phrasing gone
    assert "comment[1]" not in text                 # headline names are clean
    assert "comment" in text                        # ...but comment is named a SPOF


def test_human_downtime_bands():
    assert "second" in _human_downtime(0.2)          # sub-minute
    assert "minute" in _human_downtime(30)           # tens of minutes
    assert "hours" in _human_downtime(600)           # 10 hours
    assert "never" in _human_downtime(0.0)           # zero


def test_busy_phrase():
    assert "OVERLOADED" in _busy_phrase(1.5, False)
    assert "OVERLOADED" in _busy_phrase(0.5, False)  # unstable flag wins
    assert "headroom" in _busy_phrase(0.4, True) or "relaxed" in _busy_phrase(0.4, True)
    assert "limit" in _busy_phrase(0.95, True)


def test_unmeasured_recovery_is_never_called_measured():
    spec, meas, res = _analyze()
    meas["services"]["svc"]["repair_rate_measured"] = False
    meas["infrastructure"]["db"]["repair_rate_measured"] = False
    text = explain(res, spec, meas)
    assert "actually stopped" not in text
    assert "NOT MEASURED" in text
    assert "illustrations, not results" in text

    spec, meas, res = _analyze()
    meas["infrastructure"]["db"]["repair_rate_measured"] = False
    text = explain(res, spec, meas)
    assert "actually stopped" not in text
    assert re.search(r"db: back in .*\(not measured\)", text)
    assert not re.search(r"svc: back in .*\(not measured\)", text)


def test_a_floor_rate_is_not_called_overloaded():
    # rho = 5 / (3 * 1/3) = 5 against the floor -> but the sweep saw it keep up
    spec, meas, _ = _analyze(arrival=5.0, service=1.0)
    meas["services"]["svc"].update(service_rate_effective=5.0 / 3,
                                   service_rate_lower_bound=True)
    res = analyze(spec, meas)
    assert res["services"]["svc"].mu_lower_bound
    text = explain(res, spec, meas)
    assert "kept up with this traffic" in text
    assert "These are overloaded" not in text and "URGENT" not in text

    import contextlib
    import io

    from analyzer.run_all import report
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        report(res, "test")
    assert "kept up" in buf.getvalue() and "False" not in buf.getvalue()


def test_users_feeling_the_load_overrides_fast_enough():
    from analyzer.responsiveness import judge_levels
    from analyzer.validity import K6Summary
    spec, meas, res = _analyze()                        # light, keeps up
    assert "fast enough for its traffic" in explain(res, spec, meas)
    feel = judge_levels(spec, {100: K6Summary(p95_ms=40.0, failed=0.0),
                               400: K6Summary(p95_ms=210.0, failed=0.004)})[400]
    text = explain(res, spec, meas, feel)
    assert "fast enough" not in text and "needs no fix" not in text
    assert "keeping up but noticeably slower for its users" in text
    assert "95 of every 100 responses came back within 210 ms" in text
    for jargon in ("rho", "utilization", "p95", "M/M/c"):
        assert jargon not in text, f"jargon leaked: {jargon}"


def test_a_level_past_the_sweeps_limit_reads_overloaded():
    # past the limit the per-level math reads "fine" (failed requests lower the
    # traffic it divides); the sweep's position must win, and with a hidden
    # limit it must not prescribe copies
    from analyzer.diagnose import sweep_position
    from analyzer.tests.test_diagnose import _TARGET, _plateau_levels
    spec, levels = _plateau_levels()
    pos = sweep_position(levels, spec, _TARGET)
    lv = levels[-1]                                      # 2000 users, past
    text = explain(lv.results, spec, lv.measurements,
                   position=pos.at(lv.vus, lv.measurements, spec))
    assert "past its limit" in text and "turning work away" in text
    assert "URGENT" not in text and "more capacity for" not in text
    assert "not because the" in text
    assert "is past its limit at this load" in text      # the one sentence
    low = levels[0]
    text = explain(low.results, spec, low.measurements,
                   position=pos.at(low.vus, low.measurements, spec))
    assert "kept up: about 13% of the most the system handled" in text
    assert "fast enough for its traffic" in text


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
