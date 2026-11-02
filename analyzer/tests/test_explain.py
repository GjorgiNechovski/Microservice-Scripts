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
