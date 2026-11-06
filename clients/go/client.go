package mycelium

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	neturl "net/url"
	"reflect"
	"strconv"
	"strings"
	"time"
)

const maxResponseBytes = 1024 * 1024

// TokenProvider returns the current bearer token. It must honor ctx and be safe
// for concurrent calls. Provider errors and credentials are never included in errors.
type TokenProvider func(ctx context.Context) (string, error)

// Client is a thin HTTP adapter. It stores no authoritative transition state.
type Client struct {
	baseURL       string
	tokenProvider TokenProvider
	token         string
	tenantID      TenantID
	applicationID ApplicationID
	httpClient    *http.Client
	timeout       time.Duration
}
type ClientOptions struct {
	BaseURL       string
	TokenProvider TokenProvider
	Token         string
	TenantID      TenantID
	ApplicationID ApplicationID
	HTTPClient    *http.Client
	Timeout       time.Duration
}

func NewClient(options ClientOptions) (*Client, error) {
	u, err := neturl.Parse(options.BaseURL)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return nil, errors.New("base URL must be credential-free HTTP(S) without query or fragment")
	}
	if u.Hostname() == "" || strings.Contains(u.Hostname(), "%") {
		return nil, errors.New("base URL must use a valid host")
	}
	if (options.Token == "") == (options.TokenProvider == nil) {
		return nil, errors.New("exactly one of Token or TokenProvider is required")
	}
	if options.TenantID == "" || options.ApplicationID == "" {
		return nil, errors.New("tenant and application assertions are required")
	}
	timeout := options.Timeout
	if timeout == 0 {
		timeout = 10 * time.Second
	}
	if timeout <= 0 || timeout > 120*time.Second {
		return nil, errors.New("timeout is outside the bounded range")
	}
	hc := options.HTTPClient
	if hc == nil {
		hc = &http.Client{}
	} else {
		copy := *hc
		hc = &copy
	}
	hc.CheckRedirect = func(_ *http.Request, _ []*http.Request) error { return errors.New("redirects are not followed") }
	return &Client{baseURL: strings.TrimRight(options.BaseURL, "/"), token: options.Token, tokenProvider: options.TokenProvider, tenantID: options.TenantID, applicationID: options.ApplicationID, httpClient: hc, timeout: timeout}, nil
}

func (c *Client) request(ctx context.Context, operation, method, path string, body any, out any, authenticated bool) error {
	if !strings.HasPrefix(path, "/") || strings.Contains(path, "?") || strings.Contains(path, "#") {
		return errors.New("invalid sidecar path")
	}
	if body != nil {
		if err := validateEncodedJSON(body); err != nil {
			return err
		}
	}
	ctx, cancel := context.WithTimeout(ctx, c.timeout)
	defer cancel()
	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, reader)
	if err != nil {
		return &TransportError{Operation: operation, StateMayHaveChanged: method != http.MethodGet, ProviderEffectMayHaveHappened: method != http.MethodGet, Cause: err}
	}
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if authenticated {
		token, err := c.bearerToken(ctx)
		if err != nil {
			return err // No HTTP request was submitted; never retry automatically.
		}
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return &TransportError{Operation: operation, StateMayHaveChanged: method != http.MethodGet, ProviderEffectMayHaveHappened: method != http.MethodGet, Cause: err}
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, maxResponseBytes+1))
	if err != nil || len(raw) > maxResponseBytes {
		if err == nil {
			err = errors.New("response exceeds development limit")
		}
		return &TransportError{Operation: operation, StateMayHaveChanged: method != http.MethodGet, ProviderEffectMayHaveHappened: method != http.MethodGet, Cause: err}
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return decodeProtocolError(resp.StatusCode, raw)
	}
	if out == nil {
		return nil
	}
	if err := decodeJSON(raw, out); err != nil {
		return &ProtocolError{Code: "INVALID_RESPONSE", Message: "sidecar returned invalid JSON", HTTPStatus: resp.StatusCode}
	}
	if effect, ok := out.(*EffectReply); ok {
		return validateEffectReply(effect)
	}
	return nil
}

