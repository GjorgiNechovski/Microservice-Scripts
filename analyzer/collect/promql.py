"""A tiny, dependency-free Prometheus HTTP API client.

Only the few endpoints the collector needs, over ``urllib``:

  * ``instant(query)``      -- run a PromQL instant query, return samples
  * ``metric_names()``      -- every metric name Prometheus knows about
  * ``label_values(label)`` -- every value a label takes

Kept intentionally small and stdlib-only so the analyzer has zero install
footprint. The client never assumes anything about *which* metrics exist; the
collector discovers that at runtime (metric names differ across Kong / Beyla
versions), which is what keeps Phase 1 generic.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


class PrometheusError(RuntimeError):
    """Prometheus was unreachable or returned an error status."""


@dataclass
class Sample:
    """One time series in an instant-query result: its labels and value."""

    labels: dict
    value: float


class Prometheus:
    """Minimal read-only client for the Prometheus HTTP API."""

    def __init__(self, base_url: str = "http://localhost:9090",
                 timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # -- low level -------------------------------------------------------
    def _get(self, path: str, params: dict) -> object:
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                payload = json.load(resp)
        except urllib.error.URLError as exc:
            raise PrometheusError(
                f"cannot reach Prometheus at {self.base_url} ({exc}). "
                f"Is the observability stack up and the port published?"
            ) from exc
        except (json.JSONDecodeError, ValueError) as exc:
            raise PrometheusError(
                f"Prometheus at {self.base_url} returned non-JSON: {exc}"
            ) from exc
        if payload.get("status") != "success":
            raise PrometheusError(
                payload.get("error", "unknown error from Prometheus"))
        return payload["data"]

    # -- queries ---------------------------------------------------------
    def instant(self, query: str) -> list[Sample]:
        """Run an instant query; return one :class:`Sample` per series.

        Non-numeric, missing, and non-finite values (Prometheus reports "NaN"
        for "no data") are skipped rather than raising, so a partially-populated
        system still yields whatever is genuinely measurable.
        """
        data = self._get("/api/v1/query", {"query": query})
        result = data.get("result", []) if isinstance(data, dict) else []
        samples: list[Sample] = []
        for series in result:
            value = series.get("value")  # [ <unix_ts>, "<number>" ]
            if not value or len(value) < 2:
                continue
            try:
                number = float(value[1])
            except (TypeError, ValueError):
                continue
            if not math.isfinite(number):
                continue  # "NaN"/"Inf" -> no data; caller treats as unmeasured
            samples.append(Sample(series.get("metric", {}), number))
        return samples

    def metric_names(self) -> list[str]:
        """Every metric name currently known to Prometheus (sorted)."""
        data = self._get("/api/v1/label/__name__/values", {})
        return sorted(data) if isinstance(data, list) else []

    def label_values(self, label: str) -> list[str]:
        """Every value a given label takes across all series."""
        data = self._get(f"/api/v1/label/{urllib.parse.quote(label)}/values", {})
        return sorted(data) if isinstance(data, list) else []
