"""Generic system spec + measurements: describe a system once, analyze it.

Two files, cleanly separated:

  * The **system spec** (``systems/<name>.toml``) is the ONLY thing a user edits.
    It contains just names and topology: the services, the shared infrastructure,
    and the operations they care about. No numbers.

  * The **measurements** (``systems/<name>.measured.toml``) hold every numeric
    parameter (failure/repair rates, arrival/service rates, replica counts).
    This file is produced automatically by the measurement phases from the
    running system -- the user never edits it. Until those phases exist,
    placeholder values stand in.

``analyze(spec, measurements)`` merges the two and runs all four analysis layers.
The engine knows nothing about any particular application; the blogging platform
is just ``systems/finki-blogger.toml``.

TOML is read with the standard-library ``tomllib`` (no third-party deps).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass

from analyzer.models.ctmc import component_reliability, replicated_reliability
from analyzer.models.queueing import mmc_metrics
from analyzer.models.rbd import Component, System, parallel, series

_MINUTES_PER_YEAR = 365 * 24 * 60

# Placeholder measurements used when no measured file exists yet (i.e. before the
# measurement phases have run). Deliberately uniform so it is obvious they are
# not real data.
_DEFAULT_SERVICE = dict(replicas=1, failure_rate=0.02, repair_rate=12.0,
                        arrival_rate=10.0, service_rate=50.0)
_DEFAULT_INFRA = dict(failure_rate=0.02, repair_rate=12.0)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def load_system(path: str) -> dict:
    """Load and validate a user's structural system spec (names + topology)."""
    with open(path, "rb") as fh:
        spec = tomllib.load(fh)
    _validate_system(spec)
    return spec


def _validate_system(spec: dict) -> None:
    if not isinstance(spec.get("services"), list) or not spec["services"]:
        raise ValueError("spec must contain a non-empty 'services' list")
    known = set(spec["services"]) | set(spec.get("infrastructure", []))
    for op, requires in spec.get("operations", {}).items():
        if not isinstance(requires, list):
            raise ValueError(f"operation '{op}' must be a list of components")
        for comp in requires:
            if comp not in known:
                raise ValueError(
                    f"operation '{op}' needs unknown component '{comp}' "
                    f"(add it to 'services' or 'infrastructure')")
    infra = set(spec.get("infrastructure", []))
    for comp, n in spec.get("instances", {}).items():
        if comp not in infra:
            raise ValueError(
                f"[instances] names '{comp}', which is not infrastructure "
                f"(service replica counts are measured, not declared)")
        if not isinstance(n, int) or n < 1:
            raise ValueError(
                f"[instances].{comp} must be an integer >= 1, got {n!r}")


def infra_instances(spec: dict) -> dict:
    """How many instances each infrastructure component runs.

    Services get their replica count from measurement (cAdvisor counts the
    containers). Infrastructure cannot be counted the same way -- a component
    like a gateway may be deployed behind a load balancer -- so it is declared
    in the optional ``[instances]`` table. Anything not listed runs one.
    """
    declared = spec.get("instances", {})
    return {name: int(declared.get(name, 1))
            for name in spec.get("infrastructure", [])}


def measurements_path(system_path: str) -> str:
    """The measured-data path that pairs with a system spec path."""
    base = system_path[:-5] if system_path.endswith(".toml") else system_path
    return base + ".measured.toml"


def load_measurements(path: str) -> dict:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def default_measurements(spec: dict) -> dict:
    """Uniform placeholder measurements for a spec (used when none exist yet)."""
    m = {"services": {}, "infrastructure": {}}
    for name in spec["services"]:
        m["services"][name] = dict(_DEFAULT_SERVICE)
    for name in spec.get("infrastructure", []):
        m["infrastructure"][name] = dict(_DEFAULT_INFRA)
    return m


def load_or_default_measurements(system_path: str, spec: dict):
    """Return ``(measurements, source_label)`` for a system spec.

    Uses the sibling ``.measured.toml`` if present, otherwise placeholder
    defaults (so the analysis is still runnable before measurement exists).
    """
    mpath = measurements_path(system_path)
    if os.path.exists(mpath):
        return load_measurements(mpath), mpath
    return default_measurements(spec), "built-in placeholder defaults"


# --------------------------------------------------------------------------
# Result containers
# --------------------------------------------------------------------------

@dataclass
class ServiceResult:
    name: str
    replicas: int
    queue: object            # QueueMetrics (Layer 1)
    reliability: object      # ReliabilityResult for the tier (Layer 2)
    per_replica_availability: float


@dataclass
class TopEventResult:
    name: str
    requires: list
    system: System
    availability: float
    cut_sets: list
    spofs: list
    importance: list
    interventions: list      # (label, new_availability, downtime_saved_min_yr)


# --------------------------------------------------------------------------
# Structure building
# --------------------------------------------------------------------------

