"""Tests for the per-endpoint busy-time breakdown (analyzer.hotspots).

  python3 -m pytest analyzer/tests/ -q
  python3 -m analyzer.tests.test_hotspots

No network: a fake Prometheus answers by substring with OTel-shaped series
(service_name, http_route, http_request_method, http_response_status_code).
"""

from __future__ import annotations

import math

from analyzer.collect.promql import Sample
from analyzer.hotspots import endpoint_hotspots, hotspots_explain, hotspots_report

_SPEC = {"name": "H", "services": ["api", "gateway"], "infrastructure": []}
_COUNT = "http_server_request_duration_seconds_count"
_SUM = "http_server_request_duration_seconds_sum"


def _s(svc, route, method, value):
    return Sample({"service_name": svc, "http_route": route,
                   "http_request_method": method}, value)


class FakePrometheus:
    def __init__(self, names, labels, rules):
        self._names, self._labels, self._rules = set(names), labels, rules
        self.calls = []

    def metric_names(self):
        return sorted(self._names)

    def label_values(self, label):
        return sorted(self._labels.get(label, []))

    def instant(self, query, at=None):
        self.calls.append((query, at))
        for substr, samples in self._rules:
            if substr in query:
                return samples
        return []


def _prom(with_routes=True, with_status=True):
    labels = {"service_name": ["api", "gateway"],
              "http_request_method": ["GET", "POST"]}
    if with_routes:
        labels["http_route"] = ["games/{id}", "users", "scores"]
    if with_status:
        labels["http_response_status_code"] = ["200", "500"]
    return FakePrometheus(
        [_COUNT, _SUM], labels,
        [
            # 5xx counter first: its query also contains _count
            ('=~"5.."', [_s("api", "scores", "POST", 2.0)]),
            # mean = sum / count: the only query that mentions _sum
            (_SUM, [_s("api", "games/{id}", "GET", 0.002),
                    _s("api", "users", "GET", 0.050),
                    _s("api", "scores", "POST", 0.010),
                    _s("gateway", "{**catch-all}", "GET", 0.004)]),
            (_COUNT, [_s("api", "games/{id}", "GET", 100.0),
                      _s("api", "users", "GET", 10.0),
                      _s("api", "scores", "POST", 20.0),
                      _s("gateway", "{**catch-all}", "GET", 130.0),
                      _s("other-app", "x", "GET", 999.0)]),
        ])


def test_ranks_endpoints_by_share_of_busy_time():
    hs = endpoint_hotspots(_prom(), _SPEC, "2m")
    api = hs.by_service["api"]
    # busy: games 100*0.002=0.2, users 10*0.05=0.5, scores 20*0.01=0.2 -> 0.9
    assert [r.route for r in api] == ["users", "games/{id}", "scores"]
    assert math.isclose(api[0].share, 0.5 / 0.9)
    assert math.isclose(api[0].mean_ms, 50.0)
    assert math.isclose(sum(r.share for r in api), 1.0)
    assert hs.route_label == "http_route"


def test_busiest_by_time_not_by_traffic():
    hs = endpoint_hotspots(_prom(), _SPEC, "2m")
    api = hs.by_service["api"]
    busiest_traffic = max(api, key=lambda r: r.rate)
    assert busiest_traffic.route == "games/{id}" and api[0].route == "users"


def test_error_share_from_status_label():
    hs = endpoint_hotspots(_prom(), _SPEC, "2m")
    scores = next(r for r in hs.by_service["api"] if r.route == "scores")
    games = next(r for r in hs.by_service["api"] if r.route == "games/{id}")
    assert math.isclose(scores.error_share, 0.1) and games.error_share == 0.0


def test_without_status_label_errors_are_unknown_not_zero():
    hs = endpoint_hotspots(_prom(with_status=False), _SPEC, "2m")
    assert all(r.error_share is None for r in hs.by_service["api"])


def test_ignores_services_outside_the_spec():
    hs = endpoint_hotspots(_prom(), _SPEC, "2m")
    assert set(hs.by_service) == {"api", "gateway"}


def test_no_route_label_is_reported_not_guessed():
    hs = endpoint_hotspots(_prom(with_routes=False), _SPEC, "2m")
    assert hs.by_service == {} and hs.route_label is None
    assert any("no endpoint label" in n for n in hs.notes)
    assert "No per-request breakdown" in hotspots_explain(_SPEC, hs)


def test_no_request_metrics_is_reported():
    prom = FakePrometheus([], {"http_route": ["x"]}, [])
    hs = endpoint_hotspots(prom, _SPEC, "2m")
    assert hs.by_service == {} and any("--discover" in n for n in hs.notes)


def test_at_and_window_reach_every_query():
    prom = _prom()
    endpoint_hotspots(prom, _SPEC, "124s", at="2026-11-03T12:30:00Z")
    assert prom.calls and all(at == "2026-11-03T12:30:00Z" for _, at in prom.calls)
    assert all("[124s]" in q for q, _ in prom.calls)


def test_reports_name_the_hotspot():
    hs = endpoint_hotspots(_prom(), _SPEC, "2m")
    tech = hotspots_report(_SPEC, hs)
    assert "GET users" in tech and "55.6%" in tech and "10.0%" in tech
    plain = hotspots_explain(_SPEC, hs)
    assert "'GET users' takes 56% of its time" in plain
    assert "'POST scores' failed 10%" in plain


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
