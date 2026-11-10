"""Name a hidden limit from the containers' own logs.

When throughput levels off while the CPU idles (diagnose.hidden_limit), the
metrics stop at "something caps how much work runs at once". What it is -- a
database connection pool, a worker limit, a cache client -- is usually written
in the logs of the service whose errors start there, as a well-known error
message. This reads each component's ``docker logs`` over a load level's
window, read-only, and counts the lines that match :data:`SIGNATURES`:

  * causes   -- a pool, worker or OS limit ran out (``cause=True`` kinds);
  * symptoms -- timeouts and refused connections, which a caller logs when
                something it waits on is stuck.

A line counts once, for the first cause it matches, else the first symptom.
Per level the counts are saved as ``load-<N>/log-signals.json``; across the
sweep the cause with the most lines (first seen at or after the lighter levels
stayed clean) is the suspect the diagnosis names.

The signatures are library and server messages, not application names: extend
:data:`SIGNATURES`, or add ``[logs] extra`` entries in the spec, for a stack
not covered. Standard library only.

    # scan a finished sweep's levels from the containers' saved logs (the
    # containers must still exist; read-only):
    python3 -m analyzer.logscan systems/finki-blogger.toml --out analysis
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field

_FILE = "log-signals.json"
_EXAMPLE_CHARS = 200


@dataclass(frozen=True)
class Signature:
    key: str                      # short id, shown in reports
    kind: str                     # a key of KINDS
    pattern: str                  # regex, matched case-insensitively


# kind -> (plain-language name, is a cause rather than a symptom)
KINDS = {
    "db-pool": ("the database connection pool inside the service ran out", True),
    "db-server": ("the database refused more connections", True),
    "cache-pool": ("the cache client ran out of connections or time", True),
    "threads": ("the service ran out of worker threads or processes", True),
    "os-limit": ("the operating system ran out of sockets or file handles", True),
    "memory": ("memory ran out", True),
    "timeout": ("calls timed out (a symptom: something they wait on is stuck)",
                False),
    "refused": ("connections were refused or reset (a symptom)", False),
}

# Causes first: a line counts for the first cause it matches.
SIGNATURES = [
    Signature("mongo-wait-queue", "db-pool",
              r"MongoWaitQueueFullException|wait queue for acquiring a "
              r"connection .{0,200}is full"),
    Signature("mongo-pool-paused", "db-pool", r"MongoConnectionPoolPausedException"),
    Signature("sqlclient-pool", "db-pool",
              r"timeout period elapsed prior to obtaining a connection from the "
              r"pool"),
    Signature("npgsql-pool", "db-pool", r"connection pool has been exhausted"),
    Signature("hikari-pool", "db-pool",
              r"Connection is not available, request timed out after"),
    Signature("sqlalchemy-pool", "db-pool",
              r"QueuePool limit of size \d+ overflow \d+ reached"),
    Signature("prisma-pool", "db-pool",
              r"Timed out fetching a new connection from the connection pool"),
    Signature("knex-pool", "db-pool", r"Knex: Timeout acquiring a connection"),
    Signature("mysql-too-many", "db-server", r"\bToo many connections\b"),
    Signature("postgres-too-many", "db-server",
              r"too many clients already|remaining connection slots are reserved"),
    Signature("mongo-max-incoming", "db-server",
              r"connection refused because too many open connections"),
    Signature("redis-timeout", "cache-pool",
              r"RedisTimeoutException|Timeout performing \w+ \("),
    Signature("redis-no-connection", "cache-pool",
              r"No connection is (?:active/)?available to service this operation"
              r"|RedisConnectionException"),
    Signature("redis-max-clients", "cache-pool", r"max number of clients reached"),
    Signature("dotnet-threadpool", "threads", r"thread ?pool starvation"),
    Signature("java-rejected", "threads", r"RejectedExecutionException"),
    Signature("gunicorn-timeout", "threads", r"\bWORKER TIMEOUT\b"),
    Signature("phpfpm-max-children", "threads", r"reached (?:pm\.)?max_children"),
    Signature("uwsgi-listen-queue", "threads", r"listen queue of socket .{0,80}full"),
    Signature("open-files", "os-limit", r"Too many open files|\bEMFILE\b"),
    Signature("ephemeral-ports", "os-limit",
              r"Cannot assign requested address|\bEADDRNOTAVAIL\b"),
    Signature("out-of-memory", "memory",
              r"OutOfMemory(?:Error|Exception)|JavaScript heap out of memory"
              r"|\bout of memory\b"),
    Signature("timeout", "timeout",
              r"TaskCanceledException|HttpClient\.Timeout|\bETIMEDOUT\b"
              r"|Read timed out|deadline exceeded|\btimed out\b"),
    Signature("refused", "refused",
              r"Connection refused|\bECONNREFUSED\b|\bECONNRESET\b"
              r"|connection reset by peer|No destination available"
              r"|upstream connect error"),
]


def signatures_for(spec) -> list:
    """The built-in signatures plus the spec's ``[logs] extra`` entries
    (``{key, kind, pattern}``; ``kind`` one of :data:`KINDS`)."""
    extra = []
    for e in spec.get("logs", {}).get("extra", []):
        if e.get("kind") not in KINDS:
            raise ValueError(f"[logs] extra '{e.get('key')}': kind must be one "
                             f"of {', '.join(KINDS)}")
        extra.append(Signature(str(e["key"]), e["kind"], str(e["pattern"])))
    # spec entries are tried before the built-ins of the same rank
    return ([s for s in extra if KINDS[s.kind][1]]
            + [s for s in SIGNATURES if KINDS[s.kind][1]]
            + [s for s in extra if not KINDS[s.kind][1]]
            + [s for s in SIGNATURES if not KINDS[s.kind][1]])


def enabled(spec) -> bool:
    return bool(spec.get("logs", {}).get("scan", True))


class _Matcher:
    """Two combined regexes, causes then symptoms; ``match`` returns the
    signature a line counts for, or None."""

    def __init__(self, signatures):
        self.sigs = list(signatures)
        causes = [(i, s) for i, s in enumerate(self.sigs) if KINDS[s.kind][1]]
        symptoms = [(i, s) for i, s in enumerate(self.sigs)
                    if not KINDS[s.kind][1]]
        self.rx = [re.compile("|".join(f"(?P<g{i}>{s.pattern})"
                                       for i, s in group), re.IGNORECASE)
                   for group in (causes, symptoms) if group]

    def match(self, line):
        for rx in self.rx:
            m = rx.search(line)
            if m:
                return self.sigs[int(m.lastgroup[1:])]
        return None


def scan_lines(lines, signatures=SIGNATURES) -> tuple:
    """``(line count, {key: {"kind", "count", "example"}})`` for an iterable
    of log lines."""
    matcher = _Matcher(signatures)
    found, n = {}, 0
    for line in lines:
        n += 1
        sig = matcher.match(line)
        if sig is None:
            continue
        hit = found.get(sig.key)
        if hit is None:
            found[sig.key] = {"kind": sig.kind, "count": 1,
                              "example": line.strip()[:_EXAMPLE_CHARS]}
        else:
            hit["count"] += 1
    return n, found


# ---------------------------------------------------------------------------
# reading docker logs (read-only)
# ---------------------------------------------------------------------------

def docker_log_lines(name, since, until):
    """Yield a container's log lines (stdout and stderr) between two unix
    times, streamed so a noisy level never sits in memory whole. Raises
    RuntimeError when docker fails (no such container, daemon down)."""
    try:
        proc = subprocess.Popen(
            ["docker", "logs", "--since", str(int(since)),
             "--until", str(int(until) + 1), name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise RuntimeError("docker CLI not found on PATH") from exc
    first = []
    for line in proc.stdout:
        if len(first) < 3:
            first.append(line)
        yield line
    if proc.wait() != 0:
        raise RuntimeError(f"docker logs {name} failed: "
                           f"{''.join(first).strip()[:200]}")


def scan_level(spec, since, until, docker=None, read=docker_log_lines) -> dict:
    """Scan every spec component's containers over ``[since, until]`` (unix
    seconds). Returns the record saved as ``log-signals.json``."""
    from analyzer.chaos.dockercli import Docker, DockerError
    from analyzer.chaos.injector import component_to_service, containers_of

    docker = docker or Docker()
    sigs = signatures_for(spec)
    record = {"since": int(since), "until": int(until), "components": {},
              "errors": {}}
    for comp, svc in component_to_service(spec).items():
        try:
            names = containers_of(spec, docker, svc)
        except DockerError as exc:
            record["errors"][comp] = str(exc)
            continue
        if not names:
            record["errors"][comp] = "no containers found"
            continue
        total, signals = 0, {}
        try:
            for name in names:
                n, found = scan_lines(read(name, since, until), sigs)
                total += n
                for key, hit in found.items():
                    if key in signals:
                        signals[key]["count"] += hit["count"]
                    else:
                        signals[key] = hit
        except RuntimeError as exc:
            record["errors"][comp] = str(exc)
            continue
        if total == 0:
            # silence is not "no errors": the containers may have been
            # created after this window, or their logs rotated away
            record["errors"][comp] = ("no log lines in this window (a quiet "
                                      "container, or one created after it or "
                                      "with its logs rotated)")
            continue
        record["components"][comp] = {"containers": names, "lines": total,
                                      "signals": signals}
    return record


def save(record, level_dir) -> str:
    path = os.path.join(level_dir, _FILE)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=1)
    return path


def load(level_dir):
    """A level's saved scan, or None."""
    path = os.path.join(level_dir, _FILE)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def one_line(record) -> str:
    """Console summary of one level's scan."""
    hits = sorted(((h["count"], comp, key)
                   for comp, c in record["components"].items()
                   for key, h in c["signals"].items()), reverse=True)
    if not hits:
        text = "no known error signatures"
    else:
        text = ", ".join(f"{n} × {key} in {comp}" for n, comp, key in hits[:3])
    if record.get("errors"):
        text += f" (not read: {', '.join(record['errors'])})"
    return text


