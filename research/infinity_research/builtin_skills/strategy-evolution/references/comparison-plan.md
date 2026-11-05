# Incumbent and challenger comparison

Record this plan before observing challenger results:

1. **Scope:** Which task family and failure modes should improve?
2. **Versions:** What exact incumbent and challenger artifacts will run?
3. **Suite:** Which representative tasks and held-back tasks will be used?
4. **Conditions:** Which tool capabilities, model configuration, inputs, seeds,
   and resource limits remain fixed for an interpretable comparison?
5. **Acceptance:** What verified outcome and regression limits warrant adoption?
6. **Failure accounting:** How will timeouts, crashes, invalid outputs, and
   missing measurements affect the result?
7. **Rollback:** Which previous version and evidence remain available?

Keep strategy descriptions and implementation details out of a blinded evaluator
unless they are inputs the evaluation genuinely needs. Record all tested
challengers so selecting a lucky run cannot masquerade as independent evidence.

For a skill change, include realistic task prompts where the skill should help,
nearby tasks where it should not activate, and missing-tool situations where it
must state a limitation. Syntactic validity alone is not behavioral validation.
