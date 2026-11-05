---
name: technical-disclosure
description: Turn invention notes, designs, code, and experimental records into a technical feature ledger and a structured technical disclosure or specification draft. Use when an implementation needs its problem, mechanism, effects, embodiments, and missing details organized before claim drafting. Use patent-research for external evidence retrieval and claim-support-review for auditing an existing claim set.
---

# Prepare a technical disclosure

## Inputs

Read the requested technical scope, original notes and their authors or supplied
provenance, design versions, figures, code, and any calculation or experimental
records. Identify the intended document language and jurisdiction if supplied.
Use [disclosure-record.md](references/disclosure-record.md) for official
structure references and a feature-record example. Treat the technical
disclosure as an internal drafting input, not a separate statutory filing
document required by those references.

## Procedure

1. Register the source artifacts and revisions before drafting. Keep their
   original wording or location references available. Record host-provided
   hashes when present; do not invent a fingerprint or imply that an unread
   source was inspected.
2. Build a ledger with stable feature IDs, definitions, components or steps,
   relationships, parameter meanings and units, variants, and source locations.
   Separate a source statement, calculation, simulation, measured result,
   inference, and proposed experiment. Record whether each item is reported by
   a source, observed in available evidence, or not performed. An author's
   reported measurement is not an independently reproduced measurement.
3. Connect each technical problem to the relevant features, their operating
   mechanism, the expected effect, and the supporting evidence. For a reported
   improvement, retain the baseline, conditions, units, and limitations when
   available. Label a predicted effect as predicted. Record calculation inputs,
   assumptions, code or method, and actual execution receipts when available;
   a proposed calculation is not a computed result.
4. Draft the technical field, background, technical problem and solution,
   effects, figure descriptions, and embodiments using the ledger. Explain how
   the implementation works: interfaces, sequence, dependencies, parameter
   roles, and operating conditions. Distinguish sourced implementations from
   proposed alternatives. Use visible missing-detail markers where the records
   do not explain an essential step; do not invent dimensions, ranges, test
   values, experiments, or historical development.
5. Check terminology, feature IDs, figure labels, and conditions across the
   draft. Make each asserted effect traceable to its basis. Keep unsupported
   effects as hypotheses or open questions. Refer unresolved external-evidence
   needs to a research task when delegation is available; do not assume this
   skill grants search, execution, drawing, or document-generation tools.

## Deliverables and missing information

Return the source register, technical feature ledger, disclosure draft, and a
gap list. For each gap, identify the affected feature or section, why it matters,
and the record, calculation, experiment, or inventor clarification needed to
resolve it. Return artifact content to the parent if no authorized writer is
available. Preserve useful draft sections even when other sections remain open.

Mark the document as a draft and keep proposed work separate from completed
work. Do not assign inventorship from agent participation, turn a plausible
mechanism into a measured effect, or represent drafting completeness as a
patentability conclusion. Pass the fixed draft revision and ledger to a separate
claim-support review when a claim set becomes available.
