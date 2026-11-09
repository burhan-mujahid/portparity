using System;
using System.Collections.Generic;

namespace LegacyShipping;

// The legacy business rule being migrated. Its quirks are the behavior to preserve:
// the zone is trimmed with .NET whitespace rules and upper-cased, and the fee uses
// decimal arithmetic with banker's rounding (Math.Round defaults to MidpointRounding.ToEven).
public static class ShippingRules
{
    private static readonly Dictionary<string, decimal> RatePerKg = new()
    {
        ["A"] = 2.50m,
        ["B"] = 3.75m,
        ["C"] = 5.10m,
    };

    public static decimal Fee(int weightGrams, string? zone, bool express)
    {
        if (weightGrams <= 0) throw new ArgumentOutOfRangeException(nameof(weightGrams));
        if (zone is null) throw new ArgumentNullException(nameof(zone));
        decimal rate = RatePerKg[zone.Trim().ToUpperInvariant()];
        decimal fee = weightGrams / 1000m * rate;
        if (express) fee *= 1.5m;
        return Math.Round(fee, 2);
    }
}
