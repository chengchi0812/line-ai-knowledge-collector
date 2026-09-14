export const runtime = "nodejs";

export async function GET() {
  const missing = [
    !process.env.LINE_CHANNEL_SECRET ? "LINE_CHANNEL_SECRET" : null,
    !process.env.LINE_CHANNEL_ACCESS_TOKEN ? "LINE_CHANNEL_ACCESS_TOKEN" : null,
    !process.env.NOTION_API_KEY ? "NOTION_API_KEY" : null,
  ].filter(Boolean);

  return Response.json({
    ok: missing.length === 0,
    service: "line-ai-knowledge-collector",
    configured: {
      lineSecret: Boolean(process.env.LINE_CHANNEL_SECRET),
      lineToken: Boolean(process.env.LINE_CHANNEL_ACCESS_TOKEN),
      notion: Boolean(process.env.NOTION_API_KEY),
      ai: Boolean(process.env.AI_BASE_URL && process.env.AI_API_KEY && process.env.AI_MODEL),
    },
    missing,
    note: "NOTION_DATA_SOURCE_ID and LINE_REPLY_ENABLED now have built-in defaults.",
  });
}
