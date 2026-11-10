"""Tests for naming a hidden limit from the containers' logs (analyzer.logscan).

No Docker needed: the container lookup and the log reader are injected.

  python3 -m pytest analyzer/tests/ -q
  python3 -m analyzer.tests.test_logscan
"""

from __future__ import annotations

import tempfile

from analyzer import logscan
from analyzer.chaos.dockercli import Docker

_WAIT_QUEUE = ("fail: Api[0] MongoDB.Driver.MongoWaitQueueFullException: The wait "
               "queue for acquiring a connection to server mongo:27017 is full.")
_CANCELLED = "System.Threading.Tasks.TaskCanceledException: A task was canceled."


def test_a_line_counts_once_and_causes_beat_symptoms():
    lines = [_WAIT_QUEUE,
             _CANCELLED + " inner: " + _WAIT_QUEUE,    # both: counts as the cause
             _CANCELLED,
             "   at MongoDB.Driver.Core.Connections...",   # a stack frame: nothing
             "info: request finished in 3 ms"]
    n, found = logscan.scan_lines(lines)
    assert n == 5
    assert found["mongo-wait-queue"]["count"] == 2
    assert found["mongo-wait-queue"]["kind"] == "db-pool"
    assert found["timeout"]["count"] == 1
    assert found["mongo-wait-queue"]["example"].startswith("fail: Api[0]")


def test_known_messages_from_other_stacks():
    cases = {
        "HikariPool-1 - Connection is not available, request timed out after "
        "30000ms.": "hikari-pool",
        "FATAL:  sorry, too many clients already": "postgres-too-many",
        "StackExchange.Redis.RedisTimeoutException: Timeout performing GET (5000ms)":
            "redis-timeout",
        "Error: EMFILE: too many open files, open '/x'": "open-files",
        "[CRITICAL] WORKER TIMEOUT (pid:42)": "gunicorn-timeout",
        "connect ECONNREFUSED 10.0.0.3:5432": "refused",
    }
    for line, key in cases.items():
        _, found = logscan.scan_lines([line])
        assert list(found) == [key], (line, found)


def test_spec_can_add_signatures_and_rejects_unknown_kinds():
    spec = {"logs": {"extra": [{"key": "my-pool", "kind": "db-pool",
                                "pattern": r"BoltPool: no free slot"}]}}
    sigs = logscan.signatures_for(spec)
    _, found = logscan.scan_lines(["BoltPool: no free slot (32 busy)"], sigs)
    assert list(found) == ["my-pool"]
    try:
        logscan.signatures_for({"logs": {"extra": [
            {"key": "x", "kind": "nope", "pattern": "x"}]}})
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown kind must be refused")
    assert logscan.enabled({}) and not logscan.enabled({"logs": {"scan": False}})


def _docker(containers):
    """A Docker whose ``ps`` answers from ``containers`` (service -> names)."""
    def run(args):
        svc = next(a.split("=", 2)[2] for a in args
                   if a.startswith("label=com.docker.compose.service="))
        if any("Label" in a for a in args):           # projects_for
            return "proj\n" if containers.get(svc) else ""
        return "".join(f"{n}\n" for n in containers.get(svc, []))
    return Docker(runner=run)


def test_scan_level_reads_every_component_and_records_what_it_could_not():
    spec = {"services": ["api", "gw"], "infrastructure": ["db", "cache"]}
    logs = {"api-1": [_WAIT_QUEUE] * 3, "api-2": [_WAIT_QUEUE, "ok"],
            "gw-1": [_CANCELLED], "cache-1": []}

    def read(name, since, until):
        assert (since, until) == (100, 220)
        if name not in logs:
            raise RuntimeError(f"docker logs {name} failed: no such container")
        return iter(logs[name])

    rec = logscan.scan_level(spec, 100, 220,
                             docker=_docker({"api": ["api-1", "api-2"],
                                             "gw": ["gw-1"], "db": ["db-1"],
                                             "cache": ["cache-1"]}),
                             read=read)
    # a container with no lines in the window was not read, not "clean"
    assert "cache" not in rec["components"]
    assert "no log lines in this window" in rec["errors"]["cache"]
    api = rec["components"]["api"]
    assert api["containers"] == ["api-1", "api-2"] and api["lines"] == 5
    assert api["signals"]["mongo-wait-queue"]["count"] == 4
    assert rec["components"]["gw"]["signals"]["timeout"]["count"] == 1
    assert "db" in rec["errors"] and "no such container" in rec["errors"]["db"]
    d = tempfile.mkdtemp()
    logscan.save(rec, d)
    assert logscan.load(d) == rec
    assert logscan.load(tempfile.mkdtemp()) is None
    assert "4 × mongo-wait-queue in api" in logscan.one_line(rec)


