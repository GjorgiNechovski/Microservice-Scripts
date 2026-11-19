"""Measure Layer-1 inputs from Prometheus and emit ``*.measured.toml``.

The collector is deliberately generic: it never hard-codes a single metric
name. It asks Prometheus which metrics exist and resolves the ones it needs
from small candidate lists / fuzzy matches. Two metric families are understood:

  * **Kong gateway metrics** (per-upstream request counters + latency
    histograms), as in the original FINKI-blogger case study, and
  * **OTel HTTP-server metrics** self-reported by each instrumented service
    (``http.server.request.duration``), typically pushed over OTLP to an OTel
    collector whose Prometheus exporter is scraped. This is the path used for
    the isotope-games (.NET / YARP) system, which has no Kong.

What it needs, per service:

  * **lambda** (arrival rate)  from a request counter
      ``sum by (<service label>)(rate(<requests_total>[window]))``
  * **mu** (service rate/replica) from a service-time histogram: mean service
      time S -> ``mu = 1 / S`` (one replica's rate)
  * **c** (replicas) from cAdvisor, counting distinct containers per
      docker-compose service label
  * **rho** = lambda / (c * mu), derived, with per-replica CPU as a cross-check

The service-time source is Kong's *upstream* latency (time the backend itself
took, excluding gateway overhead) or, equivalently, the service's own
self-reported HTTP server duration. Either way it approximates mean service
time S -- under load S includes some in-replica queueing -- and is documented
as a modelling assumption in the thesis. Measure under light/moderate load for
the cleanest estimate.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone

from analyzer.collect.promql import Prometheus, PrometheusError, Sample
from analyzer.spec import compose_projects, container_target, container_targets

# Request counter -- first that exists wins. Two families are understood:
#   * Kong gateway counters (names vary across Kong versions), and
#   * OTel HTTP-server duration histograms (any OTel-instrumented service;
#     the ``_count`` series of the histogram IS a request counter). The OTel
#     collector's Prometheus exporter emits the new semconv name in seconds;
#     the older semconv used ``http.server.duration`` in milliseconds.
_REQUEST_TOTAL_CANDIDATES = (
    "kong_http_requests_total",
    "kong_http_status",
    "http_server_request_duration_seconds_count",
    "http_server_duration_milliseconds_count",
)

# Which label carries the app-service name on traffic metrics. Kong labels
# series with ``service``; OTel resources surface as ``service_name`` (when the
# exporter converts resource attributes to labels) or ``job`` / ``exported_job``
# (Prometheus scrape semantics). Resolved at runtime by overlap with the spec.
_SERVICE_LABEL_CANDIDATES = ("service", "service_name", "exported_job", "job")

# cAdvisor tags every container with its docker-compose service name here.
_COMPOSE_LABEL = "container_label_com_docker_compose_service"
_CADVISOR_MARKERS = ("container_last_seen", "container_cpu_usage_seconds_total")

# Failure-history metrics (cAdvisor): a restart changes the start time; an OOM
# kill is an unambiguous crash. Both are generic to any container.
_START_TIME_METRIC = "container_start_time_seconds"
_PROJECT_LABEL = "container_label_com_docker_compose_project"
# cAdvisor stamps container_last_seen with the time it last saw the container,
# and keeps exporting a removed one for a while (its samples carry their own
# timestamps, so Prometheus never marks them stale). A replica counts only if
# it was seen this recently; 60 s spans several 15 s scrapes.
_SEEN_WITHIN_S = 60
_OOM_METRIC = "container_oom_events_total"

# Fallbacks used only when a value could not be measured, so the emitted file
# stays runnable. Kept in sync with analyzer.spec's placeholder defaults.
_FALLBACK_REPLICAS = 1
_FALLBACK_ARRIVAL = 10.0
_FALLBACK_SERVICE = 50.0
_FALLBACK_FAILURE = 0.02
_FALLBACK_REPAIR = 12.0


# ----------------------------------------------------------------------------
# Metric resolution
# ----------------------------------------------------------------------------

@dataclass
class _LatencyMetric:
    """How to build a mean-upstream-latency query for the resolved metric."""

    sum_metric: str
    count_metric: str
    to_seconds: float          # native unit -> seconds (ms => 1e-3)
    type_selector: str = ""    # e.g. 'type="upstream"' for older Kong

    def mean_query(self, window: str, label: str = "service") -> str:
        sel = "{" + self.type_selector + "}" if self.type_selector else ""
        return (f"sum by ({label})(rate({self.sum_metric}{sel}[{window}])) "
                f"/ sum by ({label})(rate({self.count_metric}{sel}[{window}]))")


def _first_present(candidates, names) -> str | None:
    for candidate in candidates:
        if candidate in names:
            return candidate
    return None


def _unit_to_seconds(metric_name: str) -> float:
    n = metric_name.lower()
    if "_ms" in n or "millisecond" in n:
        return 1e-3
    if "_us" in n or "microsecond" in n:
        return 1e-6
    return 1.0  # assume already seconds


def _resolve_latency(names) -> _LatencyMetric | None:
    """Find a service-time histogram, tolerating naming differences."""
    # Preferred: a dedicated *upstream* latency histogram (Kong 3.x: _ms).
    sums = sorted(n for n in names
                  if "upstream" in n and "latency" in n and n.endswith("_sum"))
    counts = sorted(n for n in names
                    if "upstream" in n and "latency" in n and n.endswith("_count"))
    if sums and counts:
        return _LatencyMetric(sums[0], counts[0], _unit_to_seconds(sums[0]))

    # OTel semantic conventions: each service self-reports its HTTP server
    # duration (http.server.request.duration, or the older http.server.duration).
    # Self-reported duration is the time inside the service, so it plays the
    # same role Kong's *upstream* latency did: mean duration ~= service time S.
    osums = sorted(n for n in names
                   if "http_server" in n and "duration" in n and n.endswith("_sum"))
    ocounts = sorted(n for n in names
                     if "http_server" in n and "duration" in n and n.endswith("_count"))
    if osums and ocounts:
        return _LatencyMetric(osums[0], ocounts[0], _unit_to_seconds(osums[0]))

    # Fallback: a generic latency histogram carrying a type="upstream" label.
    gsum = sorted(n for n in names if "latency" in n and n.endswith("_sum"))
    gcount = sorted(n for n in names if "latency" in n and n.endswith("_count"))
    if gsum and gcount:
        return _LatencyMetric(gsum[0], gcount[0], _unit_to_seconds(gsum[0]),
                              type_selector='type="upstream"')
    return None


def _resolve_service_label(prom: Prometheus, spec: dict) -> str:
    """Pick the label that carries app-service names on traffic metrics.

    Tries each candidate and keeps the first whose values overlap the spec's
    service names -- so a Kong system resolves ``service`` and an
    OTel-instrumented system resolves ``service_name`` / ``job`` without any
    configuration. Defaults to ``service`` when nothing has traffic yet.
    """
    wanted = set(spec["services"])
    for label in _SERVICE_LABEL_CANDIDATES:
        try:
            values = set(prom.label_values(label))
        except PrometheusError:
            continue
        if values & wanted:
            return label
    return "service"


def _by_label(samples: list[Sample], label: str) -> dict:
    out = {}
    for s in samples:
        key = s.labels.get(label)
        if key:
            out[key] = s.value
    return out


def _window_hours(window: str) -> float:
    """Parse a Prometheus range like '7d' / '24h' / '30m' into hours."""
    units = {"s": 1 / 3600, "m": 1 / 60, "h": 1.0, "d": 24.0, "w": 168.0}
    try:
        return float(window[:-1]) * units[window[-1]]
    except (ValueError, KeyError, IndexError):
        return 0.0


def _container_selector(spec: dict) -> str:
    """PromQL label matchers for this system's containers in cAdvisor series.

    Every compose container on the host carries the service label; the spec's
    ``compose_project`` narrows that to this system, so another project's
    ``redis`` is not counted as one of its replicas. Several projects (an app
    split over repositories) match as one regex; compose's own naming rule,
    which the spec enforces, leaves nothing in a name to escape.
    """
    sel = f'{_COMPOSE_LABEL}!=""'
    projects = compose_projects(spec)
    if len(projects) == 1:
        sel += f',{_PROJECT_LABEL}="{projects[0]}"'
    elif projects:
        sel += f',{_PROJECT_LABEL}=~"{"|".join(projects)}"'
    return sel


def _split_by_project(spec: dict) -> bool:
    """Whether cAdvisor series must be told apart by project as well as by
    service: the app runs as several projects, or a component is pinned to
    one. Otherwise the queries stay exactly as they were."""
    return len(compose_projects(spec)) > 1 or any(
        project for _, project in container_targets(spec).values())


def _by_target(samples: list[Sample], split: bool) -> dict:
    """{(compose service, project or None): value}."""
    out: dict = {}
    for s in samples:
        svc = s.labels.get(_COMPOSE_LABEL)
        if svc:
            key = (svc, s.labels.get(_PROJECT_LABEL) if split else None)
            out[key] = out.get(key, 0.0) + s.value
    return out


def _for_target(values: dict, target: tuple):
    """``(value, projects it came from)`` for one component: its pinned
    project's value, else the sum over every project with that service."""
    svc, pin = target
    hits = {p: v for (s, p), v in values.items()
            if s == svc and (pin is None or p == pin)}
    if not hits:
        return None, set()
    return sum(hits.values()), {p for p in hits if p}


