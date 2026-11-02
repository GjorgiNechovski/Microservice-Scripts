"""A tiny, dependency-free Docker client over the ``docker`` CLI.

Only what the failure-injection harness needs: find a compose service's
containers, inspect their state/health/restart history, and kill/start them.
Uses ``subprocess`` against the ``docker`` CLI (no SDK dependency); the command
runner is injectable so tests can run without Docker.

Containers are located by the standard compose label
``com.docker.compose.service`` -- no project name assumptions, so it works on
any compose project.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass


class DockerError(RuntimeError):
    """A docker CLI call failed or docker was unreachable."""


@dataclass
class ContainerState:
    name: str
    running: bool
    health: str | None      # "healthy"/"unhealthy"/"starting" or None (no check)
    restart_count: int
    restart_policy: str      # "no", "unless-stopped", "always", "on-failure"
    created: str             # ISO8601
    started: str             # ISO8601

    @property
    def auto_restarts(self) -> bool:
        return self.restart_policy not in ("", "no")

    @property
    def has_healthcheck(self) -> bool:
        return self.health is not None


class Docker:
    def __init__(self, runner=None):
        # runner(list[str]) -> stdout str; raises DockerError on failure.
        self._run = runner or self._default_run

    def _default_run(self, args: list) -> str:
        try:
            proc = subprocess.run(["docker", *args], capture_output=True,
                                  text=True, timeout=60)
        except FileNotFoundError as exc:
            raise DockerError("docker CLI not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise DockerError(f"docker {' '.join(args)} timed out") from exc
        if proc.returncode != 0:
            raise DockerError(
                f"docker {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout

    # -- queries ---------------------------------------------------------
    def exec_tcp_check(self, name: str, port: int) -> bool:
        """True if the container can reach 127.0.0.1:<port> from the inside.

        Probes from within the container's own network namespace, so it works
        on Docker Desktop (Windows/macOS) where container IPs are not routable
        from the host. Tries bash's /dev/tcp first (Debian-based images), then
        busybox ``nc`` (Alpine). False when the port is closed, no probe tool
        exists, or the container is not running.
        """
        probes = (
            ["bash", "-c", f"exec 3<>/dev/tcp/127.0.0.1/{int(port)}"],
            ["sh", "-c", f"nc -z 127.0.0.1 {int(port)}"],
        )
        for probe in probes:
            try:
                self._run(["exec", name, *probe])
                return True
            except DockerError:
                continue
        return False

    def containers_for(self, compose_service: str) -> list:
        """Container names for a compose service (running or not), sorted."""
        out = self._run([
            "ps", "-a",
            "--filter", f"label=com.docker.compose.service={compose_service}",
            "--format", "{{.Names}}",
        ])
        return sorted(n for n in out.splitlines() if n.strip())

    def container_ips(self, name: str) -> list:
        """The container's IP address(es) across its networks."""
        raw = self._run(["inspect", "--format",
                         "{{json .NetworkSettings.Networks}}", name])
        nets = json.loads(raw) or {}
        return [n.get("IPAddress") for n in nets.values()
                if isinstance(n, dict) and n.get("IPAddress")]

    def inspect(self, name: str) -> ContainerState:
        raw = self._run(["inspect", name])
        data = json.loads(raw)
        if not data:
            raise DockerError(f"no such container: {name}")
        info = data[0]
        state = info.get("State", {})
        health = state.get("Health", {}).get("Status") if state.get("Health") \
            else None
        policy = info.get("HostConfig", {}).get("RestartPolicy", {}) \
            .get("Name", "no") or "no"
        return ContainerState(
            name=name,
            running=bool(state.get("Running", False)),
            health=health,
            restart_count=int(info.get("RestartCount", 0)),
            restart_policy=policy,
            created=info.get("Created", ""),
            started=state.get("StartedAt", ""),
        )

    # -- actions ---------------------------------------------------------
    def kill(self, name: str) -> None:
        self._run(["kill", name])

    def start(self, name: str) -> None:
        self._run(["start", name])