func (c *Client) bearerToken(ctx context.Context) (string, error) {
	if err := ctx.Err(); err != nil {
		return "", err
	}
	token := c.token
	if c.tokenProvider != nil {
		type result struct {
			token string
			err   error
		}
		ready := make(chan result, 1)
		go func() {
			token, err := c.tokenProvider(ctx)
			ready <- result{token, err}
		}()
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case value := <-ready:
			if err := ctx.Err(); err != nil {
				return "", err
			}
			if value.err != nil {
				return "", errors.New("sidecar token acquisition failed")
			}
			token = value.token
		}
	}
	if err := ctx.Err(); err != nil {
		return "", err
	}
	if token == "" {
		return "", errors.New("sidecar token is empty or invalid")
	}
	for _, ch := range token {
		if ch <= 32 || ch >= 127 {
			return "", errors.New("sidecar token is empty or invalid")
		}
	}
	return token, nil
}

func decodeJSON(raw []byte, out any) error {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if err := dec.Decode(out); err != nil {
		return err
	}
	var extra any
	if err := dec.Decode(&extra); err != io.EOF {
		return errors.New("trailing JSON data")
	}
	return nil
}
func decodeProtocolError(status int, raw []byte) error {
	var envelope map[string]any
	if decodeJSON(raw, &envelope) != nil {
		return &ProtocolError{Code: "INVALID_RESPONSE", Message: "sidecar returned an invalid error", HTTPStatus: status, StateMayHaveChanged: true, ProviderEffectMayHaveHappened: true}
	}
	e, ok := envelope["error"].(map[string]any)
	if !ok {
		return &ProtocolError{Code: "INVALID_RESPONSE", Message: "sidecar returned an invalid error", HTTPStatus: status, StateMayHaveChanged: true, ProviderEffectMayHaveHappened: true}
	}
	out := &ProtocolError{HTTPStatus: status, StateMayHaveChanged: true, ProviderEffectMayHaveHappened: true}
	if v, ok := e["code"].(string); ok {
		out.Code = v
	}
	if v, ok := e["message"].(string); ok {
		out.Message = v
	}
	if v, ok := e["effect_id"].(string); ok {
		out.EffectID = EffectID(v)
	}
	if v, ok := e["retryable"].(bool); ok && v {
		out.RetryClassification = "retryable"
	} else {
		out.RetryClassification = "caller_action_required"
	}
	if v, ok := e["state_may_have_changed"].(bool); ok {
		out.StateMayHaveChanged = v
	}
	if v, ok := e["effect_may_have_happened"].(bool); ok {
		out.ProviderEffectMayHaveHappened = v
	}
	out.Details = e["details"]
	if out.Code == "" {
		out.Code = "INVALID_RESPONSE"
	}
	if out.Message == "" {
		out.Message = "sidecar request failed"
	}
	return out
}

func validateGoValue(value any, field string) error {
	return validateReflect(reflect.ValueOf(value), field)
}

func validateReflect(value reflect.Value, field string) error {
	if !value.IsValid() {
		return nil
	}
	if value.Kind() == reflect.Interface || value.Kind() == reflect.Pointer {
		if value.IsNil() {
			return nil
		}
		return validateReflect(value.Elem(), field)
	}
	switch value.Kind() {
	case reflect.Float32, reflect.Float64:
		if field != "LeaseTTL" {
			return errors.New("raw floating-point values are not supported")
		}
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		n := value.Int()
		if n > (1<<53)-1 || n < -(1<<53)+1 {
			return errors.New("JSON integer is outside the safe range")
		}
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
		if value.Uint() > (1<<53)-1 {
			return errors.New("JSON integer is outside the safe range")
		}
	case reflect.Slice, reflect.Array:
		for i := 0; i < value.Len(); i++ {
			if err := validateReflect(value.Index(i), field); err != nil {
				return err
			}
		}
	case reflect.Map:
		for _, key := range value.MapKeys() {
			if err := validateReflect(value.MapIndex(key), field); err != nil {
				return err
			}
		}
	case reflect.Struct:
		for i := 0; i < value.NumField(); i++ {
			if value.Type().Field(i).PkgPath != "" {
				continue
			}
			if err := validateReflect(value.Field(i), value.Type().Field(i).Name); err != nil {
				return err
			}
		}
	}
	return nil
}

