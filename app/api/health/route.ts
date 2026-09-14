export const runtime = "nodejs";

export async function GET() {
  return Response.json({
    ok: true,
    service: "line-ai-knowledge-collector",
    configured: {
      lineSecret: Boolean(process.env.LINE_CHANNEL_SECRET),
      lineToken: Boolean(process.env.LINE_CHANNEL_ACCESS_TOKEN),
      notion: Boolean(process.env.NOTION_API_KEY && process.env.NOTION_DATA_SOURCE_ID),
      ai: Boolean(process.env.AI_BASE_URL && process.env.AI_API_KEY && process.env.AI_MODEL),
    },
  });
}
