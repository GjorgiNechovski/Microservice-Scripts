"""Tests for Phase 2 failure injection (analyzer.chaos).

Runs two ways:
  * with pytest:            python3 -m pytest analyzer/tests/ -q
  * with a bare interpreter: python3 -m analyzer.tests.test_chaos

No Docker: a FakeDocker simulates container lifecycle (kill/start, auto-restart,
health, restart counts) so MTTR timing, failure-rate derivation and the TOML
merge are tested deterministically and fast (state transitions are instant, so
the poll loops return without sleeping).
"""

from __future__ import annotations

import math
import tomllib
from datetime import datetime, timedelta, timezone

from analyzer.chaos.dockercli import ContainerState
from analyzer.chaos.injector import (
    component_to_service,
    measure_mttr,
    observed_failure_rate,
    plan,
    render_measured_toml,
    resolve_containers,
    verify_recovered,
)

REL = 1e-9


def _close(a, b, rel=REL):
    return math.isclose(a, b, rel_tol=rel, abs_tol=1e-9)


class _FakeContainer:
    def __init__(self, name, policy="no", health=None, restart_count=0,
                 created="2026-07-10T00:00:00Z", running=True):
        self.name = name
        self.policy = policy
        self.health = health
        self.restart_count = restart_count
        self.created = created
        self.running = running
        self.seq = 0
        self.started = f"t{self.seq}"

    def _restart(self):
        self.seq += 1
        self.started = f"t{self.seq}"
        self.running = True


class FakeDocker:
    def __init__(self, service_map: dict, containers: dict):
        self.service_map = service_map           # compose service -> [names]
        self.c = containers                      # name -> _FakeContainer

    def containers_for(self, svc):
        return sorted(self.service_map.get(svc, []))

    def container_ips(self, name):
        return ["10.0.0.5"]

    def exec_tcp_check(self, name, port):
        return self.exec_check_result if hasattr(self, "exec_check_result") \
            else False

    def inspect(self, name):
        fc = self.c[name]
        return ContainerState(name, fc.running, fc.health, fc.restart_count,
                              fc.policy, fc.created, fc.started)

    def kill(self, name):
        fc = self.c[name]
        if fc.policy in ("unless-stopped", "always", "on-failure"):
            fc.restart_count += 1
            fc._restart()                        # simulate auto-restart
        else:
            fc.running = False

    def start(self, name):
        self.c[name]._restart()


_SPEC = {
    "services": ["blog-service"],
    "infrastructure": ["kong", "blogging-db"],
    "containers": {"kong": "api-gateway"},       # name mismatch mapping
}


def _fake():
    containers = {
        "blog-service-1": _FakeContainer("blog-service-1"),
        "blog-service-2": _FakeContainer("blog-service-2"),
        "blog-service-3": _FakeContainer("blog-service-3"),
        "api-gateway-1": _FakeContainer("api-gateway-1"),
        "blogging-db-1": _FakeContainer("blogging-db-1",
                                        policy="unless-stopped",
                                        health="healthy"),
    }
    service_map = {
        "blog-service": ["blog-service-1", "blog-service-2", "blog-service-3"],
        "api-gateway": ["api-gateway-1"],
        "blogging-db": ["blogging-db-1"],
    }
    return FakeDocker(service_map, containers)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def test_component_to_service_identity_and_override():
    m = component_to_service(_SPEC)
    assert m["blog-service"] == "blog-service"     # identity
    assert m["kong"] == "api-gateway"              # override
    assert m["blogging-db"] == "blogging-db"


def test_resolve_containers():
    got = resolve_containers(_SPEC, _fake())
    assert got["blog-service"] == ["blog-service-1", "blog-service-2",
                                   "blog-service-3"]
    assert got["kong"] == ["api-gateway-1"]        # resolved via mapping


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def test_plan_flags_outage_vs_redundant():
    plans = {p.component: p for p in plan(_SPEC, _fake())}
    assert plans["blog-service"].running == 3
    assert plans["blog-service"].outage_on_kill is False   # redundant
    assert plans["kong"].running == 1
    assert plans["kong"].outage_on_kill is True            # single instance
    assert plans["blogging-db"].has_healthcheck is True
    assert plans["blogging-db"].restart_policy == "unless-stopped"


