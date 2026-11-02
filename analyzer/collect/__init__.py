"""Phase 1 -- collection.

Turn the *running* system (as seen through Prometheus) into the numbers the
analysis layers need, and write them into ``systems/<name>.measured.toml``.

This package measures the **performance** inputs of Layer 1 (M/M/c):

  * ``replicas``      -- how many instances of each service are running (c)
  * ``arrival_rate``  -- requests per second into each service (lambda)
  * ``service_rate``  -- requests per second one replica can serve (mu)

It deliberately does NOT touch ``failure_rate`` / ``repair_rate`` -- those are
the reliability inputs measured by Phase 2 (failure injection), and are left
exactly as they are found.

Everything is pure standard library (``urllib`` + ``json``); no third-party
deps, consistent with the rest of the analyzer.
"""

from analyzer.collect.promql import Prometheus, PrometheusError, Sample
from analyzer.collect.collector import (
    Measurement,
    discover,
    measure,
    render_measured_toml,
    write_measured,
)
from analyzer.collect.loadgen import LoadRun, build_command, load_config, run_load

__all__ = [
    "Prometheus",
    "PrometheusError",
    "Sample",
    "Measurement",
    "discover",
    "measure",
    "render_measured_toml",
    "write_measured",
    "LoadRun",
    "build_command",
    "load_config",
    "run_load",
]
