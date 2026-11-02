"""Run the whole test suite without pytest:  python3 -m analyzer.tests

Each test module already has its own ``_run_all()`` (they are dual-mode, so
they also run under pytest). This calls every one of them, prints only the
failures in full, and ends with one total -- so a fresh checkout can be
checked with a single command on a machine that has nothing but Python.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import pkgutil
import re
import sys

import analyzer.tests as _pkg


def main() -> int:
    modules = sorted(m.name for m in pkgutil.iter_modules(_pkg.__path__)
                     if m.name.startswith("test_"))
    passed = total = 0
    failed_modules = []
    for name in modules:
        mod = importlib.import_module(f"analyzer.tests.{name}")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = mod._run_all()
        text = out.getvalue()
        m = re.search(r"(\d+)/(\d+) passed", text)
        ok, n = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        passed, total = passed + ok, total + n
        mark = "ok  " if rc == 0 else "FAIL"
        print(f"  {mark} {name:<22} {ok}/{n}")
        if rc != 0:
            failed_modules.append(name)
            print("\n".join("        " + line for line in text.splitlines()
                            if "FAIL" in line or "ERROR" in line))
    verdict = "ALL GREEN" if not failed_modules else (
        f"{len(failed_modules)} module(s) failing: {', '.join(failed_modules)}")
    print(f"\n{passed}/{total} passed across {len(modules)} modules -- {verdict}")
    return 1 if failed_modules else 0


if __name__ == "__main__":
    sys.exit(main())
