"""Tiny sample module for a local Kiro workflow review demo.

Intentional simplicity: one helper with a clear docstring and a small edge case.
"""

from __future__ import annotations


def format_order_total(cents: int, currency: str = "USD") -> str:
    """Return a human-readable money string from integer cents.

    Examples:
        format_order_total(1999) -> "USD 19.99"
        format_order_total(0) -> "USD 0.00"
    """
    if cents < 0:
        raise ValueError("cents must be non-negative")
    major, minor = divmod(cents, 100)
    return f"{currency} {major}.{minor:02d}"


if __name__ == "__main__":
    print(format_order_total(1299))
