"""Tests for Phase 1 collection (analyzer.collect).

Runs two ways:
  * with pytest:            python3 -m pytest analyzer/tests/ -q
  * with a bare interpreter: python3 -m analyzer.tests.test_collect

No network: a FakePrometheus returns canned series so the collector's
metric-resolution, lambda/mu/rho math, replica counting and TOML merge are
tested deterministically. The real promql client's JSON parsing is exercised
separately by patching urlopen.
"""

from __future__ import annotations

import json
import math
import tomllib

from analyzer.collect.collector import (
    Measurement,
    _resolve_latency,
    _resolve_service_label,
    _unit_to_seconds,
    _window_hours,
    discover,
    measure,
    measure_failure_rates,
    render_measured_toml,
)
from analyzer.collect.promql import Sample

REL = 1e-9


def _close(a, b, rel=REL):
    return math.isclose(a, b, rel_tol=rel, abs_tol=1e-12)


# ---------------------------------------------------------------------------
# A fake Prometheus that answers instant queries by substring match.
# ---------------------------------------------------------------------------

class FakePrometheus:
    def __init__(self, names, series_rules, label_values):
        self._names = set(names)
        self._rules = series_rules              # list[(substr, [Sample,...])]
        self._label_values = label_values       # dict[label -> [values]]

    def metric_names(self):
        return sorted(self._names)

    def label_values(self, label):
        return sorted(self._label_values.get(label, []))

    def instant(self, query):
        for substr, samples in self._rules:
            if substr in query:
                return samples
        return []


_SPEC = {
    "services": ["blog-service", "user-service", "email-service"],
    "infrastructure": ["kong", "rabbitmq"],
}

_COMPOSE = "container_label_com_docker_compose_service"


def _kong_and_cadvisor_prom():
    """A healthy system: Kong request+latency metrics and cAdvisor present.

    blog-service: lambda=30, mean upstream 50ms -> mu=20, c=3 -> rho=0.5
    user-service: lambda=12, mean upstream 125ms -> mu=8, c=3 -> rho=0.5
    email-service: no gateway traffic (message-driven) but 3 replicas + cpu.
    """
    names = [
        "kong_http_requests_total",
        "kong_upstream_latency_ms_sum",
        "kong_upstream_latency_ms_count",
        "kong_upstream_latency_ms_bucket",
        "container_last_seen",
        "container_cpu_usage_seconds_total",
    ]
    rules = [
        ("kong_http_requests_total", [
            Sample({"service": "blog-service"}, 30.0),
            Sample({"service": "user-service"}, 12.0),
        ]),
        # mean-latency query divides sum by count; match on the sum metric.
        ("kong_upstream_latency_ms_sum", [
            Sample({"service": "blog-service"}, 50.0),   # ms
            Sample({"service": "user-service"}, 125.0),  # ms
        ]),
        ("container_last_seen", [
            Sample({_COMPOSE: "blog-service"}, 3.0),
            Sample({_COMPOSE: "user-service"}, 3.0),
            Sample({_COMPOSE: "email-service"}, 3.0),
        ]),
        ("container_cpu_usage_seconds_total", [
            Sample({_COMPOSE: "blog-service"}, 1.5),
            Sample({_COMPOSE: "user-service"}, 0.6),
            Sample({_COMPOSE: "email-service"}, 0.3),
        ]),
    ]
    label_values = {
        "service": ["blog-service", "user-service"],
        _COMPOSE: ["blog-service", "user-service", "email-service"],
    }
    return FakePrometheus(names, rules, label_values)


# ---------------------------------------------------------------------------
# Metric resolution
# ---------------------------------------------------------------------------

def test_unit_detection():
    assert _unit_to_seconds("kong_upstream_latency_ms_sum") == 1e-3
    assert _unit_to_seconds("kong_upstream_latency_us_sum") == 1e-6
    assert _unit_to_seconds("kong_upstream_latency_seconds_sum") == 1.0
    assert _unit_to_seconds("http_server_request_duration_seconds_sum") == 1.0
    assert _unit_to_seconds("http_server_duration_milliseconds_sum") == 1e-3


def test_resolve_latency_prefers_upstream_histogram():
    names = {"kong_upstream_latency_ms_sum", "kong_upstream_latency_ms_count",
             "kong_request_latency_ms_sum", "kong_request_latency_ms_count"}
    lat = _resolve_latency(names)
    assert lat is not None
    assert "upstream" in lat.sum_metric
    assert lat.type_selector == ""
    assert lat.to_seconds == 1e-3


def test_resolve_latency_type_label_fallback():
    # No dedicated upstream metric -> fall back to a generic latency histogram
    # filtered by a type="upstream" label.
    names = {"kong_latency_ms_sum", "kong_latency_ms_count"}
    lat = _resolve_latency(names)
    assert lat is not None
    assert lat.type_selector == 'type="upstream"'