func validateEffectReply(effect *EffectReply) error {
	if effect.ProtocolVersion != ProtocolVersionV1Alpha1 || !validEffectID(effect.EffectID) {
		return &ProtocolError{Code: "INVALID_RESPONSE", Message: "sidecar returned an invalid effect identity", HTTPStatus: 200}
	}
	states := map[EffectState]bool{StateIntended: true, StateAttempting: true, StateCommitted: true, StateAborted: true, StateUnknown: true}
	if !states[effect.EffectState] {
		return &ProtocolError{Code: "UNSUPPORTED_PROTOCOL", Message: "sidecar returned an unsupported effect state", HTTPStatus: 200, EffectID: effect.EffectID}
	}
	if effect.OwnerID != nil && *effect.OwnerID == "" || effect.Fence != nil && *effect.Fence <= 0 {
		return &ProtocolError{Code: "INVALID_RESPONSE", Message: "sidecar returned invalid ownership data", HTTPStatus: 200, EffectID: effect.EffectID}
	}
	if effect.ProviderBoundary != nil && *effect.ProviderBoundary != BoundaryNotCrossed && *effect.ProviderBoundary != BoundaryMaybeCrossed && *effect.ProviderBoundary != BoundaryCrossed {
		return &ProtocolError{Code: "UNSUPPORTED_PROTOCOL", Message: "sidecar returned an unsupported boundary", HTTPStatus: 200, EffectID: effect.EffectID}
	}
	return nil
}

func validEffectID(id EffectID) bool {
	const prefix = "mycelium:effect:v1:"
	if !strings.HasPrefix(string(id), prefix) || len(id) != len(prefix)+64 {
		return false
	}
	for _, ch := range string(id)[len(prefix):] {
		if !strings.ContainsRune("0123456789abcdef", ch) {
			return false
		}
	}
	return true
}

func validateEncodedJSON(value any) error {
	if err := validateGoValue(value, ""); err != nil {
		return err
	}
	raw, err := json.Marshal(value)
	if err != nil {
		return err
	}
	var decoded any
	if err := decodeJSON(raw, &decoded); err != nil {
		return err
	}
	return validateJSON(decoded)
}
func validateJSON(value any) error {
	switch v := value.(type) {
	case json.Number:
		s := string(v)
		if strings.ContainsAny(s, ".eE") {
			return errors.New("raw floating-point and exponent numbers are not supported")
		}
		n, err := strconv.ParseInt(s, 10, 64)
		if err != nil || n > (1<<53)-1 || n < -(1<<53)+1 {
			return errors.New("JSON integer is outside the safe range")
		}
	case float64:
		return errors.New("raw floating-point values are not supported")
	case []any:
		for _, item := range v {
			if err := validateJSON(item); err != nil {
				return err
			}
		}
	case map[string]any:
		for _, item := range v {
			if err := validateJSON(item); err != nil {
				return err
			}
		}
	}
	return nil
}