def _component_compose_map(spec: dict) -> dict:
    """component -> compose service, honoring the optional [containers] table."""
    return {comp: svc for comp, (svc, _) in container_targets(spec).items()}


def measure_failure_rates(prom: Prometheus, spec: dict,
                          window: str = "7d") -> dict:
    """Observed failure_rate (per hour) per component from restart/OOM history.

    Generic and empirical: counts container restarts (changes in
    ``container_start_time_seconds``) plus OOM kills over ``window``, per
    compose service, divided by the window length. Returns ``None`` for a
    component with no history in the window -- it never invents a value.

    Caveat: deliberate chaos (Phase 2) also restarts containers, so measure this
    over a window of NORMAL operation, not one polluted by failure injection.
    """
    hours = _window_hours(window)
    names = set(prom.metric_names())
    targets = container_targets(spec)
    result = {comp: None for comp in targets}
    if hours <= 0 or _START_TIME_METRIC not in names:
        return result

    split = _split_by_project(spec)
    by = f"{_COMPOSE_LABEL}, {_PROJECT_LABEL}" if split else _COMPOSE_LABEL
    restarts = _by_target(prom.instant(
        f"sum by ({by})(changes({_START_TIME_METRIC}"
        f'{{{_container_selector(spec)}}}[{window}]))'), split)
    ooms = {}
    if _OOM_METRIC in names:
        ooms = _by_target(prom.instant(
            f"sum by ({by})(increase({_OOM_METRIC}"
            f'{{{_container_selector(spec)}}}[{window}]))'), split)

    for comp, target in targets.items():
        restarted, _ = _for_target(restarts, target)
        oomed, _ = _for_target(ooms, target)
        if restarted is None and oomed is None:
            continue  # no series at all -> leave as None (unmeasured)
        result[comp] = ((restarted or 0.0) + (oomed or 0.0)) / hours
    return result


