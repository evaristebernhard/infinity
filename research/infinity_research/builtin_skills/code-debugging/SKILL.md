---
name: code-debugging
description: Diagnose and repair failing candidate programs or tool integrations from reproducible evidence. Use after an exception, timeout, invalid output, inconsistent result, or a regression in an otherwise promising research method.
---

# Debug a candidate

Preserve the failing source, exact input, command/tool parameters, environment,
and diagnostic output. Classify the failure: interface mismatch, missing input,
resource exhaustion, algorithmic error, numerical instability, or checker error.
Do not infer that the research hypothesis is false merely because its code failed.

Reproduce the issue with the smallest faithful input. Inspect the first relevant
failure rather than reacting to the last cascading exception. State a concrete
cause and a test that distinguishes it from competing explanations.

Patch the candidate or the host-authorized integration boundary. Do not change
the frozen evaluator, replace expected outputs, drop failing cases, or suppress
errors to manufacture success. Preserve the parent version for comparison.

Rerun the reproducer and the smallest meaningful checks for the changed behavior.
Then run the actual evaluation path when the fix affects scientific results.
Return the cause, patch artifact, test evidence, and any remaining uncertainty.
If the available tool cannot reproduce the environment, record that limitation
and the exact check needed next.
