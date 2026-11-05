---
name: claim-support-review
description: Review an existing claim set against a fixed description, drawings, and technical feature ledger. Use to find missing disclosure links, unsupported generalizations, inconsistent terminology, dependency errors, or gaps between a claimed combination and its embodiments. Produce a traceable support matrix; do not use this skill as a prior-art search or a patentability decision.
---

# Review claim support

## Inputs

Read the exact claim-set revision, description revision, drawings, feature ledger,
and available source evidence. Record artifact IDs and host-provided hashes so
the review stays bound to those versions. If claims or the description are
missing, produce a review plan and a missing-input list; do not reconstruct the
missing text from an agent's summary. Read
[support-matrix.md](references/support-matrix.md) for the official support
requirements and a matrix example.

## Procedure

1. Identify each independent and dependent claim. Expand the inherited features
   of dependent claims and record the dependency links. Flag missing claim
   numbers, cycles, inconsistent terms, and conflicts between inherited and
   added limitations. Preserve the claim wording under review.
2. Split each claim into technical features, relationships, operating steps,
   ranges, and functional limitations. Map every feature to exact description
   passages, embodiments, and figure labels. Record whether the cited material
   is explicit, inferred, partial, not located, or unavailable. A keyword match
   is only a lead for review, not support for the whole feature.
3. Check the claimed combination as well as its individual features. Features
   described separately are not automatically disclosed together. Compare the
   claimed scope with the disclosed conditions, ranges, alternatives, and
   implementation detail. Explain why a broader or functional expression may
   need additional review; do not silently narrow the claim or expand the
   description to make the matrix pass.
4. Distinguish the description's statements from supporting calculation,
   simulation, experiment, or reviewer inference. Check the actual referenced
   records where tools permit. An asserted benefit or another agent's agreement
   is not a substitute for implementation detail or evidence. Source access
   failure means unassessed, not proof that support is absent.
5. Classify each row as candidate support, a documented gap, or requiring
   further review, with a reason and next action. Keep substantive scope and
   support judgments as review findings. A tool that checks artifact hashes or
   reference existence does not thereby verify legal support. Do not create a
   self-issued verified or patentability flag.

## Deliverables and missing information

Return a claim-support matrix, a short review report, and proposed revision or
evidence requests. Include the reviewed revisions, feature and combination
coverage, exact source locations, unresolved assumptions, and dependency or
terminology issues. Separate optional drafting suggestions from findings about
the current text. If a revision is made by another task, require review against
the new artifact revision rather than reusing the old result.

If no retrieval or artifact-reading capability exists, work only from the
provided text and list the unavailable sources. Return structured content to
the parent when no authorized writer is available. Mark the result as draft
review: it does not decide novelty, inventiveness, validity, infringement,
freedom to operate, or whether a patent will be granted. Route those distinct
questions to appropriately scoped work rather than inferring them from a
complete support matrix.
