"""Tests for demo_pkg.utils."""

import pytest

from demo_pkg.utils import add, multiply


def test_add() -> None:
    """Test that add returns the sum of two integers."""
    assert add(2, 3) == 5
    assert add(-1, 1) == 0
    assert add(0, 0) == 0


def test_multiply() -> None:
    """Test that multiply returns the product of two integers."""
    assert multiply(2, 3) == 6
    assert multiply(-1, 1) == -1
    assert multiply(0, 5) == 0


@pytest.mark.parametrize(
    "a, b, expected",
    [
        (1, 2, 3),
        (10, 20, 30),
        (100, -100, 0),
    ],
)
def test_add_parametrized(a: int, b: int, expected: int) -> None:
    """Parametrized test for add."""
    assert add(a, b) == expected


@pytest.mark.parametrize(
    "a, b, expected",
    [
        (1, 2, 2),
        (3, 4, 12),
        (5, -1, -5),
    ],
)
def test_multiply_parametrized(a: int, b: int, expected: int) -> None:
    """Parametrized test for multiply."""
    assert multiply(a, b) == expected