---
name: counterexample-search
description: Search for and verify a counterexample to a precise mathematical or program claim. Use to stress-test a conjecture, a proposed lemma, a universal invariant, or a solver's claimed domain of validity.
---

# Search for a counterexample

Write the exact claim with its domain, quantifiers, assumptions, and exceptional
cases. Derive what a counterexample must satisfy. Check that the proposed search
space still covers the domain being discussed; a finite search cannot settle an
unbounded universal claim by exhaustion.

Start with boundaries, degenerate cases, symmetry classes, and small exhaustive
instances. Use randomized or adversarial search when the domain is large. A
numerical near-violation is a lead: reconstruct an exact witness or obtain an
appropriate interval/certified check before calling it a counterexample.

Minimize the witness while preserving failure. Store the input, representation,
claim fingerprint, verification trace, and all assumptions needed to reproduce
it. Have the registered checker evaluate the witness against the original claim.

If the witness is accepted, identify exactly which claim or dependency is
refuted. If nothing is found, report the searched region and effort; leave the
claim unresolved. Timeouts and search failures are not evidence of truth.
