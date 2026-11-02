"""Optional figure generation for the thesis (matplotlib).

This package is the ONLY part of the analyzer that uses third-party libraries
(matplotlib + numpy). The math engine in ``analyzer/models`` and the collectors
stay pure standard library; plotting is imported only when you actually render
figures, so the core keeps its zero-dependency guarantee.

Every figure is driven by the system spec + its measured file (and the models
they feed) -- no hardcoded, app-specific numbers -- so the same command produces
the right figures for any system.

    python3 -m analyzer.figures systems/finki-blogger.toml --out figures/
"""

from analyzer.figures.plots import generate_all

__all__ = ["generate_all"]
