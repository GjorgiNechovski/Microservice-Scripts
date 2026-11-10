"""Generic microservice performance & reliability analyzer.

This package is the *engine* of the master's-thesis toolkit: it is deliberately
system-agnostic. It knows nothing about blogs, Kong, or any specific language.
A concrete system is described by a separate spec (see systems/, e.g. the FINKI
blogging platform in systems/finki-blogger.toml) and pointed at by the engine.

Layers (thesis chapters):
    Layer 1  models.queueing    -- M/M/c performance
    Layer 2  models.ctmc        -- availability / MTTF / MTTR
    Layer 3  models.rbd         -- reliability block diagram + fault tree
    Layer 4  models.sensitivity -- parameter sweeps
"""

__version__ = "0.1.0"


def utf8_stdio() -> None:
    """Make stdout/stderr UTF-8 for the command-line entry points.

    The reports use ρ, μ and λ. On Windows, output redirected to a file or a
    pipe is encoded with the ANSI code page (cp1252), which has none of them,
    so a sweep whose log was redirected (as CLAUDE.md advises) crashed with
    UnicodeEncodeError while printing its final report. Called by every
    ``main()``; a no-op where the streams are already UTF-8 or cannot be
    reconfigured (e.g. captured by a test runner).
    """
    import sys

    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        reconfigure = getattr(stream, "reconfigure", None)
        if encoding != "utf8" and reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