# ---------------------------------------------------------------------------
# across a sweep
# ---------------------------------------------------------------------------

@dataclass
class LogSignal:
    component: str
    key: str
    kind: str
    counts: dict                  # vus -> lines, every scanned level
    example: str

    @property
    def cause(self) -> bool:
        return KINDS[self.kind][1]

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def first_vus(self) -> int | None:
        return min((v for v, n in self.counts.items() if n), default=None)


@dataclass
class LogFindings:
    scanned: list                 # VU levels with a scan
    signals: list                 # LogSignal, most lines first
    suspect: LogSignal | None     # the cause the logs point to
    unread: dict = field(default_factory=dict)   # component -> why not read

    @property
    def clean_below(self) -> list:
        """Scanned levels lighter than the suspect's first, all without it."""
        if self.suspect is None:
            return []
        return [v for v in self.scanned if v < self.suspect.first_vus]


def findings(levels) -> LogFindings | None:
    """What the logs say across a sweep's LevelResults (their ``logs``
    records); None when no level had any component's logs read."""
    scanned = sorted(lv.vus for lv in levels
                     if getattr(lv, "logs", None)
                     and lv.logs.get("components"))
    if not scanned:
        return None
    by_key, unread = {}, {}
    for lv in levels:
        rec = getattr(lv, "logs", None)
        if not rec:
            continue
        unread.update(rec.get("errors", {}))
        for comp, c in rec.get("components", {}).items():
            for key, h in c.get("signals", {}).items():
                if h["kind"] not in KINDS:
                    continue
                s = by_key.setdefault((comp, key), LogSignal(
                    comp, key, h["kind"], {v: 0 for v in scanned}, h["example"]))
                s.counts[lv.vus] = h["count"]
    signals = sorted(by_key.values(), key=lambda s: (-s.total, s.component))
    causes = [s for s in signals if s.cause]
    return LogFindings(scanned, signals, causes[0] if causes else None, unread)


