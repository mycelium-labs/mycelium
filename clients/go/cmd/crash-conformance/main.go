package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"time"

	mycelium "github.com/mycelium-labs/mycelium/clients/go"
)

func required(name string) string {
	value := os.Getenv(name)
	if value == "" {
		panic("missing " + name)
	}
	return value
}

func number(name string, fallback float64) float64 {
	raw := os.Getenv(name)
	if raw == "" {
		return fallback
	}
	value, err := strconv.ParseFloat(raw, 64)
	if err != nil {
		panic(fmt.Sprintf("invalid %s: %v", name, err))
	}
	return value
}

func main() {
	startAtMillis := number("MYCELIUM_CONFORMANCE_START_AT_MS", 0)
	holdMillis := number("MYCELIUM_CONFORMANCE_HOLD_MS", 0)
	leaseTTL := number("MYCELIUM_CONFORMANCE_LEASE_TTL", 1)
	if holdMillis < 0 || leaseTTL <= 0 {
		panic("invalid crash-conformance timing")
	}
	if wait := time.Until(time.UnixMilli(int64(startAtMillis))); wait > 0 {
		time.Sleep(wait)
	}

	tenant := mycelium.TenantID(required("MYCELIUM_CONFORMANCE_TENANT"))
	application := mycelium.ApplicationID(required("MYCELIUM_CONFORMANCE_APPLICATION"))
	requestID := required("MYCELIUM_CONFORMANCE_REQUEST_ID")
	client, err := mycelium.NewClient(mycelium.ClientOptions{
		BaseURL:       required("MYCELIUM_CONFORMANCE_URL"),
		Token:         required("MYCELIUM_CONFORMANCE_TOKEN"),
		TenantID:      tenant,
		ApplicationID: application,
	})
	if err != nil {
		panic(err)
	}
	identity := mycelium.IdentityRequest{
		ApplicationID:           application,
		BusinessRequestID:       mycelium.BusinessRequestID(requestID),
		CanonicalizationVersion: "jcs-1",
		Destination:             mycelium.Destination{"id": "record-9", "kind": "record"},
		ExecutionScope: mycelium.ExecutionScope{
			"entity": "record-9", "tenant": string(tenant),
		},
		IdentityVersion:     "1",
		Input:               map[string]any{"operation": "update", "value": "new-value"},
		TenantID:            tenant,
		ToolContractVersion: "1",
		ToolID:              "external_operation",
	}
	claim, err := client.ClaimEffect(context.Background(), mycelium.ClaimEffectRequest{
		IdentityRequest: identity,
		Decision: &mycelium.Decision{
			Allowed: true, Verdicts: []mycelium.Verdict{}, DeniedReasons: []string{},
		},
		LeaseTTL: &leaseTTL,
	})
	if err != nil {
		panic(err)
	}
	boundary := os.Getenv("MYCELIUM_CONFORMANCE_BOUNDARY")
	if claim.Disposition == mycelium.ClaimExecute && boundary != "" {
		if _, err := client.RecordEffectBoundary(
			context.Background(), claim.Handle, mycelium.BoundaryState(boundary),
		); err != nil {
			panic(err)
		}
	}

	result := map[string]any{
		"client":       "go",
		"disposition":  claim.Disposition,
		"effect_id":    claim.EffectID,
		"effect_state": claim.EffectState,
		"owner_id":     nil,
		"fence":        nil,
		"result":       claim.Result,
	}
	if claim.Handle != nil {
		result["owner_id"] = claim.Handle.OwnerID
		result["fence"] = claim.Handle.Fence
	}
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		panic(err)
	}
	_ = os.Stdout.Sync()

	if claim.Disposition == mycelium.ClaimExecute && holdMillis > 0 {
		time.Sleep(time.Duration(holdMillis) * time.Millisecond)
	}
}
