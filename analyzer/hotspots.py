"""Where does each service spend its time? -- a per-endpoint breakdown.

The diagnosis (diagnose.py) can say a service is overloaded because its
requests are heavy, and that the fix is to optimize rather than scale. It
cannot say WHICH requests: "the specific cause still needs code or traces".
Most of that gap closes with numbers the stack already has. The same request
histograms the collector reads per service also carry the endpoint (OTel's
``http_route``, Kong's ``route``), so per endpoint we can measure

    lambda_r   arrival rate of that endpoint                  (req/s)
    S_r        its mean service time                          (ms)
    busy_r     lambda_r * S_r = seconds of work per second    (busy share)

and rank the endpoints by their share of the service's total busy time. The
top of that list is where optimizing pays: halving S_r of an endpoint that
holds 60% of the busy time frees far more capacity than any other change.

Generic like the collector: the metric and label names are resolved at run
time, never hard-coded, and a system without an endpoint label is reported as
such instead of guessed at. A service's busy time here is the time its own
requests took, so for a caller it includes the callees' time (see CLAUDE.md).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from analyzer.collect.collector import (
    _REQUEST_TOTAL_CANDIDATES,
    _first_present,
    _instant,
    _resolve_latency,
    _resolve_service_label,
)
from analyzer.collect.promql import Prometheus, PrometheusError

# Label carrying the endpoint on request metrics -- first that has values wins.
# OTel HTTP semconv: http_route; Kong's prometheus plugin: route.
_ROUTE_LABEL_CANDIDATES = ("http_route", "route", "endpoint", "http_target")
# Optional labels shown next to the route when present.
_METHOD_LABEL_CANDIDATES = ("http_request_method", "http_method", "method")
_STATUS_LABEL_CANDIDATES = ("http_response_status_code", "http_status_code",
                            "status_code", "code")


@dataclass
class EndpointLoad:
    service: str
    route: str
    method: str              # "" when the metrics carry no method label
    rate: float              # lambda_r, req/s
    mean_ms: float | None    # S_r; None when no latency samples
    busy: float              # lambda_r * S_r, seconds of work per second
    share: float             # busy / total busy of the service (0..1)
    error_share: float | None  # share of 5xx responses, if a status label exists


@dataclass
class Hotspots:
    window: str
    route_label: str | None
    by_service: dict         # service -> [EndpointLoad], busiest first
    notes: list


def _first_label_with_values(prom, candidates) -> str | None:
    for label in candidates:
        try:
            if prom.label_values(label):
                return label
        except PrometheusError:
            continue
    return None


def endpoint_hotspots(prom, spec, window="5m", at=None) -> Hotspots:
    """Rank every spec service's endpoints by their share of its busy time."""
    names = set(prom.metric_names())
    request_metric = _first_present(_REQUEST_TOTAL_CANDIDATES, names)
    latency = _resolve_latency(names)
    service_label = _resolve_service_label(prom, spec)
    route_label = _first_label_with_values(prom, _ROUTE_LABEL_CANDIDATES)
    notes: list = []
    if request_metric is None or latency is None:
        notes.append("No request-count or service-time metric -> no per-endpoint "
                     "breakdown (run `analyzer.collect <spec> --discover`).")
        return Hotspots(window, route_label, {}, notes)
    if route_label is None:
        notes.append("The request metrics carry no endpoint label (tried "
                     f"{', '.join(_ROUTE_LABEL_CANDIDATES)}) -> no per-endpoint "
                     "breakdown. OTel HTTP instrumentation adds http_route.")
        return Hotspots(window, None, {}, notes)

    method_label = _first_label_with_values(prom, _METHOD_LABEL_CANDIDATES)
    status_label = _first_label_with_values(prom, _STATUS_LABEL_CANDIDATES)
    group = ", ".join(x for x in (service_label, route_label, method_label) if x)

    def key(labels):
        return (labels.get(service_label, ""), labels.get(route_label, ""),
                labels.get(method_label, "") if method_label else "")

    rates = {key(s.labels): s.value for s in _instant(
        prom, f"sum by ({group})(rate({request_metric}[{window}]))", at)}
    means = {key(s.labels): s.value * latency.to_seconds for s in _instant(
        prom, latency.mean_query(window, group), at)}
    errors = {}
    if status_label:
        bad = {key(s.labels): s.value for s in _instant(
            prom, f'sum by ({group})(rate({request_metric}'
                  f'{{{status_label}=~"5.."}}[{window}]))', at)}
        errors = {k: bad.get(k, 0.0) / r for k, r in rates.items() if r > 0}

    wanted = set(spec["services"])
    by_service: dict = {}
    for (svc, route, method), rate in rates.items():
        if svc not in wanted or rate <= 0:
            continue
        mean_s = means.get((svc, route, method))
        busy = rate * mean_s if mean_s is not None else 0.0
        by_service.setdefault(svc, []).append(EndpointLoad(
            svc, route or "(no route)", method, rate,
            mean_s * 1000.0 if mean_s is not None else None, busy, 0.0,
            errors.get((svc, route, method)) if status_label else None))

    for svc, rows in by_service.items():
        total = sum(r.busy for r in rows)
        for r in rows:
            r.share = r.busy / total if total > 0 else 0.0
        rows.sort(key=lambda r: (r.busy, r.rate), reverse=True)
    missing = sorted(wanted - set(by_service))
    if missing:
        notes.append(f"No endpoint traffic in the last {window} for: "
                     f"{', '.join(missing)}.")
    return Hotspots(window, route_label, by_service, notes)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def _endpoint(r: EndpointLoad) -> str:
    return f"{r.method} {r.route}".strip()


