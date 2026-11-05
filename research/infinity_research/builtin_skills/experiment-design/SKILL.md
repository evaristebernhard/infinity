---
name: experiment-design
description: Design controlled experiments for empirical hypotheses and compare competing programs or methods. Use before expensive training, solver benchmarking, an ablation, or a claim that a change improves performance.
---

# Design an experiment

State the hypothesis and an observation that would weaken it. Choose the unit
of comparison: an example, dataset, instance, seed, or whole task. Freeze the
metric, direction, input set, evaluator, and stopping rule before running.

Establish a working baseline. Start with a small diagnostic run to catch shape,
format, or execution errors; then use the agreed full evaluation. Compare one
explanatory change at a time when attributing cause. If changing several parts
is useful for search, describe the result as a combined intervention and leave
causal attribution for a later ablation.

Use matched instances and seeds for paired comparisons. Record failures,
timeouts, and missing results; never silently average only successful runs.
Report dispersion or repeated-run results when stochastic variation can change
the conclusion. A single best seed is a candidate, not an estimate of expected
performance.

Keep tuning data separate from reserved final evaluation when the task has a
generalization objective. For finite mathematical constructions, verify every
candidate against its constraints instead of inventing a train/test split.

Return the hypothesis, baseline and candidate versions, exact inputs, evaluation
protocol, measurements, and next decision. Let the host checker issue acceptance;
an experiment's own printed score is a claim until independently checked.
