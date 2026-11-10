"""Tests for the run-validity checklist (analyzer.validity).

  python3 -m pytest analyzer/tests/ -q
  python3 -m analyzer.tests.test_validity

No network: a fake Prometheus answers by substring, and the k6 summaries are
copies of real k6 output (v1.x layout, as captured on 2026-11-03, and the
older v0.x layout).
"""

from __future__ import annotations

from analyzer.collect.collector import Measurement
from analyzer.collect.promql import PrometheusError, Sample
from analyzer.scaling import LevelResult
from analyzer.validity import (
    FAIL,
    PASS,
    SKIP,
    WARN,
    K6Summary,
    check_run,
    duration_seconds,
    parse_k6_summary,
    planned_seconds,
    validity_explain,
    validity_report,
    validity_summary,
)

_SPEC = {"name": "V", "services": ["gateway", "api"], "infrastructure": ["db"]}
_COMPOSE = "container_label_com_docker_compose_service"

_K6_V1 = """
running (2m03.9s), 000/200 VUs, 4880 complete and 0 interrupted iterations
default ✓ [======================================] 200 VUs  2m0s

  █ THRESHOLDS

    http_req_failed
    ✓ 'rate<0.01' rate=0.00%

  █ TOTAL RESULTS

    checks_total.......: 15241   120.019635/s
    checks_succeeded...: 99.50% 15165 out of 15241
    checks_failed......: 0.49%  76 out of 15241

    HTTP
    http_req_duration..............: avg=21.9ms min=1.2ms med=8.1ms max=1.07s p(90)=40ms p(95)=96.17ms
    http_req_failed................: 0.20%  31 out of 15243
    http_reqs......................: 15243  120.019635/s
"""

_K6_V0 = """
running (0m45.0s), 00/16 VUs, 5422 complete and 0 interrupted iterations
     checks.........................: 100.00% ✓ 16533     ✗ 0
     http_req_failed................: 0.00%   ✓ 0         ✗ 16535
     http_reqs......................: 16535   366.474953/s
"""


class FakePrometheus:
    def __init__(self, rules, fail=False):
        self._rules = rules              # list[(substr, [Sample, ...])]
        self._fail = fail
        self.calls = []

    def instant(self, query, at=None):
        self.calls.append((query, at))
        if self._fail:
            raise PrometheusError("down")
        for substr, samples in self._rules:
            if substr in query:
                return samples
        return []


def _healthy_prom(up=1.0, cores=22.0, gateway_cpu=0.7, api_cpu=2.5):
    return FakePrometheus([
        ('up{job="cadvisor"}', [Sample({}, up)]),
        ("machine_cpu_cores", [Sample({}, cores)]),
        ("container_cpu_usage_seconds_total", [
            Sample({_COMPOSE: "gateway"}, gateway_cpu),
            Sample({_COMPOSE: "api"}, api_cpu),
        ]),
    ])


def _rows(gateway_rate=120.0, api_rate=118.0, api_mu=40.0):
    return [Measurement("gateway", 1, gateway_rate, 300.0),
            Measurement("api", 1, api_rate, api_mu)]


def _by_name(v):
    return {c.name: c for c in v.checks}


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def test_parse_k6_v1_summary():
    s = parse_k6_summary(_K6_V1)
    assert s.requests == 15243 and abs(s.rate - 120.019635) < 1e-6
    assert abs(s.failed - 0.002) < 1e-9
    assert abs(s.checks_passed - 0.995) < 1e-9
    assert abs(s.seconds - 123.9) < 1e-9


def test_parse_k6_v0_summary():
    s = parse_k6_summary(_K6_V0)
    assert s.requests == 16535 and abs(s.rate - 366.474953) < 1e-6
    assert s.failed == 0.0 and s.checks_passed == 1.0
    assert abs(s.seconds - 45.0) < 1e-9


def test_parse_k6_takes_the_last_progress_line():
    text = "running (0m01.0s), 1/1 VUs\nrunning (1m30.5s), 1/1 VUs\n"
    assert abs(parse_k6_summary(text).seconds - 90.5) < 1e-9


def test_parse_k6_empty_output_is_all_none():
    s = parse_k6_summary("level=error msg=\"setup() failed\"")
    assert (s.requests, s.rate, s.failed, s.checks_passed, s.seconds) == \
        (None, None, None, None, None)


def test_duration_seconds():
    assert duration_seconds("2m") == 120
    assert duration_seconds("90s") == 90
    assert duration_seconds("1m30s") == 90
    assert duration_seconds("1h") == 3600
    assert duration_seconds("soon") is None