def _counts_text(s: LogSignal) -> str:
    return ", ".join(f"{n} at {v}" for v, n in sorted(s.counts.items()) if n)


def findings_report(f: LogFindings, top: int = 6) -> str:
    """Technical section for the scaling report."""
    lines = ["", "=" * 74,
             "What the logs say -- known error messages per level (docker logs)",
             "=" * 74,
             f"  levels scanned: {', '.join(map(str, f.scanned))} VUs"]
    if not f.signals:
        lines.append("  No known pool, worker, memory, timeout or connection "
                     "error in any level's logs.")
    for s in f.signals[:top]:
        role = "cause" if s.cause else "symptom"
        lines.append(f"  {s.component:<18} {s.key:<20} {role:<8} "
                     f"{_counts_text(s)}")
    if len(f.signals) > top:
        lines.append(f"  (+{len(f.signals) - top} more in load-<N>/{_FILE})")
    if f.suspect is not None:
        s = f.suspect
        clean = f.clean_below
        lines += ["", f"  Suspect: {KINDS[s.kind][0]}", f"    {s.total} lines "
                  f"of {s.key} in {s.component}'s logs, first at {s.first_vus} VUs"
                  + (f" (none at {', '.join(map(str, clean))})" if clean else "")
                  + ".",
                  f"    e.g. {s.example[:120]}"]
    elif f.signals:
        lines += ["", "  Only symptoms (timeouts, refused connections): what "
                  "these services wait on is",
                  "  stuck, and its logs or metrics are where the cause is."]
    if f.unread:
        for comp, why in f.unread.items():
            lines.append(f"  Not read: {comp} -- {why}")
    return "\n".join(lines) + "\n"