def test_resolve_latency_none_when_absent():
    assert _resolve_latency({"kong_http_requests_total"}) is None


def test_resolve_latency_otel_duration_histogram():
    names = {"http_server_request_duration_seconds_sum",
             "http_server_request_duration_seconds_count",
             "http_server_request_duration_seconds_bucket"}
    lat = _resolve_latency(names)
    assert lat is not None
    assert lat.sum_metric == "http_server_request_duration_seconds_sum"
    assert lat.type_selector == ""
    assert lat.to_seconds == 1.0


def test_resolve_latency_kong_upstream_beats_otel():
    # When both exist, the gateway's dedicated upstream histogram wins
    # (it excludes gateway overhead by construction).
    names = {"kong_upstream_latency_ms_sum", "kong_upstream_latency_ms_count",
             "http_server_request_duration_seconds_sum",
             "http_server_request_duration_seconds_count"}
    lat = _resolve_latency(names)
    assert lat is not None
    assert "upstream" in lat.sum_metric


# ---------------------------------------------------------------------------
# An OTel-instrumented system (no Kong): metrics arrive via the OTel
# collector's Prometheus exporter, and the service name lives in 'job'.
# ---------------------------------------------------------------------------

_OTEL_SPEC = {
    "services": ["gamecenterapi", "astral-match-server"],
    "infrastructure": ["redis"],
}


def _otel_and_cadvisor_prom():
    """gamecenterapi: lambda=40, mean 0.025s -> mu=40, c=2 -> rho=0.5
    astral-match-server: lambda=10, mean 0.1s -> mu=10, c=2 -> rho=0.5
    """
    names = [
        "http_server_request_duration_seconds_sum",
        "http_server_request_duration_seconds_count",
        "http_server_request_duration_seconds_bucket",
        "container_last_seen",
        "container_cpu_usage_seconds_total",
    ]
    # NOTE: the sum-metric rule must come FIRST -- the mean-duration query
    # contains both the _sum and _count metric names.
    rules = [
        ("http_server_request_duration_seconds_sum", [
            Sample({"job": "gamecenterapi"}, 0.025),          # seconds
            Sample({"job": "astral-match-server"}, 0.1),
        ]),
        ("http_server_request_duration_seconds_count", [
            Sample({"job": "gamecenterapi"}, 40.0),
            Sample({"job": "astral-match-server"}, 10.0),
        ]),
        ("container_last_seen", [
            Sample({_COMPOSE: "gamecenterapi"}, 2.0),
            Sample({_COMPOSE: "astral-match-server"}, 2.0),
        ]),
        ("container_cpu_usage_seconds_total", [
            Sample({_COMPOSE: "gamecenterapi"}, 1.0),
            Sample({_COMPOSE: "astral-match-server"}, 0.4),
        ]),
    ]
    label_values = {
        "job": ["gamecenterapi", "astral-match-server",
                "cadvisor", "prometheus"],
        _COMPOSE: ["gamecenterapi", "astral-match-server"],
    }
    return FakePrometheus(names, rules, label_values)


def test_service_label_resolution():
    prom = _otel_and_cadvisor_prom()
    assert _resolve_service_label(prom, _OTEL_SPEC) == "job"
    # a 'service' label whose values match the spec wins over 'job'...
    prom._label_values["service"] = ["gamecenterapi"]
    assert _resolve_service_label(prom, _OTEL_SPEC) == "service"
    # ...but unrelated 'service' values don't hijack resolution.
    prom._label_values["service"] = ["something-else"]
    assert _resolve_service_label(prom, _OTEL_SPEC) == "job"


def test_measure_from_otel_metrics():
    rows = {m.name: m for m in measure(_otel_and_cadvisor_prom(), _OTEL_SPEC)}

    gc = rows["gamecenterapi"]
    assert gc.replicas == 2
    assert _close(gc.arrival_rate, 40.0)
    assert _close(gc.service_rate, 40.0)        # 1 / 0.025 s
    assert _close(gc.utilization, 0.5)          # 40 / (2*40)
    assert gc.fully_measured

    am = rows["astral-match-server"]
    assert _close(am.service_rate, 10.0)        # 1 / 0.1 s
    assert _close(am.utilization, 0.5)          # 10 / (2*10)
    assert am.fully_measured


def test_discover_otel_reports_label_and_request_metric():
    disc = discover(_otel_and_cadvisor_prom(), _OTEL_SPEC)
    assert disc.request_metric == "http_server_request_duration_seconds_count"
    assert disc.latency is not None
    assert disc.service_label == "job"
    assert "gamecenterapi" in disc.traffic_services


