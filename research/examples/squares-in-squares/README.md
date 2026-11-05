# An exact square-packing certificate example

This is a small design example for a future mathematical-research adapter.
It is **not integrated into the Infinity research runtime**, and it is not a
Kaggle submission generator or a validation of the competition's decimal metric.

Run from the repository root with Python 3.11 or later; no extra packages are needed:

```bash
python research/examples/squares-in-squares/exact_certificate_demo.py --self-test
python research/examples/squares-in-squares/exact_certificate_demo.py
```

The second command prints complete JSON certificates. Every exact number is
encoded as `[a, b]`, meaning `a + b sqrt(2)`, with rational coefficients stored
as strings. No floating-point rounding or collision tolerance is used.

## Five unit squares

Set the container side to

$$
L=2+\frac{1}{\sqrt{2}}=2+\frac{\sqrt{2}}{2}.
$$

Four axis-aligned unit squares occupy the four corners. Their centers are
`(1/2,1/2)`, `(L-1/2,1/2)`, `(L-1/2,L-1/2)` and `(1/2,L-1/2)`.
The fifth square has center `(L/2,L/2)` and orientation
`cos(theta)=sin(theta)=sqrt(2)/2`, corresponding to 45 degrees.

The checker takes these centers and orientations as candidate data. Independently
of the construction formula, it checks:

1. Each orientation defines orthogonal unit edge directions.
2. Every corner lies inside the closed container `[0,L]^2`.
3. Every pair has a separating axis among the edge-normal directions of the two
   squares. Their projected closed intervals may touch; their interiors cannot
   overlap. The output records the axis, both projection intervals and their
   exact nonnegative gap for all ten pairs.

For example, the southwest corner square and the central square touch on
`x+y=2`. The checker accepts that contact, while moving the southwest square
by `(1/10,1/10)` creates an overlap and is rejected.

This establishes a feasible construction and hence the upper bound
`L_min(5) <= 2 + sqrt(2)/2`. **It does not prove global optimality for five
squares.** Passing an exact checker for one construction supplies no proof that
a smaller container is impossible.

## Four unit squares

The example also checks the 2 by 2 grid in a square of side 2. Non-overlapping
unit squares require container area at least their total area, so any packing
of four squares needs `L^2 >= 4`. The checked construction attains this bound.
Its certificate therefore reports `area_bound_closed`; the five-square
certificate reports `not_proven`.

## What is trusted, and what remains separate

The implementation uses Python's standard-library `Fraction` and exact arithmetic
in `Q(sqrt(2))`. The sign of `a+b sqrt(2)` is decided using rational signs and,
when the terms have opposite signs, the comparison of `a^2` with `2b^2`.
Pairwise separation uses the separating-axis property of convex polygons.
The trusted base includes this Python checker and that geometric argument;
**this is not a proof checked by Lean or another proof-assistant kernel**.

The self-test checks arithmetic and both valid examples, and rejects three
deliberately invalid constructions: overlapping squares, an out-of-bounds square,
and an orientation with `cos(theta)^2 + sin(theta)^2 != 1`.

Conversion to a competition CSV remains a different operation. Irrational
coordinates need decimal approximations, and rounding at exact contacts can
introduce overlap. A future adapter must check the exported decimal file with
the pinned official metric, separately from this algebraic certificate. This
example does not establish that any Kaggle submission has passed.
