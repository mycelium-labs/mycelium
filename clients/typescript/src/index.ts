export { MyceliumClient, PROTOCOL_VERSION } from "./client.js";
export { JsonTransport } from "./transport.js";
export { MyceliumLocalValidationError, MyceliumProtocolError, MyceliumTransportError } from "./errors.js";
export type * from "./types.js";

import { MyceliumLocalValidationError } from "./errors.js";
import type { DecimalValue, UrlValue } from "./types.js";

export function decimal(value: string): DecimalValue {
  if (!/^-?(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value)) throw new MyceliumLocalValidationError("decimal is not in decimal-1 form");
  if (value === "-0" || (value.includes(".") && value.endsWith("0"))) throw new MyceliumLocalValidationError("decimal has noncanonical zero digits");
  const digits = value.replace(/[-.]/g, "");
  const scale = value.includes(".") ? value.length - value.indexOf(".") - 1 : 0;
  if (digits.length > 38 || scale > 18) throw new MyceliumLocalValidationError("decimal exceeds decimal-1 limits");
  return { $type: "decimal", profile: "decimal-1", value };
}

export function url(value: string): UrlValue {
  if (!value || /[\u0000-\u001f\u007f]/.test(value)) throw new MyceliumLocalValidationError("URL contains controls or is empty");
  const schemeEnd = value.indexOf(":");
  if (schemeEnd <= 0 || value.slice(0, schemeEnd) !== value.slice(0, schemeEnd).toLowerCase()) throw new MyceliumLocalValidationError("URL scheme must be lowercase");
  let parsed: URL;
  try { parsed = new URL(value); } catch { throw new MyceliumLocalValidationError("URL syntax is invalid"); }
  if ((parsed.protocol !== "http:" && parsed.protocol !== "https:") || !parsed.hostname || parsed.username || parsed.password || parsed.hash) throw new MyceliumLocalValidationError("URL is not in url-1 form");
  const authority = value.slice(schemeEnd + 3).split(/[/?#]/, 1)[0].split("@").pop() ?? "";
  const host = authority.startsWith("[") ? authority.slice(1).split("]", 1)[0] : authority.split(":", 1)[0];
  if (host !== host.toLowerCase()) throw new MyceliumLocalValidationError("URL host must be lowercase");
  return { $type: "url", profile: "url-1", value };
}