def test_planned_seconds():
    assert planned_seconds({"vus": 200, "duration": "2m"}) == 120
    assert planned_seconds({"vus": 10}) == 300                  # analyzer default
    assert planned_seconds({"stages": [{"duration": "30s", "target": 5},
                                       {"duration": "1m", "target": 5}]}) == 90
    assert planned_seconds({"script": "x.js"}) is None          # script decides
    assert planned_seconds(None) is None


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def test_healthy_run_is_valid():
    k6 = parse_k6_summary(_K6_V1)
    v = check_run(_SPEC, _healthy_prom(), _rows(), k6, 120, "124s", "load @ 200 VUs")
    assert v.valid, validity_report(v)
    c = _by_name(v)
    assert c["load ran its full duration"].status == PASS
    assert c["k6 checks passed"].status == PASS
    assert c["requests succeeded"].status == PASS
    assert c["cAdvisor stayed up"].status == PASS
    assert c["every service measured"].status == PASS
    assert c["k6 rate matches measured λ"].status == PASS
    assert c["host not saturated"].status == PASS
    assert "api 2.50" in c["host not saturated"].detail
    assert v.verdict == "VALID"


def test_short_run_means_setup_failed():
    k6 = K6Summary(requests=10, rate=5.0, failed=0.0, checks_passed=1.0, seconds=4.0)
    v = check_run(_SPEC, _healthy_prom(), _rows(5.0, 5.0), k6, 120, "60s", "x")
    assert _by_name(v)["load ran its full duration"].status == FAIL
    assert not v.valid and v.verdict == "INVALID"


def test_missed_cadvisor_scrape_invalidates():
    v = check_run(_SPEC, _healthy_prom(up=0.93), _rows(), parse_k6_summary(_K6_V1),
                  120, "124s", "x")
    assert _by_name(v)["cAdvisor stayed up"].status == FAIL
    assert "93.0%" in _by_name(v)["cAdvisor stayed up"].detail


def test_unmeasured_service_invalidates_and_names_it():
    rows = [Measurement("gateway", 1, 120.0, 300.0), Measurement("api", 1, None, None)]
    v = check_run(_SPEC, _healthy_prom(), rows, parse_k6_summary(_K6_V1), 120, "1m", "x")
    c = _by_name(v)["every service measured"]
    assert c.status == FAIL and "api" in c.detail


def test_rate_disagreement_warns():
    v = check_run(_SPEC, _healthy_prom(), _rows(gateway_rate=90.0, api_rate=80.0),
                  parse_k6_summary(_K6_V1), 120, "124s", "x")
    c = _by_name(v)["k6 rate matches measured λ"]
    assert c.status == WARN and "120.0" in c.detail and "90.0" in c.detail
    assert v.valid and v.verdict == "VALID, with warnings"


def test_saturated_host_warns():
    v = check_run(_SPEC, _healthy_prom(cores=4.0, api_cpu=3.0), _rows(),
                  parse_k6_summary(_K6_V1), 120, "124s", "x")
    assert _by_name(v)["host not saturated"].status == WARN
    # the numbers behind the check are kept for the diagnosis
    assert v.host_cores == 4.0
    assert v.cpu_by_service == {"gateway": 0.7, "api": 3.0}


def test_error_and_check_thresholds():
    base = dict(requests=100, rate=120.0, seconds=120.0)
    warn = K6Summary(failed=0.03, checks_passed=0.97, **base)
    fail = K6Summary(failed=0.20, checks_passed=0.50, **base)
    vw = _by_name(check_run(_SPEC, _healthy_prom(), _rows(), warn, 120, "1m", "x"))
    vf = _by_name(check_run(_SPEC, _healthy_prom(), _rows(), fail, 120, "1m", "x"))
    assert vw["requests succeeded"].status == WARN
    assert vw["k6 checks passed"].status == WARN
    assert vf["requests succeeded"].status == FAIL
    assert vf["k6 checks passed"].status == FAIL


def test_failed_requests_mean_overloaded_not_invalid():
    # the test ran fine; the system failed 74.54% of requests: a result
    k6 = K6Summary(requests=100, rate=120.0, seconds=120.0, failed=0.7454,
                   checks_passed=0.2545)
    v = check_run(_SPEC, _healthy_prom(), _rows(), k6, 120, "1m", "load @ 2000 VUs")
    assert v.valid and v.overloaded and v.verdict == "OVERLOADED"
    assert v.failed_share == "74.54% of requests failed"
    rep = validity_report(v)
    assert "OVERLOADED: the test worked, but the system could not keep up " \
           "(74.54% of requests failed)" in rep
    assert "INVALID" not in rep and "do not report" not in rep
    plain = validity_explain(v)
    assert "couldn't keep up" in plain and "should not be used" not in plain
    # a broken test stays INVALID even when requests failed too
    short = K6Summary(requests=10, rate=5.0, seconds=4.0, failed=0.9,
                      checks_passed=0.1)
    broken = check_run(_SPEC, _healthy_prom(), _rows(5.0, 5.0), short, 120,
                       "60s", "x")
    assert broken.verdict == "INVALID" and not broken.overloaded
    assert "couldn't keep up" not in validity_explain(broken)