# ---------------------------------------------------------------------------
# MTTR measurement
# ---------------------------------------------------------------------------

def test_measure_mttr_manual_service():
    docker = _fake()
    res = measure_mttr(docker, "blog-service",
                       ["blog-service-1", "blog-service-2", "blog-service-3"],
                       repetitions=3)
    assert len(res.samples) == 3
    assert res.mean_seconds is not None and res.mean_seconds >= 0
    assert res.repair_rate_per_hour and res.repair_rate_per_hour > 0
    assert res.auto_restarts is False
    assert res.observed_failure_rate is None       # no restarts on a "no" policy


def test_measure_mttr_auto_restart_infra_counts_failures():
    docker = _fake()
    res = measure_mttr(docker, "blogging-db", ["blogging-db-1"], repetitions=2)
    assert len(res.samples) == 2
    assert res.auto_restarts is True
    assert res.has_healthcheck is True
    # auto-restart bumps RestartCount each kill; observed rate is then > 0
    assert res.restart_count >= 0


def test_measure_mttr_no_containers():
    res = measure_mttr(_fake(), "ghost", [], repetitions=2)
    assert res.samples == []
    assert any("no containers" in n for n in res.notes)


def test_readiness_port_parsing():
    from analyzer.chaos.injector import readiness_port
    spec = {"readiness": {"blog-service": 8000, "zookeeper": "2181"}}
    assert readiness_port(spec, "blog-service") == 8000
    assert readiness_port(spec, "zookeeper") == 2181      # string coerced
    assert readiness_port(spec, "unlisted") is None
    assert readiness_port({}, "x") is None


def test_measure_mttr_uses_port_readiness():
    # A service with no healthcheck but a readiness port waits on the TCP probe.
    import analyzer.chaos.injector as inj
    original = inj._port_open
    inj._port_open = lambda ip, port, timeout=1.5: True
    try:
        res = measure_mttr(_fake(), "blog-service", ["blog-service-1"],
                           repetitions=1, port=8000)
        assert len(res.samples) == 1
        assert res.repair_rate_per_hour and res.repair_rate_per_hour > 0
    finally:
        inj._port_open = original


def test_port_readiness_falls_back_to_exec_probe():
    import analyzer.chaos.injector as inj
    original = inj._port_open
    inj._port_open = lambda ip, port, timeout=1.5: False   # host cannot reach
    try:
        fake = _fake()
        fake.exec_check_result = True                      # app IS serving
        res = measure_mttr(fake, "blog-service", ["blog-service-1"],
                           repetitions=1, port=8000)
        assert len(res.samples) == 1
        assert res.repair_rate_per_hour and res.repair_rate_per_hour > 0
    finally:
        inj._port_open = original


# ---------------------------------------------------------------------------
# Observed failure rate
# ---------------------------------------------------------------------------

def test_observed_failure_rate_none_without_restarts():
    st = ContainerState("x", True, None, 0, "no",
                        "2026-07-10T00:00:00Z", "2026-07-10T00:00:00Z")
    assert observed_failure_rate(st) is None


