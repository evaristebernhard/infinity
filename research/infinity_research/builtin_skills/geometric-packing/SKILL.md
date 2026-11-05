---
name: geometric-packing
description: Construct and improve geometric packings with explicit feasibility certificates and bounds. Use for squares in a square, packing competitions, overlap diagnosis, contact-graph search, or claims of packing optimality.
---

# Search geometric packings

Freeze the shape dimensions, allowed transformations, container, objective,
coordinate convention, angle units, and contact rules. For the Kaggle Squares
in Squares adapter, read [the task notes](references/squares-in-squares.md) and
the campaign's pinned official metric before producing a submission.

Maintain an archive per instance size. Store multiple structural families,
their producing solvers, and checked objective values. Start with a valid simple
construction so unsuccessful searches can fall back to an actual feasible object.

Try different representations: coordinates and angles, lattice patterns,
contact graphs, block decompositions, exact algebraic coordinates, or rational
rotation parameters. Deduplicate only under transformations that preserve the
actual container objective. Arbitrary global rotation need not preserve an
axis-aligned square bounding box.

Use coarse numerical search to find candidates, then check all containment and
pairwise non-overlap conditions independently. Treat unresolved precision as
unknown. Exporting exact coordinates to decimal text changes the object; check
the actual exported bytes again, especially when squares touch.

Keep feasible upper bounds separate from proved lower bounds. For n unit
squares the area bound is sqrt(n); an axis-aligned grid gives ceil(sqrt(n)).
Matching bounds can close the problem, but a good numerical layout alone cannot.
Return the construction, checker evidence, current bounds, and the unresolved gap.