def test_unreachable_prometheus_skips_instead_of_crashing():
    v = check_run(_SPEC, FakePrometheus([], fail=True), _rows(),
                  parse_k6_summary(_K6_V1), 120, "124s", "x")
    c = _by_name(v)
    assert c["cAdvisor stayed up"].status == SKIP
    assert c["host not saturated"].status == SKIP
    assert v.valid                     # nothing measured as wrong


def test_missing_inputs_are_skipped_not_invented():
    v = check_run(_SPEC, FakePrometheus([]), _rows(), K6Summary(), None, "1m", "x")
    c = _by_name(v)
    for name in ("load ran its full duration", "k6 checks passed",
                 "requests succeeded", "cAdvisor stayed up",
                 "k6 rate matches measured λ", "host not saturated"):
        assert c[name].status == SKIP, name


def test_at_is_passed_to_every_query():
    prom = _healthy_prom()
    check_run(_SPEC, prom, _rows(), parse_k6_summary(_K6_V1), 120, "124s", "x",
              at="2026-11-03T12:23:38Z")
    assert prom.calls and all(at == "2026-11-03T12:23:38Z" for _, at in prom.calls)
    assert all("[124s]" in q for q, _ in prom.calls if "rate(" in q or "over_time" in q)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def test_reports_say_not_to_use_an_invalid_run():
    k6 = K6Summary(requests=10, rate=5.0, failed=0.0, checks_passed=1.0, seconds=4.0)
    v = check_run(_SPEC, _healthy_prom(), _rows(5.0, 5.0), k6, 120, "60s", "load @ 9 VUs")
    assert "INVALID" in validity_report(v) and "do not report" in validity_report(v)
    assert "should not be used" in validity_explain(v)


def test_scaling_summary_lists_every_level():
    good = check_run(_SPEC, _healthy_prom(), _rows(), parse_k6_summary(_K6_V1),
                     120, "124s", "a")
    bad = check_run(_SPEC, _healthy_prom(up=0.5), _rows(), parse_k6_summary(_K6_V1),
                    120, "124s", "b")
    levels = [LevelResult(200, {}, {}, {}, good), LevelResult(400, {}, {}, {}, bad)]
    text = validity_summary(levels)
    assert "200 VUs  VALID" in text and "400 VUs  INVALID" in text
    assert "must not be reported" in text
    assert validity_summary([LevelResult(1, {}, {}, {})]) == ""


def test_summaries_read_overload_as_a_result():
    from analyzer.capacity import size_services
    from analyzer.scaling import scaling_explain
    from analyzer.spec import analyze
    spec = {"name": "S", "services": ["gateway", "api"], "infrastructure": [],
            "operations": {}}

    def level(vus, failed):
        k6 = K6Summary(requests=100, rate=120.0, seconds=120.0, failed=failed,
                       checks_passed=1.0 - failed)
        v = check_run(_SPEC, _healthy_prom(), _rows(), k6, 120, "1m",
                      f"load @ {vus} VUs")
        meas = {"services": {n: {"replicas": 1, "arrival_rate": 120.0,
                                 "service_rate": 300.0, "failure_rate": 0.01,
                                 "repair_rate": 100.0} for n in spec["services"]},
                "infrastructure": {}}
        return LevelResult(vus, meas, analyze(spec, meas),
                           size_services(spec, meas, 0.7), v)

    levels = [level(200, 0.0), level(2000, 0.7454)]
    text = validity_summary(levels)
    assert "2000 VUs  OVERLOADED" in text and "failure rate is the result" in text
    assert "must not be reported" not in text and "lightest" not in text
    plain = " ".join(scaling_explain(spec, levels, 0.7).split())
    assert "every test level (2) worked as planned" in plain
    assert "couldn't keep up (2000 users: 74.54% of requests failed)" in plain
    assert "went wrong" not in plain
    # failures already at the lightest level: maybe the test, not the load
    light = [level(200, 0.5), level(400, 0.6)]
    assert "Even the lightest level failed" in validity_summary(light)
    assert "Even the lightest test failed" in " ".join(
        scaling_explain(spec, light, 0.7).split())


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
