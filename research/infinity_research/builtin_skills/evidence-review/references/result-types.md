# Result types

| Evidence | What it can support | What it does not establish |
| --- | --- | --- |
| Program execution succeeded | That execution finished under recorded inputs | Correct output or scientific validity |
| Fixed validation metric | The recorded performance on that split/protocol | Unseen-data performance or optimality |
| Checked feasible construction | Existence and an attained objective value | A matching lower bound |
| Checked counterexample | Failure of the exact quantified claim it contradicts | Failure of every related claim |
| Accepted formal proof | The frozen formal target under reported axioms | Correct translation of an unreviewed natural-language target |
| Solver reported optimum | Optimum within its model and certification conditions | Optimum for a broader or differently encoded problem |
| Search found no counterexample | The recorded search did not find one | Universal truth |
| Timeout, build error, unknown | That attempt did not settle the question | Mathematical falsity |

Separate work status from knowledge status. A task can be completed with an
honest negative result or a useful hypothesis. A completed queue entry cannot
upgrade the hypothesis into a proved statement.
