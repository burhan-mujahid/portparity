"""Python port of LegacyShipping.ShippingRules.Fee."""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

RATE_PER_KG = {"A": Decimal("2.50"), "B": Decimal("3.75"), "C": Decimal("5.10")}

# .NET String.Trim() removes characters for which char.IsWhiteSpace is true.
# Python's str.strip() also removes U+001C..U+001F, so the .NET set is spelled out.
DOTNET_WHITESPACE = (
    "\t\n\v\f\r \x85\xa0 "
    "           "
    "    　"
)


class InvalidArgument(ValueError):
    pass


class UnknownZone(KeyError):
    pass


def fee(weight_grams: int, zone: str | None, express: bool) -> Decimal:
    if weight_grams <= 0:
        raise InvalidArgument("weight_grams must be positive")
    if zone is None:
        raise InvalidArgument("zone is required")
    key = zone.strip(DOTNET_WHITESPACE).upper()
    if key not in RATE_PER_KG:
        raise UnknownZone(key)
    amount = Decimal(weight_grams) / 1000 * RATE_PER_KG[key]
    if express:
        amount *= Decimal("1.5")
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
