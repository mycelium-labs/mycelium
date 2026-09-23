import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import { decimal } from "../dist/index.js";

const fixture = JSON.parse(
  await readFile(new URL("../../../sdk/docs/spec/fixtures/canonicalization.json", import.meta.url)),
);

for (const testCase of fixture.cases) {
  if (!testCase.name.startsWith("decimal-")) continue;

  if (testCase.input) {
    assert.deepEqual(decimal(testCase.input.value), testCase.input);
    continue;
  }

  for (const value of testCase.invalid_inputs) {
    assert.throws(() => decimal(value), { name: "MyceliumLocalValidationError" });
  }
}
