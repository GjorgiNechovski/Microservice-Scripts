"""Tests for optional k6 load generation (analyzer.collect.loadgen).

Runs two ways:
  * with pytest:            python3 -m pytest analyzer/tests/ -q
  * with a bare interpreter: python3 -m analyzer.tests.test_loadgen

No Docker/network: only config parsing and command construction are tested.
"""

from __future__ import annotations

import os
import tempfile

from analyzer.collect.loadgen import (
    build_command,
    load_config,
    resolve_script,
    sweep_levels,
    _stage_flags,
)


def test_load_config_absent_returns_none():
    assert load_config({"services": ["a"]}) is None
    assert load_config({"services": ["a"], "load": {}}) is None


def test_load_config_missing_script_raises():
    try:
        load_config({"load": {"vus": 10}})
    except ValueError as exc:
        assert "script" in str(exc)
    else:
        raise AssertionError("expected ValueError for [load] without script")


def test_load_config_present():
    cfg = load_config({"load": {"script": "perf/s.js"}})
    assert cfg["script"] == "perf/s.js"


def test_stage_flags_none_when_no_overrides():
    assert _stage_flags({"script": "s.js"}) == []


def test_stage_flags_from_vus_duration_is_constant_hold():
    # vus+duration -> jump to vus then hold, expressed only as --stage
    flags = _stage_flags({"script": "s.js", "vus": 50, "duration": "3m"})
    assert flags == ["--stage", "0s:50", "--stage", "3m:50"]
    assert "--duration" not in flags  # avoids the stages/duration conflict


def test_stage_flags_default_duration():
    flags = _stage_flags({"script": "s.js", "vus": 25})
    assert flags == ["--stage", "0s:25", "--stage", "5m:25"]


def test_stage_flags_explicit_stages():
    flags = _stage_flags({"stages": [
        {"duration": "1m", "target": 100},
        {"duration": "2m", "target": 200},
    ]})
    assert flags == ["--stage", "1m:100", "--stage", "2m:200"]


def test_build_command_structure():
    cfg = {"script": "perf/script.js"}
    cmd = build_command(cfg, project_dir="/repo", image="grafana/k6:latest")
    assert cmd[0] == "docker" and cmd[1] == "run" and "--rm" in cmd
    # host networking so the script can reach localhost:<port>
    assert cmd[cmd.index("--network") + 1] == "host"
    # project dir mounted, k6 invoked on the script (abspath differs per OS:
    # /repo on POSIX, C:\repo on Windows -- assert the OS's own form)
    import os as _os
    assert f"{_os.path.abspath('/repo')}:/work" in cmd
    assert cmd[-2:] == ["run", "perf/script.js"]



# ---------------------------------------------------------------------------
# The script path belongs to the spec, not to the shell's working directory.
# The analyzer and the system it measures live in different repositories, so
# a spec has to be able to name its own load script portably.
# ---------------------------------------------------------------------------

def _spec_with_script(tmp, rel):
    """Write a spec dir containing a load script at ``rel``; return spec path."""
    spec = os.path.join(tmp, "systems", "app.toml")
    script = os.path.join(os.path.dirname(spec), rel)
    os.makedirs(os.path.dirname(script), exist_ok=True)
    with open(script, "w", encoding="utf-8") as fh:
        fh.write("export default function () {}\n")
    return spec, os.path.normpath(script)


def test_resolve_script_is_relative_to_the_spec_not_the_cwd():
    spec = os.path.join(os.sep, "elsewhere", "systems", "app.toml")
    got = resolve_script("app/load.js", spec)
    assert got == os.path.normpath(os.path.join(os.sep, "elsewhere", "systems",
                                                "app", "load.js"))


def test_resolve_script_keeps_absolute_paths():
    absolute = os.path.join(os.sep, "opt", "load.js")
    assert resolve_script(absolute, "/any/spec.toml") == absolute


def test_load_config_resolves_against_spec_from_any_cwd():
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as away:
        spec, script = _spec_with_script(tmp, "app/load.js")
        here = os.getcwd()
        os.chdir(away)                       # run from somewhere unrelated
        try:
            cfg = load_config({"load": {"script": "app/load.js"}}, spec)
        finally:
            os.chdir(here)
        assert cfg["script"] == script


def test_load_config_does_not_mutate_the_spec():
    with tempfile.TemporaryDirectory() as tmp:
        spec, _ = _spec_with_script(tmp, "load.js")
        raw = {"load": {"script": "load.js"}}
        load_config(raw, spec)
        assert raw["load"]["script"] == "load.js"


