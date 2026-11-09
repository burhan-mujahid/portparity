// portparity adapter template (.NET 8+).
// Protocol: one JSON request per stdin line, one JSON response per stdout line.
// Log to stderr only (Console.Error). Works in "process" and "stream" mode.
// Build once (dotnet build -c Release -o out) and run the DLL; "dotnet run" per case is slow.
using System;
using System.IO;
using System.Text;
using System.Text.Json.Nodes;

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
        response = new JsonObject { ["id"] = id, ["ok"] = true, ["output"] = Adapter.Call(request["input"]) };
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
    // Translate the JSON input into a call to the legacy code and return a JSON result.
    // Format decimals with CultureInfo.InvariantCulture as strings to keep exact values.
    public static JsonNode? Call(JsonNode? input) => input?.DeepClone(); // TODO: replace with the real call

    // Map native exceptions to the shared error vocabulary used by BOTH sides.
    public static string ErrorType(Exception ex) => ex switch
    {
        BadInput => "bad_input",
        // ArgumentException => "invalid_argument",
        _ => "unmapped:" + ex.GetType().Name,
    };
}
