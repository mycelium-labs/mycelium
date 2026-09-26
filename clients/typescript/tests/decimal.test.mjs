import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { decimal, MyceliumLocalValidationError } from "@mycelium-labs/sidecar-client";

const fixtures = JSON.parse(readFileSync(
  new URL("../../../sdk/docs/spec/fixtures/canonicalization.json", import.meta.url),
  "utf8",
)).cases.filter((item) => item.name.startsWith("decimal-"));

assert(fixtures.length > 0, "decimal protocol fixtures must be present");
for (const fixture of fixtures) {
  test(fixture.name, () => {
    if (fixture.expected === "valid") {
      assert.deepEqual(decimal(fixture.input.value), fixture.input);
    } else {
      assert.equal(fixture.expected, "error");
      for (const value of fixture.invalid_inputs) {
        assert.throws(() => decimal(value), MyceliumLocalValidationError, value);
      }
    }
  });
}

test("negative fractional limits retain decimal-1 normalization", () => {
  assert.equal(decimal("-0.000000000000000001").value, "-0.000000000000000001");
  for (const value of ["-0.0", "-0.10", "-0.0000000000000000001"]) {
    assert.throws(() => decimal(value), MyceliumLocalValidationError, value);
  }
});