def test_observed_failure_rate_from_restart_history():
    now = datetime(2026, 7, 10, 10, 0, 0, tzinfo=timezone.utc)
    created = (now - timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    st = ContainerState("x", True, None, 5, "unless-stopped", created, created)
    rate = observed_failure_rate(st, now=now)
    assert _close(rate, 0.5)                        # 5 restarts / 10 h


# ---------------------------------------------------------------------------
# TOML merge: update reliability, preserve Phase-1 performance
# ---------------------------------------------------------------------------

def test_render_updates_repair_preserves_performance():
    existing = {
        "services": {
            "blog-service": {"replicas": 3, "failure_rate": 0.03,
                             "repair_rate": 12.0, "arrival_rate": 1.42,
                             "service_rate": 21.57},
        },
        "infrastructure": {
            "kong": {"failure_rate": 0.02, "repair_rate": 12.0},
            "blogging-db": {"failure_rate": 0.005, "repair_rate": 6.0},
        },
    }
    docker = _fake()
    results = {
        "blog-service": measure_mttr(
            docker, "blog-service",
            ["blog-service-1", "blog-service-2", "blog-service-3"], 1),
        "blogging-db": measure_mttr(
            docker, "blogging-db", ["blogging-db-1"], 1),
    }
    text = render_measured_toml(_SPEC, results, existing)
    parsed = tomllib.loads(text)

    blog = parsed["services"]["blog-service"]
    # performance preserved verbatim
    assert blog["replicas"] == 3
    assert _close(blog["arrival_rate"], 1.42)
    assert _close(blog["service_rate"], 21.57)
    # repair_rate replaced with a measured value (differs from the 12.0 prior)
    assert blog["repair_rate"] != 12.0
    # blog-service has no restarts -> failure_rate prior preserved
    assert _close(blog["failure_rate"], 0.03)
    # kong wasn't injected -> both reliability fields preserved
    assert _close(parsed["infrastructure"]["kong"]["repair_rate"], 12.0)


def test_render_preserves_effective_service_rate():
    # service_rate_effective is a PERFORMANCE number (the sweep's clean,
    # throughput-recovered mu). Chaos measures reliability only, so dropping it
    # here would silently demote Layer 1 back to the queue-poisoned latency mu.
    existing = {
        "services": {
            "blog-service": {"replicas": 3, "failure_rate": 0.03,
                             "repair_rate": 12.0, "arrival_rate": 1.42,
                             "service_rate": 0.046,
                             "service_rate_effective": 6.74},
        },
        "infrastructure": {},
    }
    docker = _fake()
    results = {"blog-service": measure_mttr(
        docker, "blog-service", ["blog-service-1"], 1)}
    parsed = tomllib.loads(render_measured_toml(_SPEC, results, existing))
    blog = parsed["services"]["blog-service"]
    assert _close(blog["service_rate_effective"], 6.74)   # survived chaos
    assert _close(blog["service_rate"], 0.046)            # raw one kept too


def test_render_omits_effective_rate_when_absent():
    existing = {
        "services": {
            "blog-service": {"replicas": 3, "failure_rate": 0.03,
                             "repair_rate": 12.0, "arrival_rate": 1.42,
                             "service_rate": 21.57},
        },
        "infrastructure": {},
    }
    docker = _fake()
    results = {"blog-service": measure_mttr(
        docker, "blog-service", ["blog-service-1"], 1)}
    parsed = tomllib.loads(render_measured_toml(_SPEC, results, existing))
    assert "service_rate_effective" not in parsed["services"]["blog-service"]


def test_emitted_toml_is_wellformed():
    docker = _fake()
    results = {"blog-service": measure_mttr(
        docker, "blog-service", ["blog-service-1"], 1)}
    tomllib.loads(render_measured_toml(_SPEC, results, existing=None))


def test_verify_recovered_all_healthy():
    report = verify_recovered(_fake(), _SPEC, timeout=1.0)
    assert report.all_ok
    assert set(report.recovered) == {"blog-service", "kong", "blogging-db"}
    assert report.missing == [] and report.unhealthy == []


def test_verify_recovered_restarts_a_stopped_container():
    docker = _fake()
    docker.c["api-gateway-1"].running = False        # chaos left it down
    report = verify_recovered(docker, _SPEC, timeout=1.0)
    assert "kong" in report.healed                    # it was restarted
    assert report.all_ok                              # and came back healthy
    assert docker.c["api-gateway-1"].running


def test_verify_recovered_flags_missing_container():
    docker = _fake()
    del docker.service_map["api-gateway"]             # container removed entirely
    report = verify_recovered(docker, _SPEC, timeout=1.0)
    assert "kong" in report.missing
    assert not report.all_ok                          # -> pipeline refuses to sweep


def test_verify_recovered_flags_unhealthy_container():
    docker = _fake()
    docker.c["blogging-db-1"].health = "unhealthy"    # has a healthcheck, failing
    report = verify_recovered(docker, _SPEC, timeout=0.2, heal=False)
    assert "blogging-db" in report.unhealthy
    assert not report.all_ok


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
