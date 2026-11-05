---
name: solver-improvement
description: Improve a search, construction, or optimization algorithm across multiple instances. Use when evolving solver code, exploring representations or operators, or deciding which promising solver branch to extend.
---

# Improve a solver

Separate the method from the objects it produces. Record solver version,
parameters, starting artifact, instance, seed, and execution environment for
each run. Keep valid objects even when their producing algorithm is superseded.

Inspect failures and bottlenecks before changing the search. Consider a different
representation, neighborhood, initialization, restart schedule, decomposition,
or constraint encoding when parameter tuning has stalled. Keep several
structurally different approaches; repeated variants of one incumbent are not
independent exploration.

First test a change on tiny instances with known checks. Then compare on a
fixed collection containing easy, difficult, and structurally different cases.
Report both quality and failures. Do not call a restricted model's optimum a
global optimum of the original problem.

Checkpoint complete candidate artifacts through the tools the runtime exposes.
Treat an interrupted run's partial output as unverified until a checker accepts
it. Preserve valid completed checkpoints when a later search step fails.

Return the solver artifact, candidate object references, measured differences,
failure cases, and a proposed next branch. When deciding whether a reusable
strategy should replace another, load `strategy-evolution` as well.
