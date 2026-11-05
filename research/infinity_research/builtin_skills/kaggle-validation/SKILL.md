---
name: kaggle-validation
description: Set up faithful local evaluation and diagnose leaderboard or validation mismatches for a Kaggle-style competition. Use when reading a competition contract, preparing splits, comparing entries, or validating submission artifacts.
---

# Validate competition work

Read the official task, data description, metric implementation, submission
format, and applicable rules. Pin the metric bytes and record special cases:
empty predictions, missing rows, invalid objects, ties, aggregation, and score
direction. A metric name alone is not a complete implementation contract.

Determine the task type. For predictive modeling, split by the real source of
dependence such as person, time, image source, or duplicate content. Keep labels
and any reserved final set outside candidate-accessible inputs. Fit preprocessing
only on the permitted training portion. For a finite construction competition,
verify submitted objects directly; do not impose a meaningless hidden-label split.

Run a simple baseline through the same execution, export, and evaluation path as
later candidates. Compare the local evaluator with official examples or reference
code when available. Test consequential edge cases rather than only average rows.

For each candidate, record code and data hashes, split, seed, environment,
predictions, and independent score. Use the fixed validation protocol for search;
do not turn repeated public leaderboard feedback into an unrecorded tuning set.

Validate the final exported artifact's IDs, coverage, dimensions, finite values,
and domain constraints. Keep local export separate from external submission;
only use an upload capability if the campaign actually provides it.
