"""Tests for analyzer.stop_signal: the setup app's Stop through standard input.

Runs with pytest or as a bare script.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import time

from analyzer.collect.loadgen import container_name, container_prefix
from analyzer.stop_signal import STOP_VARIABLE, watch_stdin


def _watch(text):
    calls = []
    thread = watch_stdin({STOP_VARIABLE: "1"}, io.StringIO(text),
                         interrupt=lambda: calls.append("interrupt"),
                         remove_k6=lambda: calls.append("remove k6"))
    thread.join(5)
    return calls


def test_nothing_is_watched_unless_the_app_asks():
    assert watch_stdin({}, io.StringIO("stop\n")) is None
    assert watch_stdin({STOP_VARIABLE: "0"}, io.StringIO("stop\n")) is None


def test_stop_removes_k6_first_then_interrupts():
    assert _watch("hello\nstop\nlater\n") == ["remove k6", "interrupt"]


def test_the_app_going_away_stops_the_run_too():
    # standard input ends: the app that started the run is gone
    assert _watch("") == ["remove k6", "interrupt"]


def test_the_k6_prefix_names_every_container_of_the_run():
    env = {"MICROSERVICE_SCRIPTS_RUN_ID": "abc123"}
    assert container_prefix(env) == "microservice-scripts-k6-abc123-"
    assert container_name(env).startswith("microservice-scripts-k6-abc123-")


def test_stop_interrupts_a_sleeping_python_like_ctrl_c():
    # end to end, as the setup app does it: the main thread is asleep (waiting
    # for a level to end) and "stop" arrives on standard input
    child = (
        "import time\n"
        "from analyzer.stop_signal import watch_stdin\n"
        "watch_stdin(remove_k6=lambda: None)\n"
        "print('ready', flush=True)\n"
        "try:\n"
        "    time.sleep(60)\n"
        "    print('slept')\n"
        "except KeyboardInterrupt:\n"
        "    print('interrupted')\n"
    )
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env = dict(os.environ, **{STOP_VARIABLE: "1"})
    started = time.monotonic()
    proc = subprocess.Popen([sys.executable, "-c", child], cwd=root, env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        proc.stdin.write("stop\n")
        proc.stdin.flush()
        out, _ = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert out.strip() == "interrupted"
    assert time.monotonic() - started < 20


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
    sys.exit(_run_all())
