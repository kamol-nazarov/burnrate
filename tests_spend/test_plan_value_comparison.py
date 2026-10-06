"""Plans against value keeps a lower bound when the month is only partly priced."""

from decimal import Decimal

from spend_app.aggregate import plan_value_comparison


def test_complete_month_is_a_ratio_including_zero():
    shown, basis, multiple = plan_value_comparison(
        Decimal("0"), complete=True, accrual_to_date=Decimal("100")
    )
    assert shown == 0
    assert basis == "ratio"
    assert multiple == 0


def test_partial_month_keeps_the_known_usage_as_a_lower_bound():
    shown, basis, multiple = plan_value_comparison(
        Decimal("2869.91"), complete=False, accrual_to_date=Decimal("78.88")
    )
    assert shown == Decimal("2869.91")
    assert basis == "lower_bound"
    assert multiple == float(Decimal("2869.91") / Decimal("78.88"))


def test_incomplete_month_with_no_priced_usage_stays_unknown():
    shown, basis, multiple = plan_value_comparison(
        Decimal("0"), complete=False, accrual_to_date=Decimal("80")
    )
    assert shown is None
    assert basis == "unavailable"
    assert multiple is None
