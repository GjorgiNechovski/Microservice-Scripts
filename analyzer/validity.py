"""Can this run's numbers be believed? -- the CLAUDE.md checklist, automated.

CLAUDE.md lists what to check before reporting any number from a run ("Before
you believe any number"): cAdvisor stayed up, k6 ran its full duration and
its checks passed, every service was measured, k6's request rate agrees with
the measured arrival rate, the host was not saturated. Each one has invalidated
a real run before, silently: a missed cAdvisor scrape makes a 5-replica
service read as 1 replica, a failed setup() makes k6 exit within seconds and
the "load level" is an idle system.

This module runs those checks after each load level and writes the verdict
next to the level's report (validity.txt + validity-explained.txt), and the
sweep's scaling report lists every level's verdict. Two kinds of FAIL mean
different things:

  * the TEST or the MEASUREMENT broke (it ran short, cAdvisor missed scrapes,
    a service went unmeasured): INVALID, do not report this level's numbers;
  * the SYSTEM failed requests (k6's checks, http_req_failed): OVERLOADED. The
    test worked and the failure rate is the result; only the capacity math
    can't use the level, since failed requests count as cheap arrivals. If a
    light load fails as well, suspect the test instead (an expired token, a
    wrong URL).

A WARN means: report the numbers with the caveat shown.

It reads only what the run already produced (k6's own summary) and Prometheus;
it never invents a number -- a check whose inputs are missing says SKIP.

Re-check a finished run without re-running any load:
    python3 -m analyzer.validity systems/<system>.toml --out analysis/<dir>
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"

# Thresholds. CLAUDE.md: k6's rate and the measured arrival rate "agreed within
# 1 to 2% in testing, slightly lower on the first level"; a run is cut short
# when setup() fails; one missed cAdvisor scrape already corrupts replica counts.
_MIN_DURATION_SHARE = 0.9
_CHECKS_OK, _CHECKS_WARN = 0.99, 0.95
_ERRORS_OK, _ERRORS_WARN = 0.01, 0.05
_CADVISOR_UP = 0.999
_RATE_AGREEMENT = 0.05
_HOST_SATURATED = 0.85


@dataclass
class Check:
    name: str
    status: str        # PASS | WARN | FAIL | SKIP
    detail: str
    outcome: bool = False   # judges how the SYSTEM coped (its failed requests),
                            # not whether the test and measurement worked


@dataclass
class Validity:
    label: str         # e.g. "load @ 200 VUs"
    checks: list
    # what check 7 measured, kept as numbers for the diagnosis (diagnose.py)
    host_cores: float | None = None
    cpu_by_service: dict = field(default_factory=dict)   # compose service -> cores

    @property
    def valid(self) -> bool:
        """The test and its measurement worked (the system may still have
        failed requests: see :attr:`overloaded`)."""
        return not any(c.status == FAIL and not c.outcome for c in self.checks)

    @property
    def overloaded(self) -> bool:
        """The test worked, but the system failed too many requests: a result,
        though its capacity numbers (mu, rho, replicas) read too low."""
        return self.valid and any(c.status == FAIL and c.outcome
                                  for c in self.checks)

    @property
    def failed_share(self) -> str | None:
        """The failed-requests detail ("74.54% of requests failed"), if any."""
        for c in self.checks:
            if c.name == "requests succeeded" and c.status != SKIP:
                return c.detail.split(" -- ")[0]
        return None

    @property
    def verdict(self) -> str:
        if not self.valid:
            return "INVALID"
        if self.overloaded:
            return "OVERLOADED"
        if any(c.status == WARN for c in self.checks):
            return "VALID, with warnings"
        return "VALID"


@dataclass
class K6Summary:
    requests: int | None = None
    rate: float | None = None          # http_reqs per second
    failed: float | None = None        # http_req_failed, share 0..1
    checks_passed: float | None = None  # share 0..1
    seconds: float | None = None       # how long the test ran
    avg_ms: float | None = None        # http_req_duration, end to end
    median_ms: float | None = None
    p95_ms: float | None = None


# ---------------------------------------------------------------------------
# k6 output
# ---------------------------------------------------------------------------

def _pct(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text, re.MULTILINE)
    return float(m.group(1)) / 100.0 if m else None


_UNIT_MS = {"ns": 1e-6, "µs": 1e-3, "us": 1e-3, "ms": 1.0, "s": 1000.0}


def k6_ms(value: str) -> float | None:
    """A k6 duration ('423.54µs', '20.48ms', '1.37s', '1m2.3s') in ms."""
    value = value.replace("Âµ", "µ").replace("μ", "µ")   # mis-decoded / Greek mu
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m(?!s))?([\d.]+)(ns|µs|us|ms|s)",
                     value.strip())
    if not m:
        return None
    h, mnt, num, unit = m.groups()
    return ((int(h or 0) * 3600 + int(mnt or 0) * 60) * 1000.0
            + float(num) * _UNIT_MS[unit])


def parse_k6_summary(text: str) -> K6Summary:
    """Read k6's end-of-test summary (both the v0.x and the v1.x layouts).

    v1.x:  checks_succeeded...: 100.00% 7427 out of 7427
           http_req_failed....: 0.00%   0 out of 7429
    v0.x:  checks.............: 100.00% ✓ 7427 ✗ 0
           http_req_failed....: 0.00%   ✓ 0    ✗ 7429
    both:  http_reqs..........: 7429    165.005832/s
           http_req_duration..: avg=20.48ms min=1.18ms med=3.7ms ... p(95)=43.67ms
           running (2m00.4s), 000/200 VUs, ...   (progress lines; last wins)
    """
    s = K6Summary()
    m = re.search(r"^\s*http_req_duration\.*:\s*(.+)$", text, re.MULTILINE)
    if m:
        fields = dict(re.findall(r"(avg|med|p\(95\))=(\S+)", m.group(1)))
        s.avg_ms = k6_ms(fields["avg"]) if "avg" in fields else None
        s.median_ms = k6_ms(fields["med"]) if "med" in fields else None
        s.p95_ms = k6_ms(fields["p(95)"]) if "p(95)" in fields else None
    m = re.search(r"^\s*http_reqs\.*:\s*(\d+)\s+([\d.]+)/s", text, re.MULTILINE)
    if m:
        s.requests, s.rate = int(m.group(1)), float(m.group(2))
    s.failed = _pct(r"^\s*http_req_failed\.*:\s*([\d.]+)%", text)
    s.checks_passed = _pct(r"^\s*checks_succeeded\.*:\s*([\d.]+)%", text)
    if s.checks_passed is None:
        s.checks_passed = _pct(r"^\s*checks\.*:\s*([\d.]+)%", text)
    runs = re.findall(r"running \((?:(\d+)h)?(?:(\d+)m)?([\d.]+)s\)", text)
    if runs:
        h, mnt, sec = runs[-1]
        s.seconds = int(h or 0) * 3600 + int(mnt or 0) * 60 + float(sec)
    return s


def duration_seconds(text: str) -> float | None:
    """'2m' / '90s' / '1h30m' / '1m30s' -> seconds."""
    m = re.fullmatch(r"\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+(?:\.\d+)?)s)?\s*",
                     str(text))
    if not m or not any(m.groups()):
        return None
    h, mnt, sec = m.groups()
    return int(h or 0) * 3600 + int(mnt or 0) * 60 + float(sec or 0)


def planned_seconds(cfg: dict | None) -> float | None:
    """How long the spec asked the load to run (None when the script decides)."""
    if not cfg:
        return None
    if cfg.get("stages"):
        parts = [duration_seconds(st["duration"]) for st in cfg["stages"]]
        return sum(parts) if None not in parts else None
    if cfg.get("vus"):
        return duration_seconds(cfg.get("duration", "5m"))
    return None


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def _one(prom, query, at):
    from analyzer.collect.collector import _instant
    samples = _instant(prom, query, at)
    return samples[0].value if samples else None


def _labelled(prom, query, label, at):
    from analyzer.collect.collector import _instant
    return {s.labels.get(label, "?"): s.value for s in _instant(prom, query, at)}


def check_run(spec, prom, rows, k6: K6Summary, planned: float | None,
              window: str, label: str, at=None) -> Validity:
    """Run every check for one load run. ``rows`` are collector Measurements."""
    from analyzer.collect.collector import _COMPOSE_LABEL
    from analyzer.collect.promql import PrometheusError

    checks = []

    # 1. k6 ran for the whole planned duration (setup() failures exit early).
    if k6.seconds is None or planned is None:
        checks.append(Check("load ran its full duration", SKIP,
                            "no k6 progress lines or no planned duration"))
    elif k6.seconds >= _MIN_DURATION_SHARE * planned:
        checks.append(Check("load ran its full duration", PASS,
                            f"{k6.seconds:.0f} s of {planned:.0f} s planned"))
    else:
        checks.append(Check("load ran its full duration", FAIL,
                            f"only {k6.seconds:.0f} s of {planned:.0f} s planned: "
                            "setup() failed or k6 was stopped -- this measured "
                            "a mostly idle system"))

    # 2. k6's own checks mostly passed.
    if k6.checks_passed is None:
        checks.append(Check("k6 checks passed", SKIP, "the script has no checks"))
    else:
        st = PASS if k6.checks_passed >= _CHECKS_OK else (
            WARN if k6.checks_passed >= _CHECKS_WARN else FAIL)
        checks.append(Check("k6 checks passed", st,
                            f"{k6.checks_passed * 100:.2f}% of checks passed",
                            outcome=True))

    # 3. Requests mostly succeeded.
    if k6.failed is None:
        checks.append(Check("requests succeeded", SKIP, "no http_req_failed in output"))
    else:
        st = PASS if k6.failed <= _ERRORS_OK else (
            WARN if k6.failed <= _ERRORS_WARN else FAIL)
        checks.append(Check("requests succeeded", st,
                            f"{k6.failed * 100:.2f}% of requests failed"
                            + ("" if st == PASS else
                               " -- errors are counted as arrivals but are "
                               "usually cheap, so μ reads too high"),
                            outcome=True))

    try:
        # 4. cAdvisor answered every scrape (replica counts depend on it).
        up = _one(prom, f'avg_over_time(up{{job="cadvisor"}}[{window}])', at)
        if up is None:
            checks.append(Check("cAdvisor stayed up", SKIP, "no 'cadvisor' scrape job"))
        elif up >= _CADVISOR_UP:
            checks.append(Check("cAdvisor stayed up", PASS,
                                "every scrape answered during the run"))
        else:
            checks.append(Check("cAdvisor stayed up", FAIL,
                                f"answered {up * 100:.1f}% of scrapes: a missed "
                                "scrape makes replicas read low and corrupts ρ"))

        # inputs for check 7 (host saturation): everything shares one machine.
        cores = _one(prom, "max(machine_cpu_cores)", at)
        per_service = _labelled(
            prom, f"sum by ({_COMPOSE_LABEL})(rate(container_cpu_usage_seconds_total"
                  f'{{name!=""}}[{window}]))', _COMPOSE_LABEL, at)
        used = sum(per_service.values()) if per_service else None
    except PrometheusError as exc:
        up = cores = used = None
        per_service = {}
        checks.append(Check("cAdvisor stayed up", SKIP, f"Prometheus: {exc}"))

    # 5. every spec service has λ and μ.
    missing = [m.name for m in rows if m.arrival_rate is None or m.service_rate is None]
    if not rows:
        checks.append(Check("every service measured", FAIL, "nothing was measured"))
    elif missing:
        checks.append(Check("every service measured", FAIL,
                            f"no λ or μ for: {', '.join(missing)} -- its numbers "
                            "are placeholders (read its logs: crash-looping?)"))
    else:
        checks.append(Check("every service measured", PASS,
                            f"λ and μ for all {len(rows)} services"))

    # 6. k6's request rate matches the entry point's measured arrival rate.
    rates = [m.arrival_rate for m in rows if m.arrival_rate is not None]
    if k6.rate is None or not rates:
        checks.append(Check("k6 rate matches measured λ", SKIP,
                            "k6 rate or measured λ missing"))
    else:
        entry = max(rates)
        diff = abs(entry - k6.rate) / k6.rate if k6.rate > 0 else 0.0
        st = PASS if diff <= _RATE_AGREEMENT else WARN
        checks.append(Check("k6 rate matches measured λ", st,
                            f"k6 sent {k6.rate:.1f} req/s, busiest service "
                            f"received {entry:.1f} req/s ({diff * 100:.1f}% apart)"
                            + ("" if st == PASS else
                               " -- telemetry names, a second entry point, or "
                               "the run window may be off")))

    # 7. the host was not the bottleneck.
    if cores is None or used is None:
        checks.append(Check("host not saturated", SKIP, "no cAdvisor CPU metrics"))
    else:
        top = sorted(per_service.items(), key=lambda kv: kv[1], reverse=True)[:3]
        busiest = ", ".join(f"{k} {v:.2f}" for k, v in top)
        share = used / cores if cores else 0.0
        st = PASS if share < _HOST_SATURATED else WARN
        checks.append(Check("host not saturated", st,
                            f"containers used {used:.1f} of {cores:.0f} cores "
                            f"({share * 100:.0f}%); busiest: {busiest}"
                            + ("" if st == PASS else
                               " -- the numbers describe this host, not the "
                               "services")))
    return Validity(label, checks, cores, dict(per_service or {}))


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def validity_report(v: Validity) -> str:
    out = [f"Run validity -- {v.label}: {v.verdict}", "-" * 74]
    for c in v.checks:
        out.append(f"  {c.status:<5} {c.name:<28} {c.detail}")
    out.append("")
    if not v.valid:
        out.append("  INVALID: the test or its measurement broke; do not report "
                   "this run's numbers")
        out.append("  (CLAUDE.md, 'Before you believe any number').")
    elif v.overloaded:
        out += [f"  OVERLOADED: the test worked, but the system could not keep up "
                f"({v.failed_share or 'checks failed'}).",
                "  The failure rate and response times are results. The capacity "
                "math (μ, ρ, replica",
                "  counts) can't use this level: failed requests count as arrivals "
                "but cost little,",
                "  so the services look less busy than they were. If a light load "
                "fails as well,",
                "  suspect the test instead (an expired token, a wrong URL)."]
    elif v.verdict != "VALID":
        out.append("  Report these numbers only with the warnings above.")
    return "\n".join(out) + "\n"


_PLAIN = {
    "load ran its full duration": "the test traffic ran for as long as planned",
    "k6 checks passed": "the test got the answers it expected",
    "requests succeeded": "requests did not fail",
    "cAdvisor stayed up": "the monitor that counts containers never missed a beat",
    "every service measured": "every part of the system was measured",
    "k6 rate matches measured λ": "the traffic sent matches the traffic received",
    "host not saturated": "the machine itself had capacity to spare",
}


def validity_explain(v: Validity) -> str:
    good = [c for c in v.checks if c.status == PASS]
    warn = [c for c in v.checks if c.status == WARN]
    bad = [c for c in v.checks if c.status == FAIL]
    out = [f"CAN THESE NUMBERS BE TRUSTED? ({v.label})", "-" * 70]
    if not v.valid:
        out.append("  No. Something went wrong with the test itself, so its "
                   "numbers")
        out.append("  should not be used:")
        for c in bad:
            if not c.outcome:
                out.append(f"    ✗ not true: {_PLAIN.get(c.name, c.name)} "
                           f"({c.detail})")
    elif v.overloaded:
        out.append("  Yes, and they say the system couldn't keep up at this load")
        out.append(f"  ({v.failed_share or 'its checks failed'}). The test "
                   "worked: that failure rate is")
        out.append("  the answer, not a fault. The speed and failure numbers "
                   "stand; only the")
        out.append("  estimates of how much it can take leave this load out, "
                   "because failed")
        out.append("  requests make it look less busy than it was. (If a light "
                   "load fails too,")
        out.append("  the test itself is wrong: an expired login, a wrong "
                   "address.)")
    else:
        out.append("  Yes" + (", with a caveat:" if warn else "."))
        for c in warn:
            out.append(f"    ! only partly true: {_PLAIN.get(c.name, c.name)} "
                       f"({c.detail})")
    if good:
        out.append("  Confirmed: " + "; ".join(_PLAIN.get(c.name, c.name)
                                              for c in good) + ".")
    out.append("")
    return "\n".join(out)


def validity_summary(levels) -> str:
    """Scaling-report section: one verdict per sweep level (skips if none)."""
    rows = [(lv.vus, lv.validity) for lv in levels
            if getattr(lv, "validity", None) is not None]
    if not rows:
        return ""
    out = ["", "=" * 74, "Run validity per level (details: load-<N>/validity.txt)",
           "=" * 74]
    for vus, v in rows:
        flagged = [c.name for c in v.checks if c.status in (FAIL, WARN)]
        out.append(f"  {vus:>6} VUs  {v.verdict:<22}"
                   + (f" ({', '.join(flagged)})" if flagged else ""))
    if any(not v.valid for _, v in rows):
        out.append("  INVALID = the test or its measurement broke: those levels "
                   "must not be reported;")
        out.append("    the verdicts above still use them.")
    over = [vus for vus, v in rows if v.overloaded]
    if over:
        out.append("  OVERLOADED = the system failed too many requests: the "
                   "failure rate is the result;")
        out.append("    the capacity math reads those levels as less busy than "
                   "they were.")
        if over[0] == rows[0][0]:
            out.append("  Even the lightest level failed: make sure the test "
                       "itself works (an expired")
            out.append("    token, a wrong URL) before reading this as overload.")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# CLI: re-check a finished run from its saved output
# ---------------------------------------------------------------------------

def recheck_saved_run(spec, prom, cfg, label, run_dir, vus=None):
    """Re-check one finished run from its saved k6-output.txt; write the files.

    Returns ``(validity, window, at)``: ``at`` is when the run ended and
    ``window`` the span the pipeline measured, so other read-only analyses
    (hotspots) can look at exactly the same data.
    """
    from analyzer.collect import loadgen
    from analyzer.collect.collector import measure

    path = os.path.join(run_dir, "k6-output.txt")
    with open(path, encoding="utf-8") as fh:
        k6 = parse_k6_summary(fh.read())
    # The pipeline writes k6-output.txt the moment k6 exits and measures right
    # after, so its modification time is the end of the measured window.
    end = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
    at = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    window = loadgen.measurement_window(k6.seconds or 60)
    planned = planned_seconds({**cfg, "vus": vus} if cfg and vus else cfg)
    rows = measure(prom, spec, window=window, at=at)
    v = check_run(spec, prom, rows, k6, planned, window, label, at=at)
    with open(os.path.join(run_dir, "validity.txt"), "w", encoding="utf-8") as fh:
        fh.write(validity_report(v))
    with open(os.path.join(run_dir, "validity-explained.txt"), "w",
              encoding="utf-8") as fh:
        fh.write(validity_explain(v))
    return v, window, at


def _run_dirs(out: str) -> list:
    """(label, dir, vus) for every load-<N>/ in ``out``, or ``out`` itself."""
    found = []
    for d in glob.glob(os.path.join(out, "load-*")):
        m = re.search(r"load-(\d+)$", d)
        if m and os.path.isfile(os.path.join(d, "k6-output.txt")):
            found.append((f"load @ {m.group(1)} VUs", d, int(m.group(1))))
    if not found and os.path.isfile(os.path.join(out, "k6-output.txt")):
        found.append(("single run", out, None))
    return sorted(found, key=lambda x: x[2] or 0)


def main(argv=None) -> int:
    from analyzer import utf8_stdio

    utf8_stdio()
    from analyzer.collect import loadgen
    from analyzer.collect.promql import Prometheus, PrometheusError
    from analyzer.spec import load_system

    parser = argparse.ArgumentParser(
        prog="python3 -m analyzer.validity",
        description="Re-check a finished run's validity from its saved k6 "
                    "output and Prometheus. Read-only: no load, no restarts.")
    parser.add_argument("spec", help="structural system spec path")
    parser.add_argument("--out", default="analysis",
                        help="the run's output directory (default: %(default)s)")
    parser.add_argument("--prometheus", default="http://localhost:9090",
                        help="Prometheus URL (default: %(default)s)")
    args = parser.parse_args(argv)

    try:
        spec = load_system(args.spec)
    except (OSError, ValueError) as exc:
        print(f"error: could not load spec '{args.spec}': {exc}", file=sys.stderr)
        return 2
    try:
        cfg = loadgen.load_config(spec, args.spec)
    except (ValueError, FileNotFoundError):
        cfg = None
    runs = _run_dirs(args.out)
    if not runs:
        print(f"error: no k6-output.txt under {args.out}", file=sys.stderr)
        return 2

    prom = Prometheus(args.prometheus)
    any_invalid = False
    for label, d, vus in runs:
        try:
            v, _window, _at = recheck_saved_run(spec, prom, cfg, label, d, vus)
        except PrometheusError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        any_invalid |= not v.valid
        sys.stdout.write(validity_report(v) + "\n")
    return 1 if any_invalid else 0


if __name__ == "__main__":
    raise SystemExit(main())
