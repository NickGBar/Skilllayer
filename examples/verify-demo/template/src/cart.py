"""A tiny shopping-cart module used by the skilllayer verify demo."""


def subtotal(items):
    """Sum of price * quantity over (price, quantity) pairs."""
    return sum(price * qty for price, qty in items)


def total(items, discount_pct=0):
    """Total after a percentage discount, rounded to cents."""
    return round(subtotal(items) * (1 - discount_pct / 100), 2)