func (c *Client) identity(in IdentityRequest) IdentityRequest {
	if in.TenantID == "" {
		in.TenantID = c.tenantID
	}
	if in.ApplicationID == "" {
		in.ApplicationID = c.applicationID
	}
	if in.IdentityVersion == "" {
		in.IdentityVersion = "1"
	}
	if in.CanonicalizationVersion == "" {
		in.CanonicalizationVersion = "jcs-1"
	}
	return in
}
func (c *Client) Health(ctx context.Context) (*HealthReply, error) {
	var out HealthReply
	err := c.request(ctx, "getHealth", http.MethodGet, "/health", nil, &out, false)
	if err == nil && (out.ProtocolVersion != ProtocolVersionV1Alpha1 || out.Status != "ok") {
		err = &ProtocolError{Code: "UNSUPPORTED_PROTOCOL", Message: "sidecar health response is incompatible", HTTPStatus: 200}
	}
	return &out, err
}
func (c *Client) Capabilities(ctx context.Context) (*CapabilitiesReply, error) {
	var out CapabilitiesReply
	err := c.request(ctx, "getCapabilities", http.MethodGet, "/v1/capabilities", nil, &out, true)
	if err == nil && (out.ProtocolVersion != ProtocolVersionV1Alpha1 || out.IdentityNamespace != "identity-v1" || !out.DevelopmentOnly) {
		err = &ProtocolError{Code: "UNSUPPORTED_PROTOCOL", Message: "sidecar capabilities are incompatible", HTTPStatus: 200}
	}
	return &out, err
}
func (c *Client) AssertCompatible(ctx context.Context) error {
	caps, err := c.Capabilities(ctx)
	if err != nil {
		return err
	}
	required := []string{"derive_identity", "claim_effect", "inspect_effect", "complete_effect"}
	if caps.ProtocolVersion != ProtocolVersionV1Alpha1 || caps.IdentityNamespace != "identity-v1" || !caps.DevelopmentOnly {
		return &ProtocolError{Code: "UNSUPPORTED_PROTOCOL", Message: "sidecar protocol is incompatible", HTTPStatus: 200}
	}
	for _, want := range required {
		found := false
		for _, got := range caps.Operations {
			if got == want {
				found = true
			}
		}
		if !found {
			return &ProtocolError{Code: "UNSUPPORTED_CAPABILITY", Message: "required sidecar operation is unavailable", HTTPStatus: 200}
		}
	}
	return nil
}

