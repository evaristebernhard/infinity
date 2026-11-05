"""Small exact certificates for unit-square packing over Q(sqrt(2)).

This checker proves feasibility of its supplied object using rational arithmetic
and the separating-axis criterion for convex polygons. It is neither the Kaggle
decimal validator nor a formal proof assistant. Only a zero area gap closes the
general lower bound; a feasible construction alone does not prove optimality.
Input-size limits belong to the host capability that calls this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Callable


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


class InvalidPacking(ValueError):
    """A well-formed candidate violates the geometric claim being checked."""


def certify(side: Q2, squares: list[Square], *,
            heartbeat: Callable[[], None] | None = None) -> dict:
    """Check unit shape, containment and pairwise disjoint interiors exactly."""
    if heartbeat:
        heartbeat()
    if side.sign() <= 0 or not squares:
        raise InvalidPacking("A positive container and at least one square are required")
    if len({square.name for square in squares}) != len(squares):
        raise InvalidPacking("Square names must be distinct")
    vertices = []
    for square in squares:
        u, v = square.axes()
        if dot(u, u) != Q2(1) or dot(v, v) != Q2(1) or dot(u, v) != Q2(0):
            raise InvalidPacking(f"{square.name}: orientation does not define a unit square")
        points = square.corners()
        if any(value.sign() < 0 or (side-value).sign() < 0 for point in points for value in point):
            raise InvalidPacking(f"{square.name}: corner outside container")
        vertices.append(points)
    certificates = []
    for i, first in enumerate(squares):
        for j in range(i + 1, len(squares)):
            if heartbeat and len(certificates) % 64 == 0:
                heartbeat()
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
                raise InvalidPacking(f"{first.name}, {second.name}: overlapping interiors")
            certificates.append(witness)
    area_gap = side * side - Q2(len(squares))
    if heartbeat:
        heartbeat()
    return {"n": len(squares), "side": side, "feasible": True,
            "squares": [{"name": q.name, "center": [q.x, q.y], "cos_sin": [q.c, q.s],
                         "corners": vertices[i]} for i, q in enumerate(squares)],
            "separating_axes": certificates, "area_lower_bound": f"sqrt({len(squares)})",
            "container_area_minus_n": area_gap,
            "global_optimality": "area_bound_closed" if area_gap == Q2(0) else "not_proven"}


def json_number(value):
    """JSON encoder for exact coefficients; never silently approximate a value."""
    if isinstance(value, Q2):
        return [str(value.a), str(value.b)]
    raise TypeError(f"Cannot encode {type(value).__name__} as an exact number")
