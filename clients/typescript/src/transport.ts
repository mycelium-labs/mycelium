import { MyceliumProtocolError, MyceliumTransportError } from "./errors.js";

export interface TransportOptions {
  baseUrl: string;
  token?: string;
  tokenProvider?: () => string | Promise<string>;
  timeoutMs?: number;
  fetch?: typeof globalThis.fetch;
}

const MAX_RESPONSE_BYTES = 1024 * 1024;

function validLoopbackBase(value: string): string {
  let url: URL;
  try { url = new URL(value); } catch { throw new Error("baseUrl must be a valid URL"); }
  if ((url.protocol !== "http:" && url.protocol !== "https:") || url.username || url.password || url.search || url.hash) {
    throw new Error("baseUrl must be credential-free HTTP(S) without query or fragment");
  }
  const host = url.hostname.replace(/^\[|\]$/g, "");
  if (!host || host.includes("/") || host.includes("%")) throw new Error("baseUrl must use a valid host");
  return url.toString().replace(/\/$/, "");
}

async function boundedText(response: Response): Promise<string> {
  if (!response.body) {
    const text = await response.text();
    if (new TextEncoder().encode(text).byteLength > MAX_RESPONSE_BYTES) throw new Error("response too large");
    return text;
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const part = await reader.read();
      if (part.done) break;
      size += part.value.byteLength;
      if (size > MAX_RESPONSE_BYTES) {
        await reader.cancel();
        throw new Error("response too large");
      }
      chunks.push(part.value);
    }
  } finally { reader.releaseLock(); }
  const merged = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { merged.set(chunk, offset); offset += chunk.byteLength; }
  return new TextDecoder("utf-8", { fatal: true }).decode(merged);
}

export class JsonTransport {
  readonly baseUrl: string;
  private readonly token?: string;
  private readonly tokenProvider?: () => string | Promise<string>;
  private readonly timeoutMs: number;
  private readonly fetcher: typeof globalThis.fetch;

  constructor(options: TransportOptions) {
    if (!options.token && !options.tokenProvider) throw new Error("token or tokenProvider is required");
    if (options.token === "") throw new Error("token must not be empty");
    this.baseUrl = validLoopbackBase(options.baseUrl);
    this.token = options.token;
    this.tokenProvider = options.tokenProvider;
    this.timeoutMs = options.timeoutMs ?? 10_000;
    if (!Number.isInteger(this.timeoutMs) || this.timeoutMs <= 0 || this.timeoutMs > 120_000) throw new Error("timeoutMs is outside the bounded range");
    this.fetcher = options.fetch ?? globalThis.fetch;
    if (!this.fetcher) throw new Error("a fetch implementation is required");
  }

  async request<T>(method: string, path: string, body?: unknown, authenticated = true): Promise<T> {
    if (!path.startsWith("/") || path.includes("?") || path.includes("#") || path.includes("//")) throw new Error("invalid sidecar path");
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const headers: Record<string, string> = { Accept: "application/json" };
      if (authenticated) {
        const token = this.tokenProvider ? await this.tokenProvider() : this.token;
        if (!token) throw new Error("token provider returned an empty token");
        headers.Authorization = `Bearer ${token}`;
      }
      if (body !== undefined) {
        headers["Content-Type"] = "application/json";
      }
      let response: Response;
      try {
        response = await this.fetcher(`${this.baseUrl}${path}`, {
          method, headers, body: body === undefined ? undefined : JSON.stringify(body),
          redirect: "error", signal: controller.signal,
        });
      } catch (error) { throw new MyceliumTransportError("sidecar request did not complete", error); }
      let text: string;
      try { text = await boundedText(response); } catch (error) { throw new MyceliumTransportError("sidecar response could not be read", error); }
      let payload: unknown;
      try { payload = JSON.parse(text); } catch { throw new MyceliumProtocolError("sidecar returned invalid JSON", { code: "INVALID_RESPONSE", httpStatus: response.status }); }
      if (!response.ok) {
        const error = payload && typeof payload === "object" && "error" in payload ? (payload as { error?: unknown }).error : undefined;
        if (!error || typeof error !== "object") throw new MyceliumProtocolError("sidecar returned an invalid error", { code: "INVALID_RESPONSE", httpStatus: response.status });
        const e = error as Record<string, unknown>;
        throw new MyceliumProtocolError(typeof e.message === "string" ? e.message : "sidecar request failed", {
          code: typeof e.code === "string" ? e.code : "INVALID_RESPONSE", httpStatus: response.status,
          effectId: typeof e.effect_id === "string" ? e.effect_id : undefined,
          retryClassification: e.retryable === true ? "retryable" : "caller_action_required",
          stateMayHaveChanged: e.state_may_have_changed !== false,
          providerEffectMayHaveHappened: e.effect_may_have_happened !== false,
          details: e.details,
        });
      }
      if (!payload || typeof payload !== "object") throw new MyceliumProtocolError("sidecar returned an invalid response", { code: "INVALID_RESPONSE", httpStatus: response.status });
      return payload as T;
    } finally { clearTimeout(timer); }
  }
}
