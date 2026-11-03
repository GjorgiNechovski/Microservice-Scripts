"""Optional k6 load generation for Phase-1 measurement.

Arrival rate (lambda) and utilization (rho) only exist while traffic flows, so
they must be measured under load. If the spec has a ``[load]`` section pointing
at the user's **own** k6 script, this module runs it in the official
``grafana/k6`` Docker image -- no host install needed -- and reports how long it
ran, so the collector can measure over a matching window.

The k6 script is always the user's to provide: endpoints, payloads and auth are
application-specific, so a generic analyzer cannot ship a universal one.

Any overrides from the spec are expressed as k6 ``--stage`` flags (never
``--duration``), which lets them cleanly override a script that hard-codes its
own ``options.stages`` without k6's "duration and stages both set" conflict.
"""

from __future__ import annotations

import math
import os
import subprocess
import time
from dataclasses import dataclass, field

_K6_IMAGE = "grafana/k6:latest"


@dataclass
class LoadRun:
    returncode: int
    seconds: float
    command: list
    output: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def load_config(spec: dict, spec_path: str | None = None) -> dict | None:
    """Return the spec's ``[load]`` config, or ``None`` if absent.

    Raises ``ValueError`` if the section exists but omits the required
    ``script`` (there is nothing to run without it).

    With ``spec_path``, a relative ``script`` is resolved against the spec
    file's own directory, not the shell's working directory. The analyzer and
    the system it measures usually live in different repositories, so the
    spec has to be able to say where its load script is without depending on
    where the command happens to be run from. A missing script is a
    ``FileNotFoundError``: silently measuring an idle system instead would
    look like a successful run.
    """
    cfg = spec.get("load")
    if not cfg:
        return None
    if not cfg.get("script"):
        raise ValueError(
            "[load] section is present but has no 'script' -- point it at your "
            "own k6 load script, or remove [load] to drive traffic manually.")
    if spec_path is None:
        return cfg
    cfg = dict(cfg)
    cfg["script"] = resolve_script(cfg["script"], spec_path)
    if not os.path.isfile(cfg["script"]):
        raise FileNotFoundError(
            f"[load].script not found: {cfg['script']} (relative paths are "
            f"resolved against the spec's directory, "
            f"{os.path.dirname(os.path.abspath(spec_path))})")
    return cfg


def resolve_script(script: str, spec_path: str) -> str:
    """Absolute path of ``script``, read relative to the spec when relative."""
    if os.path.isabs(script):
        return script
    base = os.path.dirname(os.path.abspath(spec_path))
    return os.path.normpath(os.path.join(base, script))


def sweep_levels(cfg: dict | None) -> list | None:
    """The VU levels to sweep when ``[load].vus`` is a LIST, else ``None``.

    ``vus = [50, 100, 200, 500]`` asks the pipeline to run the load once per
    level and compare them. A scalar ``vus`` (or none) is a single run, so this
    returns ``None`` and the normal single-level flow applies.
    """
    if not cfg:
        return None
    vus = cfg.get("vus")
    if isinstance(vus, list) and vus:
        return [int(v) for v in vus]
    return None


def _stage_flags(cfg: dict) -> list:
    """Translate optional overrides into k6 ``--stage`` flags.

    * explicit ``stages``    -> used as-is
    * ``vus`` (+ ``duration``) -> jump to vus, hold for duration (constant load)
    * neither                -> no flags (the script's own options run)
    """
    stages = cfg.get("stages")
    if stages:
        flags = []
        for stage in stages:
            flags += ["--stage", f'{stage["duration"]}:{stage["target"]}']
        return flags
    vus = cfg.get("vus")
    if isinstance(vus, list):
        return []          # a sweep drives each level explicitly (sweep_levels)
    if vus:
        vus = int(vus)
        duration = cfg.get("duration", "5m")
        return ["--stage", f"0s:{vus}", "--stage", f"{duration}:{vus}"]
    return []


def load_dotenv(path: str = ".env", environ=None) -> None:
    """Load KEY=VALUE lines from a git-ignored file into the environment.

    Lets secrets (GC_TOKEN, ...) live in a file you paste into instead of
    being exported by hand every session. Existing environment variables WIN
    over file values, so a fresh ``$env:GC_TOKEN`` still overrides the file.
    Blank lines and ``#`` comments are ignored; malformed lines are skipped.
    """
    environ = os.environ if environ is None else environ
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if key and value and key not in environ:
                environ[key] = value


def _env_flags(cfg: dict, environ=None) -> list:
    """``-e`` flags for the k6 container, so the script's ``__ENV`` works.

    Two sources, both optional in ``[load]``:

    * ``env`` (a table)            -> literal values from the spec
    * ``env_passthrough`` (a list) -> forwarded from the HOST environment when
      set there. This is the place for secrets (auth tokens, passwords): they
      stay out of the tracked spec file. Host values win over ``env`` ones
      (docker uses the last ``-e`` occurrence).
    """
    environ = os.environ if environ is None else environ
    flags = []
    for key, value in (cfg.get("env") or {}).items():
        flags += ["-e", f"{key}={value}"]
    for key in cfg.get("env_passthrough", []):
        value = environ.get(key)
        if value is not None:
            flags += ["-e", f"{key}={value}"]
    return flags


def build_command(cfg: dict, project_dir: str,
                  image: str = _K6_IMAGE) -> list:
    """Build the ``docker run ... k6 run`` command for the load script.

    ``--network host`` lets the script reach the app at ``localhost:<port>``
    (Linux). On macOS/Windows set ``docker_network`` in ``[load]`` and target
    the compose network / ``host.docker.internal`` in your script instead.

    An absolute ``script`` (what :func:`load_config` produces from a spec
    path) mounts the script's own directory, so imports of sibling files keep
    working. A relative one is still taken relative to ``project_dir``.
    """
    network = cfg.get("docker_network", "host")
    script = cfg["script"]
    if os.path.isabs(script):
        mount, target = os.path.dirname(script), os.path.basename(script)
    else:
        mount, target = os.path.abspath(project_dir), script
    return [
        "docker", "run", "--rm",
        "--network", network,
        "-v", f"{mount}:/work",
        "-w", "/work",
        *_env_flags(cfg),
        image, "run",
        *_stage_flags(cfg),
        target,
    ]


def measurement_window(seconds: float) -> str:
    """PromQL range covering a load run that just finished, in whole seconds.

    Rounding up to whole minutes (as this once did) let a 2m50s level be read
    over 3m, the extra 10 s being idle time or the previous level's traffic.
    Never below a minute, so a rate still spans several scrapes.
    """
    return f"{max(60, math.ceil(seconds))}s"


def run_load(cfg: dict, project_dir: str = ".", image: str = _K6_IMAGE,
             timeout: float = 1800.0) -> LoadRun:
    """Run the k6 load script to completion; return timing + exit status."""
    command = build_command(cfg, project_dir, image)
    start = time.time()
    proc = subprocess.run(command, capture_output=True, text=True,
                          timeout=timeout)
    return LoadRun(proc.returncode, time.time() - start, command,
                   (proc.stdout or "") + (proc.stderr or ""))
