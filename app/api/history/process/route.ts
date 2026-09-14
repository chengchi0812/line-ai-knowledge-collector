import { waitUntil } from "@vercel/functions";
import { processHistoryJob, type HistoryJobPayload } from "../../../../lib/history-job";
import { verifyInternalBody } from "../../../../lib/internal";

export const runtime = "nodejs";
export const maxDuration = 60;

export async function POST(req: Request) {
  const raw = await req.text();
  const signature = req.headers.get("x-internal-signature");
  if (!verifyInternalBody(raw, signature)) {
    return new Response("Unauthorized", { status: 401 });
  }

  let payload: HistoryJobPayload;
  try {
    payload = JSON.parse(raw);
  } catch {
    return new Response("Bad JSON", { status: 400 });
  }

  const origin = new URL(req.url).origin;
  waitUntil(
    processHistoryJob(origin, payload).catch(async (err) => {
      console.error("History batch failed", err);
    }),
  );

  return Response.json({ ok: true, queued: true }, { status: 202 });
}
