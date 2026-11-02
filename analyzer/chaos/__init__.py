"""Phase 2 -- failure injection.

Measure the reliability inputs of Layer 2 (CTMC) from the running system:

  * **repair_rate** = 1 / MTTR, measured by killing a container and timing how
    long it takes to recover (auto-restart, or a harness ``docker start``).
  * **failure_rate** is *observed* from Docker restart history where any
    restarts have occurred, else the prior/assumption is preserved (natural
    MTTF cannot be induced in a short lab run).

Results are written into ``systems/<name>.measured.toml``, preserving the
Phase-1 performance numbers. Dependency-free; the Docker access is injectable
so the logic is testable without Docker.
"""

from analyzer.chaos.dockercli import ContainerState, Docker, DockerError
from analyzer.chaos.injector import (
    ComponentPlan,
    MttrResult,
    component_to_service,
    measure_mttr,
    observed_failure_rate,
    plan,
    readiness_port,
    render_measured_toml,
    resolve_containers,
    write_measured,
)

__all__ = [
    "ContainerState",
    "Docker",
    "DockerError",
    "ComponentPlan",
    "MttrResult",
    "component_to_service",
    "measure_mttr",
    "observed_failure_rate",
    "plan",
    "readiness_port",
    "render_measured_toml",
    "resolve_containers",
    "write_measured",
]
