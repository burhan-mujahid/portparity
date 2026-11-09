// portparity adapter for the legacy rule: one JSON request per stdin line, one JSON
// response per stdout line. Diagnostics go to stderr only.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using System.Text.Json.Nodes;
using LegacyShipping;

var stdin = new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false));
var stdout = new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false)) { AutoFlush = true, NewLine = "\n" };

string? line;
while ((line = stdin.ReadLine()) != null)
{
    if (line.Length == 0) continue;
    var request = JsonNode.Parse(line)!.AsObject();
    var id = request["id"]!.GetValue<string>();
    JsonObject response;
    try
    {
        var (weight, zone, express) = Adapter.Parse(request["input"]);
        var fee = ShippingRules.Fee(weight, zone, express);
        response = new JsonObject
        {
            ["id"] = id,
            ["ok"] = true,
            ["output"] = new JsonObject { ["fee"] = fee.ToString("0.00", CultureInfo.InvariantCulture) },
        };
    }
    catch (Exception ex)
    {
        response = new JsonObject
        {
            ["id"] = id,
            ["ok"] = false,
            ["error"] = new JsonObject { ["type"] = Adapter.ErrorType(ex), ["message"] = ex.Message },
        };
    }
    stdout.WriteLine(response.ToJsonString());
}

sealed class BadInput(string message) : Exception(message);

static class Adapter
{
    // Map native exceptions to the shared error vocabulary. Unmapped types stay visible.
    public static string ErrorType(Exception ex) => ex switch
    {
        BadInput => "bad_input",
        ArgumentOutOfRangeException or ArgumentNullException => "invalid_argument",
        KeyNotFoundException => "unknown_zone",
        _ => "unmapped:" + ex.GetType().Name,
    };

    public static (int, string?, bool) Parse(JsonNode? node)
    {
        if (node is not JsonObject input) throw new BadInput("input must be an object");
        if (input["weight_grams"] is not JsonValue w || !w.TryGetValue<int>(out var weight))
            throw new BadInput("weight_grams must be a 32-bit integer");
        string? zone = null;
        var z = input["zone"];
        if (z is not null && (z is not JsonValue zv || !zv.TryGetValue<string>(out zone)))
            throw new BadInput("zone must be a string or null");
        if (input["express"] is not JsonValue e || !e.TryGetValue<bool>(out var express))
            throw new BadInput("express must be a boolean");
        return (weight, zone, express);
    }
}
