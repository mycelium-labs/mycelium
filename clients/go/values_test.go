package mycelium_test

import (
	"encoding/json"
	"os"
	"strings"
	"testing"

	mycelium "github.com/mycelium-labs/mycelium/clients/go"
)

func TestDecimalProtocolFixtures(t *testing.T) {
	raw, err := os.ReadFile("../../sdk/docs/spec/fixtures/canonicalization.json")
	if err != nil {
		t.Fatal(err)
	}
	var fixtures struct {
		Cases []struct {
			Name          string          `json:"name"`
			Input         json.RawMessage `json:"input"`
			InvalidInputs []string        `json:"invalid_inputs"`
			Expected      string          `json:"expected"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &fixtures); err != nil {
		t.Fatal(err)
	}
	count := 0
	for _, fixture := range fixtures.Cases {
		if !strings.HasPrefix(fixture.Name, "decimal-") {
			continue
		}
		count++
		t.Run(fixture.Name, func(t *testing.T) {
			switch fixture.Expected {
			case "valid":
				var want mycelium.DecimalValue
				if err := json.Unmarshal(fixture.Input, &want); err != nil {
					t.Fatal(err)
				}
				got, err := mycelium.Decimal(want.Value)
				if err != nil {
					t.Fatal(err)
				}
				if got != want {
					t.Fatalf("got %#v, want %#v", got, want)
				}
			case "error":
				for _, value := range fixture.InvalidInputs {
					if _, err := mycelium.Decimal(value); err == nil {
						t.Errorf("accepted noncanonical decimal %q", value)
					}
				}
			default:
				t.Fatalf("unknown fixture expectation %q", fixture.Expected)
			}
		})
	}
	if count == 0 {
		t.Fatal("decimal protocol fixtures must be present")
	}
}

func TestDecimalNegativeFractionLimits(t *testing.T) {
	value := "-0.000000000000000001"
	got, err := mycelium.Decimal(value)
	if err != nil || got.Value != value {
		t.Fatalf("Decimal(%q) = %#v, %v", value, got, err)
	}
	for _, value := range []string{"-0.0", "-0.10", "-0.0000000000000000001"} {
		if _, err := mycelium.Decimal(value); err == nil {
			t.Errorf("accepted noncanonical decimal %q", value)
		}
	}
}