def findings_explain(f: LogFindings) -> str:
    """Plain-language section for the scaling summary."""
    out = ["WHAT THE LOGS SAY", "-" * 70]
    s = f.suspect
    if s is not None:
        clean = f.clean_below
        out.append(textwrap.fill(
            f"The likeliest cause: {KINDS[s.kind][0]}. {s.component}'s logs "
            f"show it {s.total} times, starting at {s.first_vus} users"
            + (" (never in the lighter tests)." if clean else "."),
            72, initial_indent="  ", subsequent_indent="  "))
    elif f.signals:
        out.append("  The logs show timeouts or refused connections, but not "
                   "what ran out:")
        out.append("  those are what a part logs when something it waits on is "
                   "stuck.")
    else:
        out.append("  The logs show none of the usual \"ran out\" errors "
                   "(connection pools,")
        out.append("  workers, memory). If something levelled off, look for "
                   "its own error messages.")
    if f.unread and s is None:
        out.append(textwrap.fill(
            "But these parts had no logs to read for some or all tests: "
            f"{', '.join(f.unread)}; the cause may be there.",
            72, initial_indent="  ", subsequent_indent="  "))
    out.append("")
    return "\n".join(out)


def level_explain(record) -> list:
    """Plain-language lines for one level's report-explained.txt."""
    if not record:
        return []
    hits = sorted(((h["count"], comp, h["kind"])
                   for comp, c in record.get("components", {}).items()
                   for h in c.get("signals", {}).values()
                   if h.get("kind") in KINDS), reverse=True)
    causes = [h for h in hits if KINDS[h[2]][1]]
    if causes:
        n, comp, kind = causes[0]
        return [f"  The logs at this load: {KINDS[kind][0]}",
                f"  ({comp}'s logs say so {n} times)."]
    if hits:
        n, comp, kind = hits[0]
        return [f"  The logs at this load: {KINDS[kind][0]} ({comp}, {n} "
                "times)."]
    return ["  The logs at this load show none of the usual \"ran out\" "
            "errors."]


def level_report(record) -> str:
    """Block for one level's report.txt."""
    if not record:
        return ""
    lines = ["", "=" * 74, "Known error messages in the logs at this level",
             "=" * 74]
    hits = sorted(((h["count"], comp, key, h["kind"])
                   for comp, c in record.get("components", {}).items()
                   for key, h in c.get("signals", {}).items()), reverse=True)
    if not hits:
        lines.append("  none")
    for n, comp, key, kind in hits:
        role = "cause" if KINDS.get(kind, ("", False))[1] else "symptom"
        lines.append(f"  {comp:<18} {key:<20} {role:<8} {n} lines")
    if record.get("errors"):
        lines.append(f"  not read: {', '.join(record['errors'])}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI: scan a finished sweep from the containers' saved logs
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    from datetime import datetime

    from analyzer import utf8_stdio
    from analyzer.spec import load_system
    from analyzer.validity import _run_dirs, parse_k6_summary

    utf8_stdio()
    p = argparse.ArgumentParser(
        prog="python3 -m analyzer.logscan",
        description="Scan each level of a finished sweep for known error "
                    "messages in the containers' logs (docker logs, "
                    "read-only) and save load-<N>/log-signals.json. Each "
                    "level's window comes from its k6-output.txt. The "
                    "containers must still exist. Re-run the analysis with "
                    "--no-measure afterwards to use the result.")
    p.add_argument("spec")
    p.add_argument("--out", default="analysis", help="the sweep's directory")
    args = p.parse_args(argv)
    spec = load_system(args.spec)
    runs = [(label, d) for label, d, vus in _run_dirs(args.out) if vus]
    if not runs:
        print(f"error: no load-<N>/k6-output.txt under {args.out}",
              file=sys.stderr)
        return 2
    for label, d in runs:
        path = os.path.join(d, "k6-output.txt")
        with open(path, encoding="utf-8") as fh:
            k6 = parse_k6_summary(fh.read())
        # k6-output.txt is written the moment k6 exits (see validity.py)
        until = os.path.getmtime(path)
        since = until - (k6.seconds or 60)
        record = scan_level(spec, since, until)
        save(record, d)
        print(f"[logs] {label} ({datetime.fromtimestamp(since):%H:%M:%S}"
              f"-{datetime.fromtimestamp(until):%H:%M:%S}): {one_line(record)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
