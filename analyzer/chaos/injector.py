"""Failure injection: measure repair_rate (and observe failure_rate).

For each component the harness kills a container and times how long it takes to
come back:

  * **MTTR** (mean time to repair) = kill -> recovered-and-ready, averaged over
    repetitions. ``repair_rate = 1 / MTTR`` (per hour).
  * Recovery is either automatic (containers with a restart policy) or performed
    by the harness (``docker start`` for those without one).
  * "Ready" = container running again AND, if it has a health check, healthy;
    otherwise running is the proxy (noted as a lower bound for app warm-up).

**failure_rate** is not something a short lab run can induce -- natural MTTF
comes from long observation. We derive an *observed* failure rate from Docker's
``RestartCount`` over the container's lifetime where any restarts have happened;
where none have, the prior (placeholder/assumption) is preserved. This is the
honest split: repair is measured, failure is observed-or-assumed.

Everything routes through the injectable :class:`~analyzer.chaos.dockercli.Docker`
so the logic is testable without Docker.
"""

from __future__ import annotations

import os
import socket
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone

from analyzer.chaos.dockercli import ContainerState, Docker, DockerError
from analyzer.collect.collector import (
    _FALLBACK_ARRIVAL,
    _FALLBACK_FAILURE,
    _FALLBACK_REPAIR,
    _FALLBACK_REPLICAS,
    _FALLBACK_SERVICE,
    _fmt,
)


# ----------------------------------------------------------------------------
# Component -> container resolution
# ----------------------------------------------------------------------------

def component_to_service(spec: dict) -> dict:
    """Map each spec component to its compose-service name.

    Identity by default; the optional ``[containers]`` table overrides names
    that differ (e.g. this system's ``kong`` -> ``api-gateway``).
    """
    overrides = spec.get("containers", {})
    components = list(spec["services"]) + list(spec.get("infrastructure", []))
    return {comp: overrides.get(comp, comp) for comp in components}


def resolve_containers(spec: dict, docker: Docker) -> dict:
    """component -> [container names] for everything in the spec."""
    return {comp: docker.containers_for(svc)
            for comp, svc in component_to_service(spec).items()}


# ----------------------------------------------------------------------------
# Planning (dry-run: no kills)
# ----------------------------------------------------------------------------

@dataclass
class ComponentPlan:
    component: str
    service: str
    containers: list
    running: int
    restart_policy: str
    has_healthcheck: bool
    single_instance: bool
    note: str = ""

    @property
    def outage_on_kill(self) -> bool:
        # killing takes the component fully down if there's no surviving replica
        return self.running <= 1


def plan(spec: dict, docker: Docker) -> list:
    """Describe what injection *would* do, without touching anything."""
    plans = []
    for comp, svc in component_to_service(spec).items():
        try:
            containers = docker.containers_for(svc)
        except DockerError as exc:
            plans.append(ComponentPlan(comp, svc, [], 0, "?", False, True,
                                       note=f"docker error: {exc}"))
            continue
        if not containers:
            plans.append(ComponentPlan(comp, svc, [], 0, "?", False, True,
                                       note="no containers found for this "
                                            "compose service"))
            continue
        running = 0
        policy = "no"
        healthcheck = False
        for name in containers:
            try:
                st = docker.inspect(name)
            except DockerError:
                continue
            running += 1 if st.running else 0
            policy = st.restart_policy
            healthcheck = st.has_healthcheck
        plans.append(ComponentPlan(
            comp, svc, containers, running, policy, healthcheck,
            single_instance=len(containers) <= 1))
    return plans


# ----------------------------------------------------------------------------
# Measurement
# ----------------------------------------------------------------------------

@dataclass
class MttrResult:
    component: str
    target: str | None = None
    samples: list = field(default_factory=list)      # seconds
    mean_seconds: float | None = None
    repair_rate_per_hour: float | None = None
    observed_failure_rate: float | None = None       # per hour, or None
    restart_count: int = 0
    auto_restarts: bool = False
    has_healthcheck: bool = False
    notes: list = field(default_factory=list)


