from src.cart import subtotal, total


def test_subtotal():
    assert subtotal([(10.0, 2), (5.0, 1)]) == 25.0


def test_total_without_discount():
    assert total([(10.0, 2)]) == 20.0


def test_total_with_discount():
    assert total([(10.0, 2)], discount_pct=10) == 18.0


def test_discount_never_makes_the_total_negative():
    assert total([(10.0, 1)], discount_pct=150) == 0.0
