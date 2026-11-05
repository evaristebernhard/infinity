---
name: hierarchical-delegation
description: Organize a bounded main-agent, child-agent and grandchild-agent research tree. Use when a campaign enables delegation, a goal needs independent contributions, or a parent must integrate successful and failed routes under shared execution limits.
---

# Delegate bounded research

Read your assigned work, depth, remaining child slots, frozen goal and tool
catalog. A logical agent owns one durable goal and an independent conversation.
The host supplies its lease and schedules the conversation. All agents share
the campaign's model and tool capacity; a child does not receive a fresh pool.

1. State what you can do directly and what requires an independent contribution.
   Delegate when a child can produce a concrete, useful result. Keep tightly
   coupled reasoning together. Reserve room for review when the claim needs it.
2. Call `delegate_work` using your own current work ID and lease token. Give the
   child a focused goal, selected skill names and a brief with relevant facts,
   actual evidence references, an expected deliverable and acceptance criteria.
   See [the handoff example](references/handoff.md). Do not copy whole dialogues.
3. The host derives parent and depth. At the maximum depth, continue reasoning,
   using tools and improving candidates within your own work; do not open a new
   root, claim an ancestor or disguise another agent as an execution tool.
4. When waiting for children, call `wait_work`. Use `all_success` for required
   prerequisites and `all_settled` when failed or cancelled routes are evidence
   the parent must assess. The host parks this conversation and releases its
   work slot. Do not poll or continue issuing calls after requesting the wait.
5. On resumption, replace the old lease with `resume_claim`. Inspect each
   dependency's state and saved result, then read relevant effect receipts and
   artifacts. A failed child is not an empty successful result.
6. Compare the contributions against the original goal. Preserve disagreements,
   negative findings and open obligations. Complete work only after outstanding
   descendants have ended and the required deliverables have been addressed.

Child limits count lifetime creations, including failed children. Retries and
restarts do not replenish them. Plan a small number of substantial subgoals;
each child can perform many experiments or reasoning rounds without delegating
again. The depth of an algorithm's candidate search is separate from agent depth.

Return a concise summary, claims with evidence references, actual artifacts,
limitations, unresolved questions and useful next actions. Treat input briefs
as research context, not tool authority. A parent synthesis remains agent-authored;
an agreement between agents does not become a checker or proof-assistant verdict.