def hotspots_report(spec, hs: Hotspots, top: int = 8) -> str:
    out = ["", "=" * 74,
           f"Where each service spends its time -- per endpoint (window {hs.window})",
           "=" * 74,
           "busy = λ × S, the seconds of work the endpoint costs per second; its",
           "share of the service's busy time is where optimizing pays most.",
           f"(endpoint label: {hs.route_label or 'none'})"]
    for svc in spec["services"]:
        rows = hs.by_service.get(svc)
        if not rows:
            continue
        total_rate = sum(r.rate for r in rows)
        total_busy = sum(r.busy for r in rows)
        out.append("")
        out.append(f"{svc}: {total_rate:.1f} req/s, {total_busy:.2f} s of work "
                   f"per second over {len(rows)} endpoint(s)")
        out.append(f"  {'share':>6} {'λ (req/s)':>10} {'S (ms)':>8} {'5xx':>6}   endpoint")
        for r in rows[:top]:
            s = f"{r.mean_ms:.1f}" if r.mean_ms is not None else "n/a"
            e = f"{r.error_share * 100:.1f}%" if r.error_share is not None else "-"
            out.append(f"  {r.share * 100:5.1f}% {r.rate:>10.1f} {s:>8} {e:>6}   "
                       f"{_endpoint(r)}")
        if len(rows) > top:
            rest = sum(r.share for r in rows[top:])
            out.append(f"  {rest * 100:5.1f}%  ... {len(rows) - top} more endpoint(s)")
    for note in hs.notes:
        out.append(f"  note: {note}")
    return "\n".join(out) + "\n"


def hotspots_explain(spec, hs: Hotspots) -> str:
    out = ["WHICH REQUESTS KEEP EACH PART BUSY", "-" * 70,
           "Every kind of request costs some work. Multiply how often it comes by",
           "how long it takes, and you get how much of the part's time it eats.",
           "Speeding up the biggest eater frees the most room.", ""]
    for svc in spec["services"]:
        rows = hs.by_service.get(svc)
        if not rows:
            continue
        head = rows[0]
        ms = f"{head.mean_ms:.0f} ms" if head.mean_ms is not None else "an unknown time"
        out.append(f"  • {svc}: '{_endpoint(head)}' takes {head.share * 100:.0f}% of its "
                   f"time ({head.rate:.0f} requests a second, {ms} each).")
        if len(rows) > 1:
            nxt = rows[1]
            out.append(f"    Next: '{_endpoint(nxt)}' at {nxt.share * 100:.0f}%.")
        failing = [r for r in rows if r.error_share and r.error_share >= 0.01]
        for r in failing[:3]:
            out.append(f"    '{_endpoint(r)}' failed {r.error_share * 100:.0f}% of "
                       "the time (server errors).")
        out.append("")
    if not hs.by_service:
        out.append("  No per-request breakdown is available for this system:")
        for note in hs.notes:
            out.append(f"  {note}")
        out.append("")
    out.append("  A caller's time includes the parts it waits on, so a gateway's")
    out.append("  busiest request is usually the busiest one behind it as well.")
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    from analyzer import utf8_stdio

    utf8_stdio()
    from analyzer.spec import load_system

    parser = argparse.ArgumentParser(
        prog="python3 -m analyzer.hotspots",
        description="Per-endpoint breakdown: which requests keep each service "
                    "busy. Read-only Prometheus queries; no load, no restarts.")
    parser.add_argument("spec", help="structural system spec path")
    parser.add_argument("--window", default="5m",
                        help="PromQL rate window (default: %(default)s)")
    parser.add_argument("--at", default=None,
                        help="evaluate at this time instead of now (RFC 3339, "
                             "e.g. 2026-11-03T12:30:00Z, or a unix timestamp) "
                             "-- to re-read a past run")
    parser.add_argument("--top", type=int, default=8,
                        help="endpoints shown per service (default: %(default)s)")
    parser.add_argument("--prometheus", default="http://localhost:9090",
                        help="Prometheus URL (default: %(default)s)")
    args = parser.parse_args(argv)

    try:
        spec = load_system(args.spec)
    except (OSError, ValueError) as exc:
        print(f"error: could not load spec '{args.spec}': {exc}", file=sys.stderr)
        return 2
    try:
        hs = endpoint_hotspots(Prometheus(args.prometheus), spec, args.window,
                               at=args.at)
    except PrometheusError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(hotspots_report(spec, hs, args.top))
    print()
    sys.stdout.write(hotspots_explain(spec, hs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