def _record(api_hits, gw_timeouts):
    sig = {}
    if api_hits:
        sig["mongo-wait-queue"] = {"kind": "db-pool", "count": api_hits,
                                   "example": _WAIT_QUEUE}
    gw = ({"timeout": {"kind": "timeout", "count": gw_timeouts,
                       "example": _CANCELLED}} if gw_timeouts else {})
    return {"since": 0, "until": 1, "errors": {},
            "components": {"api": {"containers": ["a"], "lines": 9,
                                   "signals": sig},
                           "gw": {"containers": ["g"], "lines": 9,
                                  "signals": gw}}}


class _Lv:
    def __init__(self, vus, logs):
        self.vus, self.logs = vus, logs


def test_findings_name_the_cause_that_starts_with_the_failures():
    levels = [_Lv(200, _record(0, 0)), _Lv(800, _record(40, 900)),
              _Lv(1600, _record(5000, 30000)), _Lv(2000, None)]
    f = logscan.findings(levels)
    assert f.scanned == [200, 800, 1600]
    # the symptom has more lines, but the cause is the suspect
    assert f.signals[0].key == "timeout" and f.suspect.key == "mongo-wait-queue"
    assert f.suspect.first_vus == 800 and f.clean_below == [200]
    tech = logscan.findings_report(f)
    assert "Suspect: the database connection pool" in tech
    assert "5040 lines of mongo-wait-queue in api's logs, first at 800 VUs" in tech
    assert "(none at 200)" in tech
    plain = logscan.findings_explain(f)
    flat = " ".join(plain.split())
    assert "likeliest cause" in flat and "starting at 800 users" in flat
    assert logscan.findings([_Lv(200, None)]) is None
    nothing_read = {"since": 0, "until": 1, "components": {},
                    "errors": {"api": "no log lines in this window"}}
    assert logscan.findings([_Lv(200, nothing_read)]) is None
    partly = _record(0, 0)
    del partly["components"]["api"]
    partly["errors"]["api"] = "no log lines in this window"
    assert "no logs to read for some or all tests: api" in " ".join(
        logscan.findings_explain(logscan.findings([_Lv(800, partly)])).split())
    only_symptoms = logscan.findings([_Lv(800, _record(0, 50))])
    assert only_symptoms.suspect is None
    assert "Only symptoms" in logscan.findings_report(only_symptoms)


def test_the_hidden_limit_diagnosis_names_what_the_logs_say():
    from analyzer.diagnose import diagnose, diagnosis_explain, diagnosis_report
    from analyzer.tests.test_diagnose import _TARGET, _plateau_levels
    spec, levels = _plateau_levels()
    hits = {200: 0, 800: 120, 1200: 900, 1600: 4000, 2000: 20000}
    for lv in levels:
        lv.logs = _record(hits[lv.vus], 0)
    dr = diagnose(levels, spec, _TARGET)
    tech = diagnosis_report(dr, spec)
    assert "HIDDEN LIMIT — not CPU" in tech
    assert "The logs name it: the database connection pool" in tech
    assert "first at 800 VUs (none at lighter levels)" in tech
    assert "usually\n  name it" not in tech
    plain = diagnosis_explain(dr, spec)
    assert "Here the logs say which: the database connection pool" in         " ".join(plain.split())
    assert "(api, from 800 users on)" in " ".join(plain.split())
    assert "ran out (api, from 800" in plain.replace("\n    ", " ")
    from analyzer.scaling import scaling_explain, scaling_report
    assert "What the logs say" in scaling_report(spec, levels, _TARGET)
    assert "WHAT THE LOGS SAY" in scaling_explain(spec, levels, _TARGET)
    # without any scan the diagnosis says where to look, as before
    for lv in levels:
        lv.logs = None
    assert "analyzer.logscan" in diagnosis_report(diagnose(levels, spec, _TARGET),
                                                  spec)


def test_level_report_lists_this_levels_messages():
    text = logscan.level_report(_record(40, 900))
    assert "gw" in text and "timeout" in text and "symptom" in text
    assert "mongo-wait-queue" in text and "cause" in text
    assert logscan.level_report(None) == ""
    plain = "\n".join(logscan.level_explain(_record(40, 900)))
    # the cause, not the louder symptom
    assert "connection pool" in plain and "api's logs say so 40 times" in plain
    assert "timed out" in "\n".join(logscan.level_explain(_record(0, 900)))
    assert logscan.level_explain(None) == []


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