func (c *Client) AssertCompositeCompatible(ctx context.Context) error {
	if err := c.AssertCompatible(ctx); err != nil {
		return err
	}
	caps, err := c.Capabilities(ctx)
	if err != nil {
		return err
	}
	available := make(map[string]bool)
	for _, operation := range caps.Extensions["composite-v1"] {
		available[operation] = true
	}
	for _, required := range []string{
		"claim_composite", "claim_composite_step", "boundary_composite_step",
		"complete_composite_step", "resolve_composite_step", "finish_composite",
	} {
		if !available[required] {
			return &ProtocolError{Code: "UNSUPPORTED_CAPABILITY", Message: "composite-v1 extension is unavailable", HTTPStatus: 200}
		}
	}
	return nil
}
func (c *Client) DeriveEffectIdentity(ctx context.Context, in IdentityRequest) (*DeriveIdentityReply, error) {
	var out DeriveIdentityReply
	err := c.request(ctx, "deriveEffectIdentity", http.MethodPost, "/v1/identities/derive", c.identity(in), &out, true)
	if err == nil && (out.ProtocolVersion != ProtocolVersionV1Alpha1 || out.IdentityNamespace != "identity-v1" || !validEffectID(out.EffectID)) {
		err = &ProtocolError{Code: "UNSUPPORTED_PROTOCOL", Message: "sidecar identity response is incompatible", HTTPStatus: 200}
	}
	return &out, err
}
func (c *Client) ClaimEffect(ctx context.Context, in ClaimEffectRequest) (*ClaimReply, error) {
	in.IdentityRequest = c.identity(in.IdentityRequest)
	var raw map[string]any
	if err := c.request(ctx, "claimEffect", http.MethodPost, "/v1/effects/claim", in, &raw, true); err != nil {
		return nil, err
	}
	return decodeClaim(raw, in.IdentityRequest)
}
func (c *Client) GetEffect(ctx context.Context, id EffectID) (*EffectReply, error) {
	var out EffectReply
	err := c.request(ctx, "getEffect", http.MethodGet, "/v1/effects/"+neturl.PathEscape(string(id)), nil, &out, true)
	return &out, err
}
func requireIdentity(h *EffectHandle) (IdentityRequest, error) {
	if h == nil || h.identity == nil {
		return IdentityRequest{}, errors.New("effect handle has no identity binding")
	}
	return *h.identity, nil
}
func (c *Client) RenewEffectLease(ctx context.Context, h *EffectHandle, ttl *float64) (*EffectReply, error) {
	identity, err := requireIdentity(h)
	if err != nil {
		return nil, err
	}
	var out EffectReply
	err = c.request(ctx, "renewEffectLease", http.MethodPost, "/v1/effects/"+neturl.PathEscape(string(h.EffectID))+"/renew", RenewLeaseRequest{IdentityRequest: c.identity(identity), FencedRequest: FencedRequest{OwnerID: h.OwnerID, Fence: h.Fence}, LeaseTTL: ttl}, &out, true)
	return &out, err
}
func (c *Client) RecordEffectBoundary(ctx context.Context, h *EffectHandle, boundary BoundaryState) (*EffectReply, error) {
	identity, err := requireIdentity(h)
	if err != nil {
		return nil, err
	}
	var out EffectReply
	err = c.request(ctx, "recordEffectBoundary", http.MethodPost, "/v1/effects/"+neturl.PathEscape(string(h.EffectID))+"/boundary", RecordBoundaryRequest{IdentityRequest: c.identity(identity), FencedRequest: FencedRequest{OwnerID: h.OwnerID, Fence: h.Fence}, Boundary: boundary}, &out, true)
	return &out, err
}
func (c *Client) AttachProviderReference(ctx context.Context, h *EffectHandle, reference string) (*EffectReply, error) {
	identity, err := requireIdentity(h)
	if err != nil {
		return nil, err
	}
	var out EffectReply
	err = c.request(ctx, "attachProviderReference", http.MethodPost, "/v1/effects/"+neturl.PathEscape(string(h.EffectID))+"/provider-reference", AttachProviderReferenceRequest{IdentityRequest: c.identity(identity), FencedRequest: FencedRequest{OwnerID: h.OwnerID, Fence: h.Fence}, ProviderOperationRef: reference}, &out, true)
	return &out, err
}
func (c *Client) CompleteEffect(ctx context.Context, h *EffectHandle, result any) (*EffectReply, error) {
	identity, err := requireIdentity(h)
	if err != nil {
		return nil, err
	}
	var out EffectReply
	err = c.request(ctx, "completeEffect", http.MethodPost, "/v1/effects/"+neturl.PathEscape(string(h.EffectID))+"/complete", CompleteEffectRequest{IdentityRequest: c.identity(identity), FencedRequest: FencedRequest{OwnerID: h.OwnerID, Fence: h.Fence}, Result: result}, &out, true)
	return &out, err
}
func (c *Client) FailEffect(ctx context.Context, h *EffectHandle, boundary BoundaryState) (*EffectReply, error) {
	identity, err := requireIdentity(h)
	if err != nil {
		return nil, err
	}
	var out EffectReply
	err = c.request(ctx, "failEffect", http.MethodPost, "/v1/effects/"+neturl.PathEscape(string(h.EffectID))+"/fail", FailEffectRequest{IdentityRequest: c.identity(identity), FencedRequest: FencedRequest{OwnerID: h.OwnerID, Fence: h.Fence}, Boundary: boundary}, &out, true)
	return &out, err
}
func (c *Client) ReconcileEffect(ctx context.Context, id EffectID, in ReconcileEffectRequest) (*EffectReply, error) {
	var raw map[string]any
	in = c.identity(in)
	if err := c.request(ctx, "reconcileEffect", http.MethodPost, "/v1/effects/"+neturl.PathEscape(string(id))+"/reconcile", in, &raw, true); err != nil {
		return nil, err
	}
	var out EffectReply
	if err := mapTo(raw, &out); err != nil {
		return nil, err
	}
	if value, ok := raw["reconciliation"].(string); !ok || value != "authoritative-engine-result" {
		return nil, &ProtocolError{Code: "INVALID_RESPONSE", Message: "invalid reconciliation response", HTTPStatus: 200}
	}
	if err := validateEffectReply(&out); err != nil {
		return nil, err
	}
	return &out, nil
}

