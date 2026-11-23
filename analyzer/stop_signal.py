"""Stop the pipeline cleanly from whoever started it, where Ctrl+C can't reach.

The setup app starts ``python -m analyzer`` with ``MICROSERVICE_SCRIPTS_STOP_ON_STDIN=1``
and keeps the process's standard input open. A line ``stop`` on it, or its
end (the app went away without stopping the run), stops the pipeline the way
Ctrl+C does:

1. this run's k6 container is removed at once, so a load level that holds the
   main thread in ``docker run`` ends now rather than at the end of the level;
2. SIGINT is raised, so the clean-up on the way out runs exactly as for Ctrl+C
   (a container fault injection stopped is started again, the k6 container is
   removed; ``loadgen.run_load``, ``injector.measure_mttr``).

On Windows a program can't send Ctrl+C to a process it started without a
console of its own, so before this the setup app could only kill the analyzer,
and none of that clean-up ran. Run by hand, without the variable, nothing here
happens.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading

#: Set by the setup app; anything other than "1" leaves standard input alone.
STOP_VARIABLE = "MICROSERVICE_SCRIPTS_STOP_ON_STDIN"


def _interrupt() -> None:
    """Ctrl+C for this process, from any thread."""
    if os.name == "nt":
        # runs Python's C-level handler, which also wakes a sleeping main thread
        signal.raise_signal(signal.SIGINT)
    else:
        os.kill(os.getpid(), signal.SIGINT)


def _remove_k6(environ=None) -> None:
    """Remove this run's k6 containers (the ones loadgen named for it)."""
    from analyzer.collect.loadgen import container_prefix

    prefix = container_prefix(environ)
    try:
        found = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"name={prefix}"],
            capture_output=True, text=True, timeout=20, check=False)
        for cid in found.stdout.split():
            subprocess.run(["docker", "rm", "-f", cid], capture_output=True,
                           timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        pass  # no docker here: nothing of ours to remove


def watch_stdin(environ=None, stdin=None, interrupt=None, remove_k6=None):
    """Start watching standard input for ``stop`` when the setup app asked for it.

    Returns the watching thread, or None when ``STOP_VARIABLE`` isn't "1".
    ``stdin``, ``interrupt`` and ``remove_k6`` are for tests.
    """
    environ = os.environ if environ is None else environ
    if environ.get(STOP_VARIABLE) != "1":
        return None
    stream = sys.stdin if stdin is None else stdin
    interrupt = _interrupt if interrupt is None else interrupt
    remove_k6 = (lambda: _remove_k6(environ)) if remove_k6 is None else remove_k6
    if stream is None:
        return None

    def watch() -> None:
        try:
            for line in stream:
                if line.strip().lower() == "stop":
                    break
        except (OSError, ValueError):
            pass  # closed under us: the app is gone, so stop as well
        remove_k6()
        interrupt()

    thread = threading.Thread(target=watch, name="stop-on-stdin", daemon=True)
    thread.start()
    return thread