# ----------------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------------

@dataclass
class Discovery:
    metric_count: int
    request_metric: str | None
    latency: _LatencyMetric | None
    has_cadvisor: bool
    service_label: str
    traffic_services: list
    compose_services: list
    notes: list


def discover(prom: Prometheus, spec: dict) -> Discovery:
    """Report what the collector can and cannot measure on this Prometheus."""
    names = set(prom.metric_names())
    request_metric = _first_present(_REQUEST_TOTAL_CANDIDATES, names)
    latency = _resolve_latency(names)
    has_cadvisor = any(m in names for m in _CADVISOR_MARKERS)
    service_label = _resolve_service_label(prom, spec)

    try:
        traffic_services = [v for v in prom.label_values(service_label) if v]
    except PrometheusError:
        traffic_services = []
    try:
        compose_services = [v for v in prom.label_values(_COMPOSE_LABEL) if v]
    except PrometheusError:
        compose_services = []

    spec_services = set(spec["services"])
    notes: list[str] = []
    if request_metric is None:
        notes.append("No request-count metric found -> lambda cannot be "
                     "measured (is a gateway metrics plugin or per-service "
                     "OTel instrumentation being scraped?).")
    if latency is None:
        notes.append("No service-time histogram found -> mu (service rate) "
                     "cannot be measured.")
    if not has_cadvisor:
        notes.append("No cAdvisor metrics found -> replica counts cannot be "
                     "measured.")
    if traffic_services:
        missing = sorted(spec_services - set(traffic_services))
        if missing:
            notes.append(f"Spec services with no traffic metrics yet (or a name "
                         f"mismatch on label '{service_label}'): {missing}. "
                         f"Drive traffic to them first.")
    if compose_services:
        missing = sorted(spec_services - set(compose_services))
        if missing:
            notes.append(f"Spec services not seen by cAdvisor: {missing}.")

    return Discovery(len(names), request_metric, latency, has_cadvisor,
                     service_label, sorted(traffic_services),
                     sorted(compose_services), notes)


