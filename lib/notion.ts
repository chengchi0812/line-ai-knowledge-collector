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

function richTextLong(content: string, limit = 7000) {
  const text = (content || "").slice(0, limit);
  const parts: Array<{ type: "text"; text: { content: string } }> = [];
  for (let i = 0; i < text.length; i += 1800) {
    parts.push({ type: "text", text: { content: text.slice(i, i + 1800) } });
  }
  return { rich_text: parts };
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

async function originalUrlAlreadyExists(originalUrl?: string): Promise<boolean> {
  if (!originalUrl) return false;
  try {
    const res = await fetch(`https://api.notion.com/v1/data_sources/${dataSourceId()}/query`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({
        page_size: 1,
        filter: { property: "原始連結", url: { equals: originalUrl } },
      }),
    });
    if (!res.ok) {
      console.error("Notion URL dedupe query failed", res.status, (await res.text()).slice(0, 500));
      return false;
    }
    const data = await res.json();
    return Array.isArray(data.results) && data.results.length > 0;
  } catch (err) {
    console.error("Notion URL dedupe query error", err);
    return false;
  }
}

export async function createKnowledgePage(item: StoredItem): Promise<{ id: string; url: string; isDuplicate: boolean }> {
  const isDuplicate = await originalUrlAlreadyExists(item.originalUrl);
  const needsContentRecovery = item.captureStatus === "失敗" || item.captureStatus === "未擷取";
  const videoNeedsTranscription = item.contentType === "影片" && Boolean(item.originalUrl) && !isDuplicate;
  const aiProcessed = isDuplicate || (!videoNeedsTranscription && !needsContentRecovery && (
    item.ai.category !== "暫存待判斷" || item.ai.summary !== "已先保存到知識庫，等待 AI 進一步整理。"
  ));

  const props: Record<string, unknown> = {
    "標題": { title: [{ type: "text", text: { content: item.title.slice(0, 120) } }] },
    "來源平台": { select: { name: item.sourcePlatform } },
    "內容類型": { select: { name: item.contentType } },
    "主分類": { select: { name: item.ai.category } },
    "標籤": { multi_select: item.ai.tags.map((name) => ({ name })) },
    "AI 摘要": richText(item.ai.summary),
    "為什麼值得留": richText(item.ai.why),
    "應用情境": { multi_select: item.ai.applications.map((name) => ({ name })) },
    "狀態": { select: { name: isDuplicate ? "封存" : item.ai.status } },
    "重要度": { number: item.ai.importance },
    "收藏日期": { date: { start: item.collectedAt || new Date().toISOString() } },
    "原始備註": richText(item.originalNote),
    "內容快照": richText(item.snapshot),
    "平台原文": richTextLong(item.snapshot),
    "可能關聯": richText(isDuplicate ? `與既有相同原始連結重複；保留本次轉傳紀錄。${item.ai.related ? ` ${item.ai.related}` : ""}` : item.ai.related),
    "是否重複": { checkbox: isDuplicate },
    "AI處理完成": { checkbox: aiProcessed },
    "LINE訊息ID": richText(item.messageId),
    "擷取狀態": { select: { name: item.captureStatus } },
  };

  if (item.originalTitle) props["原始標題"] = richText(item.originalTitle);

  if (videoNeedsTranscription) {
    props["逐字稿狀態"] = { select: { name: "待處理" } };
  }

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
  return { id: page.id, url: page.url, isDuplicate };
}