def _wait(predicate, timeout: float, interval: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except DockerError:
            pass
        time.sleep(interval)
    return False


def readiness_port(spec: dict, component: str) -> int | None:
    """TCP port to probe for readiness (optional ``[readiness]`` table)."""
    value = spec.get("readiness", {}).get(component)
    return int(value) if value else None


def _port_open(ip: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _app_port_ready(docker: Docker, name: str, port: int) -> bool:
    """Is the app inside ``name`` accepting connections on ``port``?

    Two probes, first success wins:
      * TCP to the container IP -- works when the host routes to the docker
        network (Linux). On Docker Desktop (Windows/macOS) container IPs are
        NOT host-reachable, so this can never succeed there.
      * ``docker exec`` probe from inside the container's own network
        namespace -- OS-independent fallback. Without it, every readiness
        wait on Docker Desktop burns the full timeout and MTTR reads as a
        60s ceiling instead of a measurement.
    """
    if any(_port_open(ip, port) for ip in docker.container_ips(name)):
        return True
    return docker.exec_tcp_check(name, port)


def _ready(docker: Docker, name: str, pre_started: str,
           port: int | None) -> bool:
    """True once the recovered container is actually serving again.

    Readiness signal, best available first:
      * a Docker health check  -> wait for ``healthy``
      * a configured TCP port  -> wait for the app to accept connections
      * otherwise              -> container running (a lower bound; app warm-up
                                   is not observed)
    A changed ``StartedAt`` guards against reading the pre-kill state.
    """
    st = docker.inspect(name)
    if not st.running or st.started == pre_started:
        return False
    if st.has_healthcheck:
        return st.health == "healthy"
    if port:
        return _app_port_ready(docker, name, port)
    return True


def _pick_running(docker: Docker, containers: list) -> str | None:
    for name in containers:
        try:
            if docker.inspect(name).running:
                return name
        except DockerError:
            continue
    return containers[0] if containers else None


def _parse_docker_time(value: str):
    if not value:
        return None
    text = value.strip()
    if text.startswith("0001"):        # docker's zero-time
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # trim sub-second precision to microseconds (docker emits nanoseconds)
    if "." in text:
        head, _, tail = text.partition(".")
        frac = ""
        off = ""
        for ch in tail:
            if ch in "+-":
                off = tail[tail.index(ch):]
                break
            frac += ch
        text = f"{head}.{frac[:6]}{off}"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def observed_failure_rate(state: ContainerState, now=None) -> float | None:
    """RestartCount over the container's lifetime, per hour (None if no data)."""
    if state.restart_count <= 0:
        return None
    created = _parse_docker_time(state.created)
    if created is None:
        return None
    now = now or datetime.now(timezone.utc)
    hours = (now - created).total_seconds() / 3600.0
    if hours <= 0:
        return None
    return state.restart_count / hours


def measure_mttr(docker: Docker, component: str, containers: list,
                 repetitions: int = 3, timeout: float = 60.0,
                 port: int | None = None) -> MttrResult:
    """Kill/recover a container ``repetitions`` times; average the recovery.

    ``port`` (from the spec's ``[readiness]`` table) makes recovery mean "the
    app accepts TCP connections again" for components without a Docker health
    check -- otherwise recovery is only "container running" (a lower bound).
    """
    result = MttrResult(component)
    if not containers:
        result.notes.append("no containers found")
        return result

    target = _pick_running(docker, containers)
    result.target = target
    try:
        base = docker.inspect(target)
    except DockerError as exc:
        result.notes.append(f"inspect failed: {exc}")
        return result
    result.auto_restarts = base.auto_restarts
    result.has_healthcheck = base.has_healthcheck
    result.restart_count = base.restart_count
    result.observed_failure_rate = observed_failure_rate(base)

    for i in range(repetitions):
        try:
            pre = docker.inspect(target)
            if not pre.running:                      # ensure a clean start
                docker.start(target)
                _wait(lambda: docker.inspect(target).running, timeout)
                pre = docker.inspect(target)

            t0 = time.monotonic()
            docker.kill(target)

            # GUARANTEE recovery ourselves -- never trust the declared restart
            # policy to fire. In testing, `docker kill` left `unless-stopped`
            # containers dead (Exited 137); passively waiting for an auto-restart
            # that never comes turns a measurement into a long outage. So: once
            # the container is observed down, actively start it. If a policy DID
            # already bring it back, the start is a harmless no-op.
            went_down = _wait(lambda: not docker.inspect(target).running,
                              timeout=min(15.0, timeout))
            if went_down:
                try:
                    docker.start(target)
                except DockerError:
                    pass  # already restarting via its policy -- fine

            recovered = _wait(
                lambda: _ready(docker, target, pre.started, port), timeout)
            elapsed = time.monotonic() - t0

            if recovered:
                result.samples.append(elapsed)
            else:
                result.notes.append(f"rep {i + 1}: recovery timed out "
                                    f"(> {timeout:.0f}s)")
        except DockerError as exc:
            result.notes.append(f"rep {i + 1}: {exc}")

    if result.samples:
        result.mean_seconds = sum(result.samples) / len(result.samples)
        if result.mean_seconds <= 0.0:
            result.mean_seconds = 0.016
            result.notes.append("recovery faster than clock resolution; "
                                "MTTR clamped to 16 ms (lower bound)")
        result.repair_rate_per_hour = 3600.0 / result.mean_seconds
    return result


# ----------------------------------------------------------------------------
# Post-chaos recovery check (did the whole stack actually come back?)
# ----------------------------------------------------------------------------

@dataclass
class RecoveryReport:
    recovered: list = field(default_factory=list)   # components healthy again
    healed: list = field(default_factory=list)      # had to be restarted here
    unhealthy: list = field(default_factory=list)   # running but not ready in time
    missing: list = field(default_factory=list)     # no container at all (removed)

    @property
    def all_ok(self) -> bool:
        return not self.unhealthy and not self.missing


def _healthy_now(docker: Docker, name: str, port: int | None) -> bool:
    """Is this container up and serving right now (no pre-kill reference needed)?"""
    try:
        st = docker.inspect(name)
    except DockerError:
        return False
    if not st.running:
        return False
    if st.has_healthcheck:
        return st.health == "healthy"
    if port:
        try:
            return _app_port_ready(docker, name, port)
        except DockerError:
            return False
    return True


def verify_recovered(docker: Docker, spec: dict, timeout: float = 60.0,
                     heal: bool = True) -> RecoveryReport:
    """After chaos, confirm every component is back up and ready.

    Restarts anything left stopped (``heal``) and waits up to ``timeout`` for each
    component to become ready (health check / readiness port / running). Reports
    what could not be restored -- an unhealthy component, or one whose container
    is gone entirely -- so the caller can refuse to measure a degraded stack
    instead of silently producing meaningless numbers.
    """
    report = RecoveryReport()
    for comp, svc in component_to_service(spec).items():
        try:
            containers = docker.containers_for(svc)
        except DockerError:
            containers = []
        if not containers:
            report.missing.append(comp)          # removed -> can't docker-start it
            continue
        if heal:
            for name in containers:
                try:
                    if not docker.inspect(name).running:
                        docker.start(name)
                        if comp not in report.healed:
                            report.healed.append(comp)
                except DockerError:
                    pass
        port = readiness_port(spec, comp)
        ready = _wait(
            lambda: any(_healthy_now(docker, n, port) for n in containers), timeout)
        (report.recovered if ready else report.unhealthy).append(comp)
    return report


# ----------------------------------------------------------------------------
# Emitting the measured file (update reliability, preserve Phase-1 performance)
# ----------------------------------------------------------------------------

def _measured_repair(res: MttrResult | None, prev: dict) -> float:
    if res and res.repair_rate_per_hour:
        return res.repair_rate_per_hour
    return prev.get("repair_rate", _FALLBACK_REPAIR)


def _measured_failure(res: MttrResult | None, prev: dict) -> float:
    if res and res.observed_failure_rate:
        return res.observed_failure_rate
    return prev.get("failure_rate", _FALLBACK_FAILURE)


def render_measured_toml(spec: dict, results: dict,
                         existing: dict | None = None) -> str:
    """Render ``*.measured.toml`` updating reliability from chaos ``results``.

    Phase 2 owns ``failure_rate`` / ``repair_rate``. It PRESERVES Phase-1's
    ``replicas`` / ``arrival_rate`` / ``service_rate`` -- and the sweep's
    ``service_rate_effective`` -- from ``existing`` so a chaos run never
    clobbers the performance numbers. ``results`` maps a component name to its
    :class:`MttrResult`.

    ``service_rate_effective`` is preserved verbatim (it is a performance
    number, not a reliability one). Dropping it would silently demote Layer 1
    back to the latency-based ``service_rate``, which is queue-poisoned under
    load -- the failure mode the effective rate exists to prevent.
    """
    existing = existing or {}
    ex_services = existing.get("services", {})
    ex_infra = existing.get("infrastructure", {})
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# " + "=" * 75,
        "# AUTO-GENERATED MEASUREMENTS  --  DO NOT EDIT BY HAND.",
        "#",
        "# Reliability numbers (repair_rate) were MEASURED by failure injection",
        f"# (kill a container, time recovery) by Phase 2 at {stamp}.",
        "# failure_rate is observed from restart history where any restarts have",
        "# occurred, else preserved as a prior/assumption. Performance numbers",
        "# (replicas/arrival_rate/service_rate) are preserved from Phase 1.",
        "#",
        "#   failure_rate / repair_rate : per hour   (reliability, Layer 2)",
        "#   arrival_rate / service_rate: per second (performance, Layer 1)",
        "#   service_rate_effective     : per second, preserved from the sweep",
        "# " + "=" * 75,
        "",
    ]

    for name in spec["services"]:
        prev = ex_services.get(name, {})
        res = results.get(name)
        lines.append(f"[services.{name}]")
        lines.append(f"replicas = {int(prev.get('replicas', _FALLBACK_REPLICAS))}")
        lines.append(f"failure_rate = {_fmt(_measured_failure(res, prev))}")
        lines.append(f"repair_rate = {_fmt(_measured_repair(res, prev))}")
        lines.append(f"arrival_rate = "
                     f"{_fmt(float(prev.get('arrival_rate', _FALLBACK_ARRIVAL)))}")
        lines.append(f"service_rate = "
                     f"{_fmt(float(prev.get('service_rate', _FALLBACK_SERVICE)))}")
        eff = prev.get("service_rate_effective")
        if eff is not None:
            lines.append(f"service_rate_effective = {_fmt(float(eff))}")
        lines.append("")

    for name in spec.get("infrastructure", []):
        prev = ex_infra.get(name, {})
        res = results.get(name)
        lines.append(f"[infrastructure.{name}]")
        lines.append(f"failure_rate = {_fmt(_measured_failure(res, prev))}")
        lines.append(f"repair_rate = {_fmt(_measured_repair(res, prev))}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_measured(path: str, spec: dict, results: dict) -> str:
    """Write the measured file, merging chaos results with the existing one."""
    existing: dict = {}
    if os.path.exists(path):
        with open(path, "rb") as fh:
            existing = tomllib.load(fh)
    text = render_measured_toml(spec, results, existing)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return text
