import type { StoredItem } from "./types";

const NOTION_VERSION = "2026-03-11";
const DEFAULT_NOTION_DATA_SOURCE_ID = "344dad85-d683-494d-9eac-d4624159a0de";

function authHeaders(json = true): HeadersInit {
  const key = process.env.NOTION_API_KEY;
  if (!key) throw new Error("NOTION_API_KEY is missing");
  return {
    Authorization: `Bearer ${key}`,
    "Notion-Version": NOTION_VERSION,
    ...(json ? { "content-type": "application/json" } : {}),
  };
}

function dataSourceId(): string {
  return process.env.NOTION_DATA_SOURCE_ID || DEFAULT_NOTION_DATA_SOURCE_ID;
}

function richText(content: string) {
  return { rich_text: [{ type: "text", text: { content: content.slice(0, 1900) } }] };
}

export async function uploadFileToNotion(fileName: string, contentType: string, buffer: Buffer): Promise<string> {
  const create = await fetch("https://api.notion.com/v1/file_uploads", {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify({ mode: "single_part", filename: fileName.slice(0, 200), content_type: contentType }),
  });
  if (!create.ok) throw new Error(`Notion create file upload failed: ${create.status} ${(await create.text()).slice(0, 500)}`);
  const upload = await create.json();
  const form = new FormData();
  const bytes = Uint8Array.from(buffer);
  form.append("file", new Blob([bytes], { type: contentType }), fileName);
  const sent = await fetch(upload.upload_url || `https://api.notion.com/v1/file_uploads/${upload.id}/send`, {
    method: "POST",
    headers: authHeaders(false),
    body: form,
  });
  if (!sent.ok) throw new Error(`Notion send file failed: ${sent.status} ${(await sent.text()).slice(0, 500)}`);
  return upload.id;
}

export async function findExistingHistoryIds(ids: string[]): Promise<Set<string>> {
  const found = new Set<string>();
  for (let i = 0; i < ids.length; i += 20) {
    const chunk = ids.slice(i, i + 20);
    if (!chunk.length) continue;
    try {
      const res = await fetch(`https://api.notion.com/v1/data_sources/${dataSourceId()}/query`, {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({
          page_size: 100,
          filter: {
            or: chunk.map((id) => ({ property: "LINE訊息ID", rich_text: { equals: id } })),
          },
        }),
      });
      if (!res.ok) {
        console.error("Notion history dedupe query failed", res.status, (await res.text()).slice(0, 500));
        continue;
      }
      const data = await res.json();
      for (const page of data.results || []) {
        const rt = page?.properties?.["LINE訊息ID"]?.rich_text;
        const value = Array.isArray(rt) ? rt.map((x: any) => x?.plain_text || "").join("") : "";
        if (value) found.add(value);
      }
    } catch (err) {
      console.error("Notion history dedupe query error", err);
    }
  }
  return found;
}

export async function createKnowledgePage(item: StoredItem): Promise<{ id: string; url: string }> {
  const needsContentRecovery = item.captureStatus === "失敗" || item.captureStatus === "未擷取";
  const aiProcessed = !needsContentRecovery && (
    item.ai.category !== "暫存待判斷" || item.ai.summary !== "已先保存到知識庫，等待 AI 進一步整理。"
  );

  const props: Record<string, unknown> = {
    "標題": { title: [{ type: "text", text: { content: item.title.slice(0, 120) } }] },
    "來源平台": { select: { name: item.sourcePlatform } },
    "內容類型": { select: { name: item.contentType } },
    "主分類": { select: { name: item.ai.category } },
    "標籤": { multi_select: item.ai.tags.map((name) => ({ name })) },
    "AI 摘要": richText(item.ai.summary),
    "為什麼值得留": richText(item.ai.why),
    "應用情境": { multi_select: item.ai.applications.map((name) => ({ name })) },
    "狀態": { select: { name: item.ai.status } },
    "重要度": { number: item.ai.importance },
    "收藏日期": { date: { start: item.collectedAt || new Date().toISOString() } },
    "原始備註": richText(item.originalNote),
    "內容快照": richText(item.snapshot),
    "可能關聯": richText(item.ai.related),
    "是否重複": { checkbox: false },
    "AI處理完成": { checkbox: aiProcessed },
    "LINE訊息ID": richText(item.messageId),
    "擷取狀態": { select: { name: item.captureStatus } },
  };
  if (item.originalUrl) props["原始連結"] = { url: item.originalUrl };
  if (item.notionFileUploadId) {
    props["附件"] = { files: [{ type: "file_upload", file_upload: { id: item.notionFileUploadId } }] };
  }

  const res = await fetch("https://api.notion.com/v1/pages", {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify({ parent: { type: "data_source_id", data_source_id: dataSourceId() }, properties: props }),
  });
  if (!res.ok) throw new Error(`Notion create page failed: ${res.status} ${(await res.text()).slice(0, 1000)}`);
  const page = await res.json();
  return { id: page.id, url: page.url };
}
