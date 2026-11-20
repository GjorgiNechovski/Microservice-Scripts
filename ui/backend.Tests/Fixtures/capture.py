"""Capture a compose project as a test fixture for the setup app's backend.

Saves `docker compose config --format json` with the project folder written as
{PROJECT} and secrets masked, plus every Dockerfile it builds from. Read-only:
it starts nothing.

    python3 capture.py <project-dir> <fixture-name> [compose files...]
"""

import json
import os
import re
import shutil
import subprocess
import sys

SECRET_KEY = re.compile(r"PASS|SECRET|TOKEN|KEY|CREDENTIAL|COOKIE|HEADERS", re.IGNORECASE)
INLINE_PASSWORD = re.compile(r"((?:password|pwd)\s*=)[^;]*", re.IGNORECASE)
URL_PASSWORD = re.compile(r"(://[^:/@\s]+:)[^@\s]+@")


def main(project_dir, name, *files):
    project_dir = os.path.abspath(project_dir)
    args = ["docker", "compose"] + [x for f in files for x in ("-f", f)]
    raw = subprocess.run(args + ["config", "--format", "json"], cwd=project_dir,
                         check=True, capture_output=True, text=True).stdout
    model = json.loads(raw)
    for svc in model.get("services", {}).values():
        env = svc.get("environment") or {}
        for key, value in env.items():
            if value and SECRET_KEY.search(key):
                env[key] = "***"
            elif value:
                env[key] = INLINE_PASSWORD.sub(r"\1***", URL_PASSWORD.sub(r"\1***@", value))
    text = json.dumps(model, indent=1)
    text = text.replace(project_dir, "{PROJECT}").replace(os.path.expanduser("~"), "{HOME}")

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "compose.json"), "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    for svc in model.get("services", {}).values():
        build = svc.get("build")
        if not build:
            continue
        dockerfile = os.path.join(build["context"], build.get("dockerfile", "Dockerfile"))
        if os.path.isfile(dockerfile):
            target = os.path.join(out, os.path.relpath(dockerfile, project_dir))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(dockerfile, target)
    print(f"captured {name}: {len(model.get('services', {}))} services -> {out}")


if __name__ == "__main__":
    main(*sys.argv[1:])