# ---------------------------------------------------------------------------
# Measurement math
# ---------------------------------------------------------------------------

def test_measure_lambda_mu_rho():
    rows = {m.name: m for m in measure(_kong_and_cadvisor_prom(), _SPEC)}

    blog = rows["blog-service"]
    assert blog.replicas == 3
    assert _close(blog.arrival_rate, 30.0)
    assert _close(blog.service_rate, 20.0)      # 1000/50ms
    assert _close(blog.utilization, 0.5)        # 30 / (3*20)
    assert _close(blog.per_replica_cpu, 0.5)    # 1.5 cores / 3
    assert blog.fully_measured

    user = rows["user-service"]
    assert _close(user.service_rate, 8.0)       # 1000/125ms
    assert _close(user.utilization, 0.5)        # 12 / (3*8)


def test_measure_partial_service_gets_notes_not_crash():
    # email-service has replicas + cpu but no gateway lambda/mu.
    rows = {m.name: m for m in measure(_kong_and_cadvisor_prom(), _SPEC)}
    email = rows["email-service"]
    assert email.replicas == 3
    assert email.arrival_rate is None
    assert email.service_rate is None
    assert email.utilization is None
    assert not email.fully_measured
    assert any("lambda not measured" in n for n in email.notes)


def test_measure_overload_flagged():
    prom = _kong_and_cadvisor_prom()
    # Push blog-service lambda above capacity: c*mu = 60, so lambda=90 -> rho=1.5
    prom._rules[0] = ("kong_http_requests_total", [
        Sample({"service": "blog-service"}, 90.0)])
    rows = {m.name: m for m in measure(prom, _SPEC)}
    blog = rows["blog-service"]
    assert blog.utilization is not None and blog.utilization >= 1.0
    assert any("OVERLOADED" in n for n in blog.notes)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def test_discover_reports_sources_and_gaps():
    disc = discover(_kong_and_cadvisor_prom(), _SPEC)
    assert disc.request_metric == "kong_http_requests_total"
    assert disc.latency is not None
    assert disc.has_cadvisor
    assert disc.service_label == "service"
    assert "blog-service" in disc.traffic_services
    # email-service has no gateway traffic -> flagged in notes.
    assert any("email-service" in n for n in disc.notes)


def test_discover_flags_missing_stack():
    empty = FakePrometheus(["up"], [], {})
    disc = discover(empty, _SPEC)
    assert disc.request_metric is None
    assert disc.latency is None
    assert not disc.has_cadvisor
    assert len(disc.notes) >= 3


# ---------------------------------------------------------------------------
# Failure rate from restart/OOM history
# ---------------------------------------------------------------------------

def test_window_hours():
    assert _window_hours("7d") == 168.0
    assert _window_hours("24h") == 24.0
    assert _window_hours("30m") == 0.5
    assert _window_hours("bad") == 0.0


def test_measure_failure_rates_from_history():
    names = ["container_start_time_seconds", "container_oom_events_total"]
    rules = [
        ("changes(container_start_time_seconds", [
            Sample({_COMPOSE: "blog-service"}, 14.0),   # 14 restarts in window
        ]),
        ("increase(container_oom_events_total", [
            Sample({_COMPOSE: "blog-service"}, 2.0),     # + 2 OOM kills
        ]),
    ]
    prom = FakePrometheus(names, rules, {})
    spec = {"services": ["blog-service", "user-service"], "infrastructure": []}
    rates = measure_failure_rates(prom, spec, window="7d")
    assert _close(rates["blog-service"], 16.0 / 168.0, rel=1e-6)  # (14+2)/168h
    assert rates["user-service"] is None            # no history -> unmeasured


def test_measure_failure_rates_absent_metric_is_unmeasured():
    prom = FakePrometheus(["up"], [], {})
    spec = {"services": ["blog-service"], "infrastructure": ["kong"],
            "containers": {"kong": "api-gateway"}}
    rates = measure_failure_rates(prom, spec, window="7d")
    assert rates == {"blog-service": None, "kong": None}


# ---------------------------------------------------------------------------
# TOML emission / merge
# ---------------------------------------------------------------------------

