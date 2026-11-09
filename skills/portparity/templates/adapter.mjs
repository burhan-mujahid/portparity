// portparity adapter template (Node.js 18+).
// Protocol: one JSON request per stdin line, one JSON response per stdout line.
// Log to stderr only (console.error). Works in "process" and "stream" mode.
// Caution: JavaScript numbers are doubles. 1 and 1.0 serialize identically, and integers
// beyond 2**53 lose precision. Encode such values as strings on both sides.
import { createInterface } from "node:readline";

class BadInput extends Error {}

// Map native errors to the shared error vocabulary used by BOTH sides.
function errorType(err) {
  if (err instanceof BadInput) return "bad_input";
  // if (err instanceof RangeError) return "invalid_argument";
  return `unmapped:${err?.name ?? typeof err}`;
}

function call(value) {
  return value; // TODO: replace with the real call
}

const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of rl) {
  if (!line.trim()) continue;
  const request = JSON.parse(line);
  let response;
  try {
    response = { id: request.id, ok: true, output: call(request.input) };
  } catch (err) {
    response = { id: request.id, ok: false, error: { type: errorType(err), message: String(err?.message ?? err) } };
  }
  process.stdout.write(JSON.stringify(response) + "\n");
}
