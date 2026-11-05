# Squares in Squares task notes

Read the campaign's pinned contract first. These notes summarize the official
metric observed on 2026-11-05 and must not silently override a different version.

- Instances have n = 1 through 1000 unit squares.
- Submission columns are id, n, square_id, x, y, angle. Coordinates denote centers;
  angles use radians. Square IDs within an instance are 0 through n - 1.
- The enclosing side L is the larger span of all corners in x and y. The valid
  instance contribution is max(0, L / sqrt(n) - 1); lower is better.
- Missing or invalid instances receive a grid-based fallback penalty. For a
  perfect-square n that penalty is zero. A zero score is therefore insufficient
  evidence that a submitted layout exists or has been checked.
- Emit finite decimal/scientific literals, not symbolic expressions. Exact
  algebraic contact certificates do not automatically certify the rounded CSV.
- A packing accepted by the competition checker establishes competition
  validity under that checker. It does not establish global optimality.

Official references:

- https://www.kaggle.com/competitions/squares-in-squares/data
- https://www.kaggle.com/code/metric/squares-in-squares/output

Keep the metric code version/hash with receipts. Confirm the applicable contest
rules before reusing third-party constructions; public availability alone does
not establish permission for a particular competition.