def test_render_preserves_reliability_and_infra():
    existing = {
        "services": {
            "blog-service": {"replicas": 1, "failure_rate": 0.03,
                             "repair_rate": 12.0, "arrival_rate": 1.0,
                             "service_rate": 1.0},
        },
        "infrastructure": {
            "kong": {"failure_rate": 0.02, "repair_rate": 12.0},
            "rabbitmq": {"failure_rate": 0.01, "repair_rate": 10.0},
        },
    }
    rows = measure(_kong_and_cadvisor_prom(), _SPEC)
    text = render_measured_toml(_SPEC, rows, existing)
    parsed = tomllib.loads(text)

    blog = parsed["services"]["blog-service"]
    # measured values overwrite performance fields...
    assert blog["replicas"] == 3
    assert _close(blog["arrival_rate"], 30.0)
    assert _close(blog["service_rate"], 20.0)
    # ...but Phase-2 reliability fields are preserved.
    assert _close(blog["failure_rate"], 0.03)
    assert _close(blog["repair_rate"], 12.0)
    # infrastructure preserved verbatim.
    assert _close(parsed["infrastructure"]["rabbitmq"]["failure_rate"], 0.01)


def test_render_falls_back_when_unmeasured():
    # No existing file, email-service unmeasured -> fallbacks, still valid TOML.
    rows = measure(_kong_and_cadvisor_prom(), _SPEC)
    text = render_measured_toml(_SPEC, rows, existing=None)
    parsed = tomllib.loads(text)
    email = parsed["services"]["email-service"]
    assert email["replicas"] == 3                 # this one WAS measured
    assert "arrival_rate" in email and "service_rate" in email
    # every service present and parseable
    assert set(parsed["services"]) == set(_SPEC["services"])


def test_render_emits_effective_service_rate():
    rows = measure(_kong_and_cadvisor_prom(), _SPEC)
    text = render_measured_toml(_SPEC, rows, existing=None,
                                effective={"blog-service": 273.0})
    parsed = tomllib.loads(text)
    assert _close(parsed["services"]["blog-service"]["service_rate_effective"],
                  273.0)
    # services without an effective entry simply omit the field
    assert "service_rate_effective" not in parsed["services"]["user-service"]


def test_render_preserves_existing_effective_rate():
    existing = {"services": {"blog-service": {
        "replicas": 3, "failure_rate": 0.02, "repair_rate": 12.0,
        "arrival_rate": 30.0, "service_rate": 20.0,
        "service_rate_effective": 99.0}}, "infrastructure": {}}
    rows = measure(_kong_and_cadvisor_prom(), _SPEC)
    text = render_measured_toml(_SPEC, rows, existing=existing)  # no new effective
    parsed = tomllib.loads(text)
    # preserved from the existing file when not freshly supplied
    assert _close(parsed["services"]["blog-service"]["service_rate_effective"],
                  99.0)


def test_render_can_refuse_to_inherit_a_stale_effective_rate():
    # The sweep's preliminary pass must judge saturation on THIS run's mu, so
    # it asks for fresh values only. A previous run's effective rate would
    # otherwise drive the verdict and mask a regression.
    existing = {"services": {"blog-service": {
        "replicas": 3, "failure_rate": 0.02, "repair_rate": 12.0,
        "arrival_rate": 30.0, "service_rate": 20.0,
        "service_rate_effective": 99.0}}, "infrastructure": {}}
    rows = measure(_kong_and_cadvisor_prom(), _SPEC)
    text = render_measured_toml(_SPEC, rows, existing=existing,
                                inherit_effective=False)
    parsed = tomllib.loads(text)
    assert "service_rate_effective" not in parsed["services"]["blog-service"]
    # a freshly derived value still lands even when inheritance is off
    text = render_measured_toml(_SPEC, rows, existing=existing,
                                effective={"blog-service": 7.5},
                                inherit_effective=False)
    assert _close(tomllib.loads(text)["services"]["blog-service"]
                  ["service_rate_effective"], 7.5)


def test_emitted_toml_is_wellformed():
    rows = measure(_kong_and_cadvisor_prom(), _SPEC)
    text = render_measured_toml(_SPEC, rows, existing=None)
    tomllib.loads(text)  # raises if malformed


# ---------------------------------------------------------------------------
# Real promql JSON parsing (no network: patch urlopen)
# ---------------------------------------------------------------------------

def test_promql_instant_parses_and_skips_nan():
    from analyzer.collect import promql

    payload = {
        "status": "success",
        "data": {"resultType": "vector", "result": [
            {"metric": {"service": "blog-service"}, "value": [1.0, "30"]},
            {"metric": {"service": "bad"}, "value": [1.0, "NaN"]},
        ]},
    }

    class _Resp:
        def __init__(self, b): self._b = b
        def read(self, *a): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    orig = promql.urllib.request.urlopen
    promql.urllib.request.urlopen = lambda url, timeout=0: _Resp(
        json.dumps(payload).encode())
    try:
        client = promql.Prometheus("http://x:9090")
        samples = client.instant("whatever")
    finally:
        promql.urllib.request.urlopen = orig

    assert len(samples) == 1                      # NaN row skipped
    assert samples[0].labels["service"] == "blog-service"
    assert _close(samples[0].value, 30.0)


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