def test_load_config_missing_script_file_is_a_hard_error():
    with tempfile.TemporaryDirectory() as tmp:
        spec = os.path.join(tmp, "app.toml")
        try:
            load_config({"load": {"script": "nope.js"}}, spec)
        except FileNotFoundError as exc:
            assert "nope.js" in str(exc)
        else:
            raise AssertionError("a missing load script must not be ignored")


def test_build_command_mounts_the_scripts_own_directory():
    with tempfile.TemporaryDirectory() as tmp:
        spec, script = _spec_with_script(tmp, "app/load.js")
        cfg = load_config({"load": {"script": "app/load.js"}}, spec)
        cmd = build_command(cfg, project_dir="/unrelated/cwd")
        assert f"{os.path.dirname(script)}:/work" in cmd
        assert cmd[-1] == "load.js"          # run by name inside the mount
        assert not any("/unrelated/cwd" in c for c in cmd)

def test_sweep_levels_list_triggers_sweep():
    assert sweep_levels({"script": "s.js", "vus": [50, 100, 200]}) == [50, 100, 200]


def test_sweep_levels_scalar_or_absent_is_single_run():
    assert sweep_levels({"script": "s.js", "vus": 50}) is None
    assert sweep_levels({"script": "s.js"}) is None
    assert sweep_levels({"script": "s.js", "vus": []}) is None
    assert sweep_levels(None) is None


def test_stage_flags_list_vus_defers_to_sweep():
    # a list vus is driven per-level by the sweep, not by a single --stage
    assert _stage_flags({"script": "s.js", "vus": [50, 100]}) == []


def test_load_dotenv_reads_file_but_shell_wins(tmp_path=None):
    import os
    import tempfile
    from analyzer.collect.loadgen import load_dotenv

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, ".env")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# comment\n\nGC_TOKEN=from-file\nGC_EMPTY=\n"
                     "malformed line\nGC_OTHER = spaced \n")
        env = {"GC_TOKEN": "from-shell"}
        load_dotenv(path, environ=env)
        assert env["GC_TOKEN"] == "from-shell"     # shell value wins
        assert env["GC_OTHER"] == "spaced"         # trimmed
        assert "GC_EMPTY" not in env               # empty values skipped
        assert "malformed" not in env


def test_load_dotenv_missing_file_is_noop():
    from analyzer.collect.loadgen import load_dotenv
    env = {}
    load_dotenv("definitely/not/a/real/.env", environ=env)
    assert env == {}


def test_env_flags_spec_and_passthrough():
    from analyzer.collect.loadgen import _env_flags
    cfg = {"script": "s.js",
           "env": {"GC_BASE_URL": "http://gw:8080"},
           "env_passthrough": ["GC_TOKEN", "GC_MISSING"]}
    flags = _env_flags(cfg, environ={"GC_TOKEN": "jwt123"})
    assert flags == ["-e", "GC_BASE_URL=http://gw:8080", "-e", "GC_TOKEN=jwt123"]
    # unset host vars are simply omitted, never passed empty
    assert not any("GC_MISSING" in f for f in flags)


def test_env_flags_host_wins_over_spec():
    from analyzer.collect.loadgen import _env_flags
    cfg = {"script": "s.js", "env": {"GC_TOKEN": "stale"},
           "env_passthrough": ["GC_TOKEN"]}
    flags = _env_flags(cfg, environ={"GC_TOKEN": "fresh"})
    # docker uses the LAST -e occurrence -> the host value wins
    assert flags[-1] == "GC_TOKEN=fresh"


def test_build_command_env_flags_before_image():
    cfg = {"script": "s.js", "env": {"A": "1"}}
    cmd = build_command(cfg, project_dir="/x", image="grafana/k6:latest")
    assert cmd[cmd.index("-e") + 1] == "A=1"
    assert cmd.index("-e") < cmd.index("grafana/k6:latest")


def test_build_command_custom_network_and_overrides():
    cfg = {"script": "s.js", "docker_network": "backend", "vus": 10,
           "duration": "1m"}
    cmd = build_command(cfg, project_dir="/x")
    assert cmd[cmd.index("--network") + 1] == "backend"
    # override stages appear before the script argument
    assert "--stage" in cmd and cmd[-1] == "s.js"


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