def _build_system(requires, services, infra, service_results,
                  infra_avail, infra_redundancy=None, extra_replicas=None):
    """Build the RBD System for one operation.

    A required *service* expands to a parallel block over its replicas; a
    required *infrastructure* component is a single block unless
    ``infra_redundancy`` bumps it (used by where-to-invest scenarios).
    ``extra_replicas`` maps a service to how many extra replicas to add.
    """
    infra_redundancy = infra_redundancy or {}
    extra_replicas = extra_replicas or {}
    availability = {}
    parts = []
    for name in requires:
        if name in services:
            c = service_results[name].replicas + extra_replicas.get(name, 0)
            reps = [f"{name}[{i}]" for i in range(1, c + 1)]
            for r in reps:
                availability[r] = service_results[name].per_replica_availability
            parts.append(parallel(*reps) if c > 1 else Component(reps[0]))
        else:  # infrastructure
            n = infra_redundancy.get(name, 1)
            insts = [f"{name}#{i}" for i in range(1, n + 1)]
            for inst in insts:
                availability[inst] = infra_avail[name]
            parts.append(parallel(*insts) if n > 1 else Component(insts[0]))
    return System(series(*parts), availability)


def _ordinal(n: int) -> str:
    """2 -> '2nd', 3 -> '3rd' ... used for the 'add Nth <component>' labels."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _where_to_invest(requires, services, infra, service_results, infra_avail,
                     instances=None):
    """Rank interventions for one operation by downtime saved per year.

    Each intervention adds ONE instance to the component's *current* count, so
    the ranking stays meaningful after a component has already been made
    redundant (``[instances]`` in the spec).
    """
    instances = instances or {}
    base = _build_system(requires, services, infra, service_results,
                         infra_avail, infra_redundancy=instances)
    base_dt = base.system_unavailability() * _MINUTES_PER_YEAR

    interventions = []
    for name in requires:
        if name in infra:                       # one more instance of it
            have = instances.get(name, 1)
            bumped = dict(instances)
            bumped[name] = have + 1
            variant = _build_system(requires, services, infra, service_results,
                                    infra_avail, infra_redundancy=bumped)
        elif name in services:                  # add one replica to a service
            variant = _build_system(requires, services, infra, service_results,
                                    infra_avail, extra_replicas={name: 1},
                                    infra_redundancy=instances)
        else:
            continue
        if name in infra:
            nth = instances.get(name, 1) + 1
            label = f"add {_ordinal(nth)} {name}"
        else:
            label = f"add replica to {name}"
        dt = variant.system_unavailability() * _MINUTES_PER_YEAR
        interventions.append((label, variant.system_availability(), base_dt - dt))

    interventions.sort(key=lambda t: t[2], reverse=True)
    return interventions


# --------------------------------------------------------------------------
# The generic analysis
# --------------------------------------------------------------------------

def failure_rate_for(spec: dict, component: str, measured: float) -> float:
    """Resolve a component's failure_rate with a clear precedence.

    A user-supplied ``[failure_rates]`` entry in the spec wins (this is the
    pre-production escape hatch: you can't observe natural failures before
    launch, so you provide an estimate). Then a ``[failure_rates].default``.
    Otherwise the measured/loaded value is used.
    """
    overrides = spec.get("failure_rates", {})
    if component in overrides:
        return float(overrides[component])
    if "default" in overrides:
        return float(overrides["default"])
    return measured


def analyze(spec: dict, measurements: dict) -> dict:
    """Run all four analysis layers on a spec + its measurements."""
    services = spec["services"]
    infra = spec.get("infrastructure", [])
    ms = measurements.get("services", {})
    mi = measurements.get("infrastructure", {})

    missing = [s for s in services if s not in ms] + \
              [i for i in infra if i not in mi]
    if missing:
        raise ValueError(f"no measurements for: {missing}")

    # Layers 1 & 2, per service.
    service_results = {}
    for name in services:
        p = ms[name]
        c = int(p["replicas"])
        fr = failure_rate_for(spec, name, p["failure_rate"])
        mu_perf = float(p.get("service_rate_effective", p["service_rate"]))
        queue = mmc_metrics(p["arrival_rate"], mu_perf, c)
        tier = replicated_reliability(c, fr, p["repair_rate"], k=1)
        per_replica = component_reliability(fr, p["repair_rate"]).availability
        service_results[name] = ServiceResult(name, c, queue, tier, per_replica)

    # Layer 2, per infrastructure component.
    infra_results = {name: component_reliability(
                         failure_rate_for(spec, name, mi[name]["failure_rate"]),
                         mi[name]["repair_rate"])
                     for name in infra}
    infra_avail = {name: r.availability for name, r in infra_results.items()}

    # Layers 3 & 4, per operation (top event).
    top_results = {}
    instances = infra_instances(spec)
    for op, requires in spec.get("operations", {}).items():
        system = _build_system(requires, services, infra,
                               service_results, infra_avail,
                               infra_redundancy=instances)
        top_results[op] = TopEventResult(
            name=op,
            requires=requires,
            system=system,
            availability=system.system_availability(),
            cut_sets=system.minimal_cut_sets(),
            spofs=system.single_points_of_failure(),
            importance=system.importance_ranking(),
            interventions=_where_to_invest(requires, services, infra,
                                           service_results, infra_avail,
                                           instances=instances),
        )

    return {
        "name": spec.get("name", "system"),
        "services": service_results,
        "infrastructure": infra_results,
        "infra_instances": instances,
        "top_events": top_results,
    }
