"""Exact, dependency-free packing checks over Q(sqrt(2)); not a Kaggle adapter."""

from __future__ import annotations

import argparse
from dataclasses import replace
from fractions import Fraction
import json
from pathlib import Path
import sys

# Keep the documented direct command working from a source checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from infinity_research.math_certificates import Q2, Square, certify, json_number


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
    print(json.dumps(result, default=json_number, indent=2))


if __name__ == "__main__":
    main()
