package mycelium

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

type decimalFixture struct {
	Name          string          `json:"name"`
	Input         json.RawMessage `json:"input"`
	InvalidInputs []string        `json:"invalid_inputs"`
}

type canonicalizationFixture struct {
	Cases []decimalFixture `json:"cases"`
}

func TestDecimalMatchesFixture(t *testing.T) {
	_, file, _, _ := runtime.Caller(0)
	fixturePath := filepath.Join(filepath.Dir(file), "..", "..", "sdk", "docs", "spec", "fixtures", "canonicalization.json")
	contents, err := os.ReadFile(fixturePath)
	if err != nil {
		t.Fatal(err)
	}
	var fixture canonicalizationFixture
	if err := json.Unmarshal(contents, &fixture); err != nil {
		t.Fatal(err)
	}

	for _, testCase := range fixture.Cases {
		if len(testCase.Name) < len("decimal-") || testCase.Name[:len("decimal-")] != "decimal-" {
			continue
		}
		if len(testCase.Input) > 0 {
			var input struct {
				Value string `json:"value"`
			}
			if err := json.Unmarshal(testCase.Input, &input); err != nil {
				t.Fatalf("%s: decode input: %v", testCase.Name, err)
			}
			value, err := Decimal(input.Value)
			if err != nil {
				t.Errorf("%s: unexpected error: %v", testCase.Name, err)
				continue
			}
			if value.Value != input.Value {
				t.Errorf("%s: got %q, want %q", testCase.Name, value.Value, input.Value)
			}
			continue
		}
		for _, input := range testCase.InvalidInputs {
			if _, err := Decimal(input); err == nil {
				t.Errorf("%s: %q unexpectedly accepted", testCase.Name, input)
			}
		}
	}
}

func TestDecimalNegativeFractionLimits(t *testing.T) {
	value := "-0.000000000000000001"
	got, err := Decimal(value)
	if err != nil || got.Value != value {
		t.Fatalf("Decimal(%q) = %#v, %v", value, got, err)
	}
	for _, value := range []string{"-0.0", "-0.10", "-0.0000000000000000001"} {
		if _, err := Decimal(value); err == nil {
			t.Errorf("accepted noncanonical decimal %q", value)
		}
	}
}