func validateCompositeReply(reply *CompositeReply) error {
	if reply.ProtocolVersion != ProtocolVersionV1Alpha1 || reply.CompositeProtocolVersion != "composite-v1" || reply.OperationID == "" ||
		len(reply.ManifestDigest) != 64 || (reply.Status != "RUNNING" && reply.Status != "COMPLETED") ||
		reply.Fence <= 0 || reply.NextStep < 0 || reply.Children == nil {
		return &ProtocolError{Code: "INVALID_RESPONSE", Message: "sidecar returned invalid composite state", HTTPStatus: 200}
	}
	return nil
}

func (c *Client) compositePath(handle *CompositeHandle) string {
	return "/extensions/composite-v1/composites/" + neturl.PathEscape(handle.OperationID)
}

func (c *Client) compositeBody(handle *CompositeHandle, extra map[string]any) map[string]any {
	body := map[string]any{
		"tenant_id": c.tenantID, "application_id": c.applicationID,
		"owner_id": handle.OwnerID, "fence": handle.Fence,
	}
	for key, value := range extra {
		body[key] = value
	}
	return body
}

func (c *Client) ClaimComposite(ctx context.Context, in ClaimCompositeRequest) (*CompositeReply, *CompositeHandle, error) {
	body := map[string]any{
		"tenant_id": c.tenantID, "application_id": c.applicationID,
		"operation_id": in.OperationID, "definition": in.Definition, "steps": in.Steps,
	}
	if in.LeaseTTL != nil {
		body["lease_ttl"] = *in.LeaseTTL
	}
	var out CompositeReply
	if err := c.request(ctx, "claimComposite", http.MethodPost, "/extensions/composite-v1/composites/claim", body, &out, true); err != nil {
		return nil, nil, err
	}
	if err := validateCompositeReply(&out); err != nil {
		return nil, nil, err
	}
	if out.OwnerID == nil || *out.OwnerID == "" {
		return nil, nil, &ProtocolError{Code: "INVALID_RESPONSE", Message: "composite claim lacks owner", HTTPStatus: 200}
	}
	return &out, &CompositeHandle{OperationID: out.OperationID, OwnerID: *out.OwnerID, Fence: out.Fence}, nil
}

func (c *Client) GetComposite(ctx context.Context, operationID string) (*CompositeReply, error) {
	var out CompositeReply
	err := c.request(ctx, "getComposite", http.MethodGet, "/extensions/composite-v1/composites/"+neturl.PathEscape(operationID), nil, &out, true)
	if err == nil {
		err = validateCompositeReply(&out)
	}
	return &out, err
}

func (c *Client) compositeCommand(ctx context.Context, handle *CompositeHandle, action string, extra map[string]any) (*CompositeReply, error) {
	var out CompositeReply
	err := c.request(ctx, action+"Composite", http.MethodPost, c.compositePath(handle)+"/"+action, c.compositeBody(handle, extra), &out, true)
	if err == nil {
		err = validateCompositeReply(&out)
	}
	return &out, err
}

func (c *Client) RenewComposite(ctx context.Context, handle *CompositeHandle, ttl *float64) (*CompositeReply, error) {
	extra := map[string]any{}
	if ttl != nil {
		extra["lease_ttl"] = *ttl
	}
	return c.compositeCommand(ctx, handle, "renew", extra)
}
func (c *Client) ReleaseComposite(ctx context.Context, handle *CompositeHandle) (*CompositeReply, error) {
	return c.compositeCommand(ctx, handle, "release", nil)
}
func (c *Client) FinishComposite(ctx context.Context, handle *CompositeHandle) (*CompositeReply, error) {
	return c.compositeCommand(ctx, handle, "finish", nil)
}

func (c *Client) compositeStepPath(handle *CompositeHandle, stepID string, action string) string {
	return c.compositePath(handle) + "/steps/" + neturl.PathEscape(stepID) + "/" + action
}

func (c *Client) ClaimCompositeStep(ctx context.Context, handle *CompositeHandle, stepID string, in ClaimEffectRequest) (*ClaimReply, error) {
	in.IdentityRequest = c.identity(in.IdentityRequest)
	extra := map[string]any{"identity": in.IdentityRequest}
	if in.Decision != nil {
		extra["decision"] = in.Decision
	}
	if in.LeaseTTL != nil {
		extra["lease_ttl"] = *in.LeaseTTL
	}
	var raw map[string]any
	if err := c.request(ctx, "claimCompositeStep", http.MethodPost, c.compositeStepPath(handle, stepID, "claim"), c.compositeBody(handle, extra), &raw, true); err != nil {
		return nil, err
	}
	return decodeClaim(raw, in.IdentityRequest)
}

