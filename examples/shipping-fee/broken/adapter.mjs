// A plausible but wrong JavaScript port, kept as a negative control.
// Bugs: binary floating point with half-up rounding, and unknown zones fall back to zone A.
import { createInterface } from "node:readline";

const RATE = { A: 2.5, B: 3.75, C: 5.1 };

function fee(weightGrams, zone, express) {
  if (weightGrams <= 0) throw Object.assign(new RangeError("weight"), { kind: "invalid_argument" });
  if (zone == null) throw Object.assign(new TypeError("zone"), { kind: "invalid_argument" });
  const rate = RATE[zone.trim().toUpperCase()] ?? RATE.A;
  let amount = (weightGrams / 1000) * rate;
  if (express) amount *= 1.5;
  return (Math.round(amount * 100) / 100).toFixed(2);
}

const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of rl) {
  if (!line.trim()) continue;
  const request = JSON.parse(line);
  let response;
  try {
    const { weight_grams, zone, express } = request.input;
    response = { id: request.id, ok: true, output: { fee: fee(weight_grams, zone, express) } };
  } catch (err) {
    response = { id: request.id, ok: false, error: { type: err.kind ?? `unmapped:${err.name}`, message: String(err.message) } };
  }
  process.stdout.write(JSON.stringify(response) + "\n");
}
