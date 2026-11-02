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