# ----------------------------------------------------------------------------
# Measurement
# ----------------------------------------------------------------------------

@dataclass
class Measurement:
    name: str
    replicas: int | None = None
    arrival_rate: float | None = None     # lambda, req/s
    service_rate: float | None = None     # mu per replica, req/s
    utilization: float | None = None      # rho = lambda / (c * mu)
    cpu_cores_total: float | None = None  # summed over replicas (cross-check)
    notes: list = field(default_factory=list)

    @property
    def per_replica_cpu(self) -> float | None:
        if self.cpu_cores_total is None or not self.replicas:
            return None
        return self.cpu_cores_total / self.replicas

    @property
    def fully_measured(self) -> bool:
        return None not in (self.replicas, self.arrival_rate, self.service_rate)


def _instant(prom: Prometheus, query: str, at=None) -> list[Sample]:
    """Instant query now, or at ``at`` (only passed when set, so any client
    with a one-argument ``instant`` keeps working)."""
    return prom.instant(query) if at is None else prom.instant(query, at=at)


def measure(prom: Prometheus, spec: dict, window: str = "5m",
            at=None) -> list[Measurement]:
    """Measure c, lambda, mu, rho for every service in the spec.

    ``at`` re-measures a finished run: the window then ends at that moment
    (RFC 3339 or unix time) instead of now.
    """
    names = set(prom.metric_names())
    request_metric = _first_present(_REQUEST_TOTAL_CANDIDATES, names)
    latency = _resolve_latency(names)
    has_cadvisor = any(m in names for m in _CADVISOR_MARKERS)
    label = _resolve_service_label(prom, spec)

    lam: dict = {}
    if request_metric:
        lam = _by_label(
            _instant(prom, f"sum by ({label})(rate({request_metric}[{window}]))",
                     at),
            label)

    mu: dict = {}
    if latency:
        raw_mean = _by_label(_instant(prom, latency.mean_query(window, label), at),
                             label)
        for svc, raw in raw_mean.items():
            seconds = raw * latency.to_seconds
            if seconds > 0:
                mu[svc] = 1.0 / seconds

    replicas: dict = {}
    cpu: dict = {}
    if has_cadvisor:
        sel = _container_selector(spec)
        split = _split_by_project(spec)
        by = f"{_COMPOSE_LABEL}, {_PROJECT_LABEL}" if split else _COMPOSE_LABEL
        rq = (f"count by ({by})(group by ({by}, name)"
              f"(container_last_seen{{{sel}}} > time() - {_SEEN_WITHIN_S}))")
        replicas = _by_target(_instant(prom, rq, at), split)
        cq = (f"sum by ({by})(rate("
              f"container_cpu_usage_seconds_total{{{sel}}}[{window}]))")
        cpu = _by_target(_instant(prom, cq, at), split)

    results: list[Measurement] = []
    for name in spec["services"]:
        target = container_target(spec, name)
        m = Measurement(name)
        count, projects = _for_target(replicas, target)
        m.replicas = int(round(count)) if count is not None else None
        m.arrival_rate = lam.get(name)
        m.service_rate = mu.get(name)
        m.cpu_cores_total = _for_target(cpu, target)[0]

        if m.replicas is None:
            m.notes.append("replicas not measured (no cAdvisor series)")
        elif len(projects) > 1:
            m.notes.append(
                f"replicas counted across {len(projects)} compose projects "
                f"({', '.join(sorted(projects))}) that all have a "
                f"'{target[0]}': pin it to one in [containers]")
        # A rate needs at least two scrapes of a counter, so a service whose
        # metrics appeared seconds ago reads as unmeasured even though
        # --discover already lists it. Say so: otherwise the two outputs
        # contradict each other with no hint of why.
        if m.arrival_rate is None:
            m.notes.append(
                f"lambda not measured (no request rate in the last {window}; "
                "if --discover lists this service with traffic, its counters "
                "are too new -- drive traffic for a minute and retry, "
                "otherwise its telemetry name does not match the spec)")
        if m.service_rate is None:
            m.notes.append(f"mu not measured (no service-time samples in the "
                           f"last {window})")

        if m.arrival_rate is not None and m.service_rate and m.replicas:
            m.utilization = m.arrival_rate / (m.replicas * m.service_rate)
            if m.utilization >= 1.0:
                m.notes.append(f"OVERLOADED: rho={m.utilization:.2f} >= 1 "
                               "(queue is unstable at this load)")
        results.append(m)
    return results


