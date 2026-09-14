import crypto from "node:crypto";

function secret(): string {
  const value = process.env.LINE_CHANNEL_SECRET;
  if (!value) throw new Error("LINE_CHANNEL_SECRET is missing");
  return value;
}

export function signInternalBody(rawBody: string): string {
  return crypto.createHmac("sha256", secret()).update(rawBody).digest("hex");
}

export function verifyInternalBody(rawBody: string, signature: string | null): boolean {
  if (!signature) return false;
  const expected = signInternalBody(rawBody);
  const a = Buffer.from(expected);
  const b = Buffer.from(signature);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
