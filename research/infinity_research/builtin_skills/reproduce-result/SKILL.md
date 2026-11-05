---
name: reproduce-result
description: Recreate a result from its recorded artifacts and write a durable handoff for another worker. Use before accepting a major improvement, resuming interrupted research, or transferring a program, construction, or proof between tasks.
---

# Reproduce a result

Read the original evidence record and obtain exact artifact versions. Record
source hash, input hash, task contract, tool/checker versions, environment image,
parameters, seeds, thread settings, and any starting checkpoint. If a field is
missing, mark the reproduction incomplete rather than guessing it silently.

Use the available execution tools in a clean workspace. Keep generated outputs
separate from original artifacts. Run the recorded procedure and independently
check the output. Compare the right property: exact bytes for deterministic
certificates, toleranced quantities for numerical results, or a distribution
across seeds when randomness matters.

On disagreement, isolate changes in data, code, environment, initialization,
resource limits, and metric implementation. Preserve both results and the
diagnostic comparison. A changed checker or task target creates a new evaluation
context and must not be mixed into the original ranking.

Write a compact handoff containing artifact references, the commands or tool
calls that ran, observed results, accepted scope, remaining questions, and the
next useful task. Keep this record sufficient for a fresh worker; a reference
to an old conversation is not a reproducible procedure.