# ----------------------------------------------------------------------------
# Emitting the measured file
# ----------------------------------------------------------------------------

def _fmt(value: float) -> str:
    """Compact but faithful float formatting for TOML."""
    rounded = round(value, 4)
    if rounded == int(rounded):
        return f"{int(rounded)}.0"
    return repr(rounded)


def _repair_placeholder(prev: dict) -> bool:
    """True when the repair rate carried forward is a placeholder, not measured."""
    return "repair_rate" not in prev or prev.get("repair_rate_measured") is False


def render_measured_toml(spec: dict, measurements: list[Measurement],
                         existing: dict | None = None,
                         effective: dict | None = None,
                         inherit_effective: bool = True,
                         lower_bound=()) -> str:
    """Render a ``*.measured.toml`` string.

    Phase 1 owns ``replicas`` / ``arrival_rate`` / ``service_rate``. It
    PRESERVES ``failure_rate`` / ``repair_rate`` (Phase 2's job) and the whole
    ``[infrastructure]`` section from ``existing`` when available, so running
    collection never clobbers reliability numbers.

    ``effective`` (optional, ``{service: mu_effective}``) is written as
    ``service_rate_effective`` -- the clean throughput-recovered per-replica
    rate the load sweep derives once it can see a service saturate. Layer 1
    (performance) prefers it over the latency-based ``service_rate``; the latter
    is kept for reference (it exposes the μ-poisoning under load).

    ``inherit_effective`` controls the fallback when ``effective`` carries no
    value for a service: by default the one in ``existing`` is carried forward,
    which is what a normal write wants. Pass ``False`` to emit ONLY freshly
    derived values. The sweep's preliminary pass needs that: it must decide
    whether a service saturated *in this run*, and inheriting a previous run's
    effective rate would let a stale mu drive that decision -- masking a real
    regression, or inventing a saturation that did not happen.

    ``lower_bound`` names services whose effective rate is only a floor (the
    sweep never saw their throughput level off); they get
    ``service_rate_lower_bound = true``, carried forward with an inherited rate.
    """
    existing = existing or {}
    effective = effective or {}
    ex_services = existing.get("services", {})
    ex_infra = existing.get("infrastructure", {})
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# " + "=" * 75,
        "# AUTO-GENERATED MEASUREMENTS  --  DO NOT EDIT BY HAND.",
        "#",
        "# Performance numbers (replicas, arrival_rate, service_rate) were MEASURED",
        f"# from the running system via Prometheus by Phase 1 (collection) at {stamp}.",
        "# failure_rate / repair_rate are preserved from Phase 2 (failure injection)",
        "# or placeholders until it runs (marked repair_rate_measured = false).",
        "# Infrastructure rates are preserved as-is.",
        "#",
        "#   failure_rate / repair_rate : per hour   (reliability, Layer 2)",
        "#   arrival_rate / service_rate: per second (performance, Layer 1)",
        "#   service_rate               : 1 / measured mean latency; a fallback",
        "#                                is marked service_rate_measured = false",
        "#   service_rate_effective     : per second, clean throughput-recovered",
        "#                                per-replica rate (Layer 1 prefers it)",
        "#   service_rate_lower_bound   : true = no plateau seen, so the effective",
        "#                                rate is only a floor (limit not reached)",
        "#   replicas                   : counted from cAdvisor",
        "# " + "=" * 75,
        "",
    ]

    for m in measurements:
        prev = ex_services.get(m.name, {})
        replicas = m.replicas if m.replicas is not None else \
            prev.get("replicas", _FALLBACK_REPLICAS)
        arrival = m.arrival_rate if m.arrival_rate is not None else \
            prev.get("arrival_rate", _FALLBACK_ARRIVAL)
        service = m.service_rate if m.service_rate is not None else \
            prev.get("service_rate", _FALLBACK_SERVICE)
        failure = prev.get("failure_rate", _FALLBACK_FAILURE)
        repair = prev.get("repair_rate", _FALLBACK_REPAIR)

        lines.append(f"[services.{m.name}]")
        lines.append(f"replicas = {int(replicas)}")
        lines.append(f"failure_rate = {_fmt(float(failure))}")
        lines.append(f"repair_rate = {_fmt(float(repair))}")
        if _repair_placeholder(prev):
            lines.append("repair_rate_measured = false")
        lines.append(f"arrival_rate = {_fmt(float(arrival))}")
        lines.append(f"service_rate = {_fmt(float(service))}")
        if m.service_rate is None:      # a fallback: never read it as latency
            lines.append("service_rate_measured = false")
        eff = effective.get(m.name)
        floor = m.name in lower_bound
        if eff is None and inherit_effective:
            eff = prev.get("service_rate_effective")
            floor = bool(prev.get("service_rate_lower_bound"))
        if eff is not None:
            lines.append(f"service_rate_effective = {_fmt(float(eff))}")
            if floor:
                lines.append("service_rate_lower_bound = true")
        if not m.fully_measured:
            lines.append("# NOTE: one or more values above are fallbacks, not "
                         "measured. See collector output.")
        lines.append("")

    for name in spec.get("infrastructure", []):
        prev = ex_infra.get(name, {})
        failure = prev.get("failure_rate", _FALLBACK_FAILURE)
        repair = prev.get("repair_rate", _FALLBACK_REPAIR)
        lines.append(f"[infrastructure.{name}]")
        lines.append(f"failure_rate = {_fmt(float(failure))}")
        lines.append(f"repair_rate = {_fmt(float(repair))}")
        if _repair_placeholder(prev):
            lines.append("repair_rate_measured = false")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_measured(path: str, spec: dict,
                   measurements: list[Measurement]) -> str:
    """Write the measured file, merging with any existing one. Returns text."""
    existing: dict = {}
    if os.path.exists(path):
        with open(path, "rb") as fh:
            existing = tomllib.load(fh)
    text = render_measured_toml(spec, measurements, existing)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return text