func (c *Client) compositeEffectCommand(ctx context.Context, handle *CompositeHandle, stepID string, effect *EffectHandle, action string, extra map[string]any) (*EffectReply, error) {
	identity, err := requireIdentity(effect)
	if err != nil {
		return nil, err
	}
	extra["identity"] = c.identity(identity)
	extra["effect_owner_id"] = effect.OwnerID
	extra["effect_fence"] = effect.Fence
	var out EffectReply
	err = c.request(ctx, action+"CompositeStep", http.MethodPost, c.compositeStepPath(handle, stepID, action), c.compositeBody(handle, extra), &out, true)
	if err == nil {
		err = validateEffectReply(&out)
	}
	return &out, err
}

func (c *Client) BoundaryCompositeStep(ctx context.Context, handle *CompositeHandle, stepID string, effect *EffectHandle) (*EffectReply, error) {
	return c.compositeEffectCommand(ctx, handle, stepID, effect, "boundary", map[string]any{})
}
func (c *Client) CompleteCompositeStep(ctx context.Context, handle *CompositeHandle, stepID string, effect *EffectHandle, result any) (*EffectReply, error) {
	return c.compositeEffectCommand(ctx, handle, stepID, effect, "complete", map[string]any{"result": result})
}
func (c *Client) ResolveCompositeStep(ctx context.Context, handle *CompositeHandle, stepID string, identity IdentityRequest) (*CompositeReply, error) {
	var out CompositeReply
	err := c.request(ctx, "resolveCompositeStep", http.MethodPost, c.compositeStepPath(handle, stepID, "resolve"), c.compositeBody(handle, map[string]any{"identity": c.identity(identity)}), &out, true)
	if err == nil {
		err = validateCompositeReply(&out)
	}
	return &out, err
}
func mapTo(raw map[string]any, out any) error {
	b, err := json.Marshal(raw)
	if err != nil {
		return err
	}
	return decodeJSON(b, out)
}
func decodeClaim(raw map[string]any, identity IdentityRequest) (*ClaimReply, error) {
	var out EffectReply
	if err := mapTo(raw, &out); err != nil {
		return nil, &ProtocolError{Code: "INVALID_RESPONSE", Message: "invalid claim response", HTTPStatus: 200}
	}
	if err := validateEffectReply(&out); err != nil {
		return nil, err
	}
	d, ok := raw["disposition"].(string)
	if !ok {
		return nil, &ProtocolError{Code: "UNSUPPORTED_PROTOCOL", Message: "claim response has no disposition", HTTPStatus: 200}
	}
	known := map[string]bool{"EXECUTE": true, "RETURN_STORED_RESULT": true, "WAIT_FOR_OWNER": true, "RECORD_DECISION": true, "UNKNOWN": true, "DENIED": true, "TERMINAL_ABORTED": true}
	if !known[d] {
		return nil, &ProtocolError{Code: "UNSUPPORTED_PROTOCOL", Message: "claim disposition is unsupported", HTTPStatus: 200, EffectID: out.EffectID}
	}
	reply := &ClaimReply{EffectReply: out, Disposition: ClaimDisposition(d)}
	if d == string(ClaimExecute) {
		if out.OwnerID == nil || out.Fence == nil || *out.OwnerID == "" || *out.Fence <= 0 || out.Lease.LeasedUntil == nil && out.Lease.LastHeartbeatAt == nil {
			return nil, &ProtocolError{Code: "INVALID_RESPONSE", Message: "execution disposition lacks lease authority", HTTPStatus: 200, EffectID: out.EffectID}
		}
		reply.Handle = &EffectHandle{EffectID: out.EffectID, OwnerID: *out.OwnerID, Fence: *out.Fence, identity: &identity}
	}
	return reply, nil
}
