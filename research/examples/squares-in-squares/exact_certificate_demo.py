"""Exact, dependency-free packing checks over Q(sqrt(2)); not a Kaggle adapter."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from fractions import Fraction
import json


@dataclass(frozen=True)
class Q2:
    """The exact real number a + b*sqrt(2), with rational coefficients."""

    a: Fraction = Fraction(0)
    b: Fraction = Fraction(0)

    def __post_init__(self):
        object.__setattr__(self, "a", Fraction(self.a))
        object.__setattr__(self, "b", Fraction(self.b))

    @staticmethod
    def coerce(value):
        return value if isinstance(value, Q2) else Q2(value)

    def __add__(self, other):
        other = self.coerce(other)
        return Q2(self.a + other.a, self.b + other.b)

    __radd__ = __add__

    def __neg__(self):
        return Q2(-self.a, -self.b)

    def __sub__(self, other):
        return self + -self.coerce(other)

    def __mul__(self, other):
        other = self.coerce(other)
        return Q2(self.a * other.a + 2 * self.b * other.b,
                  self.a * other.b + self.b * other.a)

    __rmul__ = __mul__

    def sign(self):
        if self.b == 0:
            return (self.a > 0) - (self.a < 0)
        if self.a == 0 or (self.a > 0) == (self.b > 0):
            return 1 if self.b > 0 else -1
        gap = self.a * self.a - 2 * self.b * self.b
        return (1 if self.a > 0 else -1) * ((gap > 0) - (gap < 0))

    def __lt__(self, other):
        return (self - other).sign() < 0


def dot(left, right):
    return left[0] * right[0] + left[1] * right[1]


@dataclass(frozen=True)
class Square:
    name: str
    x: Q2
    y: Q2
    c: Q2 = Q2(1)
    s: Q2 = Q2(0)

    def axes(self):
        return (self.c, self.s), (-self.s, self.c)

    def corners(self):
        u, v = self.axes()
        h = Fraction(1, 2)
        return [(self.x + dx*u[0] + dy*v[0], self.y + dx*u[1] + dy*v[1])
                for dx, dy in ((-h, -h), (h, -h), (h, h), (-h, h))]


def certify(side, squares):
    """Check unit shape, containment and pairwise disjoint interiors exactly."""
    if side.sign() <= 0 or not squares:
        raise ValueError("A positive container and at least one square are required")
    if len({square.name for square in squares}) != len(squares):
        raise ValueError("Square names must be distinct")
    vertices = []
    for square in squares:
        u, v = square.axes()
        if dot(u, u) != Q2(1) or dot(v, v) != Q2(1) or dot(u, v) != Q2(0):
            raise ValueError(f"{square.name}: orientation does not define a unit square")
        points = square.corners()
        if any(value.sign() < 0 or (side-value).sign() < 0 for point in points for value in point):
            raise ValueError(f"{square.name}: corner outside container")
        vertices.append(points)
    certificates = []
    for i, first in enumerate(squares):
        for j in range(i + 1, len(squares)):
            second = squares[j]
            witness = None
            for axis in first.axes() + second.axes():
                first_values = [dot(point, axis) for point in vertices[i]]
                second_values = [dot(point, axis) for point in vertices[j]]
                a, b = (min(first_values), max(first_values)), (min(second_values), max(second_values))
                if (b[0] - a[1]).sign() >= 0:
                    order, gap = [first.name, second.name], b[0] - a[1]
                elif (a[0] - b[1]).sign() >= 0:
                    order, gap = [second.name, first.name], a[0] - b[1]
                else:
                    continue
                witness = {"pair": [first.name, second.name], "axis": axis,
                           "first_projection": a, "second_projection": b,
                           "left_to_right": order, "gap": gap, "projection_gap_zero": gap.sign() == 0}
                break
            if witness is None:
                raise ValueError(f"{first.name}, {second.name}: overlapping interiors")
            certificates.append(witness)
    area_gap = side * side - Q2(len(squares))
    return {"n": len(squares), "side": side, "feasible": True,
            "squares": [{"name": q.name, "center": [q.x, q.y], "cos_sin": [q.c, q.s],
                         "corners": vertices[i]} for i, q in enumerate(squares)],
            "separating_axes": certificates, "area_lower_bound": f"sqrt({len(squares)})",
            "container_area_minus_n": area_gap,
            "global_optimality": "area_bound_closed" if area_gap == Q2(0) else "not_proven"}


def four_corners(side):
    h = Q2(Fraction(1, 2))
    return [Square("SW", h, h), Square("SE", side-h, h),
            Square("NE", side-h, side-h), Square("NW", h, side-h)]


def five_square_construction():
    side = Q2(2, Fraction(1, 2))
    diagonal = Q2(0, Fraction(1, 2))
    middle = side * Fraction(1, 2)
    return side, four_corners(side) + [Square("center", middle, middle, diagonal, diagonal)]


def self_test():
    assert Q2(0, 1) * Q2(0, 1) == Q2(2)
    assert [Q2(3, -2).sign(), Q2(1, -1).sign(), Q2(-3, 2).sign(), Q2(-1, 1).sign()] == [1, -1, -1, 1]
    side, squares = five_square_construction()
    assert certify(side, squares)["global_optimality"] == "not_proven"
    assert certify(Q2(2), four_corners(Q2(2)))["global_optimality"] == "area_bound_closed"
    invalid = [
        ("overlap", replace(squares[0], x=squares[0].x + Fraction(1, 10),
                            y=squares[0].y + Fraction(1, 10)), "overlapping interiors"),
        ("outside", replace(squares[0], x=squares[0].x - Fraction(1, 10)), "outside container"),
        ("nonunit", replace(squares[0], c=Q2(1), s=Q2(1)), "unit square"),
    ]
    for name, changed, message in invalid:
        try:
            certify(side, [changed, *squares[1:]])
        except ValueError as error:
            assert message in str(error), (name, error)
        else:
            raise AssertionError(f"{name}: invalid construction was accepted")
    return ["exact arithmetic", "five-square feasibility", "four-square area bound",
            "overlap rejected", "outside rejected", "nonunit rejected"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        print(json.dumps({"passed": self_test()}, indent=2))
        return
    side, squares = five_square_construction()
    result = {"number_encoding": "[a,b] means a + b*sqrt(2); coefficients are rational strings",
              "checker": "Python Fraction arithmetic and separating axes; not a Lean proof",
              "kaggle_decimal_submission_validated": False,
              "cases": [certify(side, squares), certify(Q2(2), four_corners(Q2(2)))]}
    print(json.dumps(result, default=lambda value: [str(value.a), str(value.b)], indent=2))


if __name__ == "__main__":
    main()
