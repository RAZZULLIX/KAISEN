# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""KAISEN — a self-improving code-generation framework.

The framework evolves candidate programs through a per-project pipeline of
ordered harness programs: build -> verify* -> score*.  Harness outputs are
parsed into a unified set of weighted metrics; the champion is the candidate
with the best composite fitness.  Projects are declarative specs (project.json)
that define the pipeline order, the harness programs, the metric schema and
the guardrail policy.
"""

__version__ = "0.1.2-alpha"
