import { analyzeWithAI } from "../../../../lib/ai";
import { detectContentTypeFromMessage, detectPlatform, extractUrls, fetchWebSnapshot } from "../../../../lib/extract";
import { parseLineHistoryBuffer, type HistoricalKnowledgeItem } from "../../../../lib/history";
import { downloadLineContent, replyLine, verifyLineSignature } from "../../../../lib/line";
import { createKnowledgePage, findExistingHistoryIds, uploadFileToNotion } from "../../../../lib/notion";
import type { CaptureStatus } from "../../../../lib/types";

export const runtime = "nodejs";
export const maxDuration = 60;

const HISTORY_IMPORT_LIMIT = 30;

type LineEvent = {
  type: string;
  replyToken?: string;
  source?: { type?: string; groupId?: string; userId?: string };
  message?: { id: string; type: string; text?: string; fileName?: string; fileSize?: number };
};

function extensionFor(contentType: string, messageType: string): string {
  const map: Record<string, string> = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
    "video/mp4": ".mp4", "audio/m4a": ".m4a", "audio/mp4": ".m4a", "audio/mpeg": ".mp3",
    "application/pdf": ".pdf",
  };
  return map[contentType] || (messageType === "image" ? ".jpg" : messageType === "video" ? ".mp4" : messageType === "audio" ? ".m4a" : ".bin");
}

async function mapLimit<T, R>(items: T[], concurrency: number, fn: (item: T, index: number) => Promise<R>): Promise<R[]> {
  const results = new Array<R>(items.length);
  let cursor = 0;
  async function worker() {
    while (true) {
      const index = cursor++;
      if (index >= items.length) return;
      results[index] = await fn(items[index], index);
    }
  }
  await Promise.all(Array.from({ length: Math.min(concurrency, items.length) }, () => worker()));
  return results;
}

async function chooseHistoryBatch(items: HistoricalKnowledgeItem[]) {
  const selected: HistoricalKnowledgeItem[] = [];
  let scanned = 0;
  let existingCount = 0;

  for (let i = 0; i < items.length && selected.length < HISTORY_IMPORT_LIMIT; i += 20) {
    const chunk = items.slice(i, i + 20);
    const existing = await findExistingHistoryIds(chunk.map((item) => item.historyId));
    scanned += chunk.length;
    existingCount += existing.size;
    for (const item of chunk) {
      if (!existing.has(item.historyId) && selected.length < HISTORY_IMPORT_LIMIT) selected.push(item);
    }
  }

  return { selected, scanned, existingCount };
}

async function importLineHistory(event: LineEvent, fileName: string) {
  if (!event.message) return;
  const max = Number(process.env.MAX_FILE_BYTES || 20 * 1024 * 1024);
  const dl = await downloadLineContent(event.message.id);
  if (dl.buffer.byteLength > max) {
    await replyLine(event.replyToken, `歷史匯入失敗：檔案超過 ${Math.round(max / 1024 / 1024)} MB 上限。`);
    return;
  }

  const parsed = parseLineHistoryBuffer(dl.buffer);
  if (!parsed.items.length) {
    await replyLine(event.replyToken, `歷史匯入：已讀取 ${parsed.messageCount} 則訊息，但沒有找到可整理的網址或文字筆記。請確認這是 LINE 匯出的 .txt 聊天紀錄。`);
    return;
  }

  const batch = await chooseHistoryBatch(parsed.items);
  if (!batch.selected.length) {
    const unscanned = Math.max(0, parsed.items.length - batch.scanned);
    await replyLine(event.replyToken, `歷史匯入檢查完成 ✅\n已掃描 ${batch.scanned} 筆，這些資料都已存在知識庫。${unscanned ? `\n尚有 ${unscanned} 筆未掃描，請再上傳同一份檔案以繼續。` : "\n這份檔案已全部處理完成。"}`);
    return;
  }

  const results = await mapLimit(batch.selected, 6, async (item) => {
    try {
      let pageTitle = "";
      let snapshot = item.rawText;
      let captureStatus: CaptureStatus = "成功";

      if (item.url) {
        const web = await fetchWebSnapshot(item.url);
        pageTitle = web.title;
        snapshot = [item.note || item.rawText, web.excerpt].filter(Boolean).join("\n\n").slice(0, 7600);
        captureStatus = web.status;
      }

      const sourcePlatform = item.url ? detectPlatform(item.url) : "LINE";
      const contentType = item.url ? detectContentTypeFromMessage("text", undefined, item.url) : "貼文";
      const userText = item.note || item.rawText;
      const ai = await analyzeWithAI({
        userText,
        url: item.url,
        pageTitle,
        snapshot,
        sourcePlatform,
        contentType,
      });

      await createKnowledgePage({
        title: ai.result.title || pageTitle || userText.slice(0, 80) || "LINE 歷史收藏",
        sourcePlatform,
        contentType,
        originalUrl: item.url,
        originalNote: `[LINE 歷史匯入｜${item.sender}] ${userText}`.slice(0, 1900),
        snapshot: `[歷史匯入檔案：${fileName}]\n${snapshot}`.slice(0, 7600),
        messageId: item.historyId,
        captureStatus,
        ai: ai.result,
        collectedAt: item.collectedAt,
      });
      return { ok: true as const };
    } catch (err) {
      console.error("Historical item import failed", item.historyId, err);
      return { ok: false as const };
    }
  });

  const created = results.filter((r) => r.ok).length;
  const failed = results.length - created;
  const unscanned = Math.max(0, parsed.items.length - batch.scanned);
  const lines = [
    "LINE 歷史整理完成 ✅",
    `檔案：${fileName}`,
    `解析訊息：${parsed.messageCount} 則`,
    `知識候選：${parsed.items.length} 筆`,
    `本次新增：${created} 筆`,
    `已存在略過：${batch.existingCount} 筆`,
    `失敗：${failed} 筆`,
  ];
  if (unscanned > 0 || batch.selected.length >= HISTORY_IMPORT_LIMIT) {
    lines.push("尚有資料待處理：請再次上傳同一份 .txt，系統會略過已匯入資料並接著處理下一批。 ");
  } else {
    lines.push("這份聊天紀錄已掃描完成。");
  }
  await replyLine(event.replyToken, lines.join("\n"));
}

async function processEvent(event: LineEvent) {
  if (event.type !== "message" || !event.message) return;
  if (event.source?.type !== "group") return; // 個人知識庫版本只收群組，避免陌生人私訊寫入

  const allowedGroup = process.env.LINE_ALLOWED_GROUP_ID;
  if (allowedGroup && event.source.groupId !== allowedGroup) return;

  const msg = event.message;
  const userText = msg.type === "text" ? (msg.text || "") : "";
  const urls = extractUrls(userText);
  const originalUrl = urls[0];
  const sourcePlatform = msg.type === "file" ? "檔案" : detectPlatform(originalUrl);
  const contentType = detectContentTypeFromMessage(msg.type, msg.fileName, originalUrl);

  if (msg.type === "file" && msg.fileName?.toLowerCase().endsWith(".txt")) {
    await importLineHistory(event, msg.fileName);
    return;
  }

  let pageTitle = "";
  let snapshot = userText;
  let captureStatus: CaptureStatus = "成功";
  let notionFileUploadId: string | undefined;
  let fileName: string | undefined = msg.fileName;

  if (originalUrl) {
    const web = await fetchWebSnapshot(originalUrl);
    pageTitle = web.title;
    snapshot = [userText, web.excerpt].filter(Boolean).join("\n\n").slice(0, 7600);
    captureStatus = web.status;
  }

  if (["file", "image", "video", "audio"].includes(msg.type)) {
    try {
      const max = Number(process.env.MAX_FILE_BYTES || 20 * 1024 * 1024);
      if (msg.fileSize && msg.fileSize > max) {
        captureStatus = "部分";
        snapshot = `附件超過自動備份上限（${Math.round(msg.fileSize / 1024 / 1024)} MB），已保留訊息資訊。`;
      } else {
        const dl = await downloadLineContent(msg.id);
        if (dl.buffer.byteLength > max) {
          captureStatus = "部分";
          snapshot = `附件實際大小超過自動備份上限（${Math.round(dl.buffer.byteLength / 1024 / 1024)} MB），未上傳至 Notion。`;
        } else {
          fileName ||= `${msg.type}-${msg.id}${extensionFor(dl.contentType, msg.type)}`;
          notionFileUploadId = await uploadFileToNotion(fileName, dl.contentType, dl.buffer);
          snapshot = `已於收到 LINE 訊息當下備份附件：${fileName}（${Math.round(dl.buffer.byteLength / 1024)} KB）`;
        }
      }
    } catch (err) {
      console.error("Attachment capture failed", err);
      captureStatus = "部分";
      snapshot = `附件自動備份失敗，但已保留 LINE 訊息 ID。檔名：${fileName || "未提供"}`;
    }
  }

  const ai = await analyzeWithAI({ userText, url: originalUrl, pageTitle, snapshot, sourcePlatform, contentType, fileName });
  const stored = await createKnowledgePage({
    title: ai.result.title || pageTitle || fileName || "LINE 收藏",
    sourcePlatform,
    contentType,
    originalUrl,
    originalNote: userText,
    snapshot,
    messageId: msg.id,
    captureStatus,
    ai: ai.result,
    notionFileUploadId,
  });

  await replyLine(event.replyToken, `已收進知識庫 ✅\n${ai.result.title}\n分類：${ai.result.category}｜重要度：${ai.result.importance}/5${ai.usedAI ? "" : "\n（AI 尚未設定，目前先保存待整理）"}`);
  return stored;
}

export async function POST(req: Request) {
  const raw = await req.text();
  const signature = req.headers.get("x-line-signature");
  if (!verifyLineSignature(raw, signature)) {
    return new Response("Invalid signature", { status: 401 });
  }

  let body: { events?: LineEvent[] };
  try { body = JSON.parse(raw); } catch { return new Response("Bad JSON", { status: 400 }); }

  // LINE Verify 可能送 events: []；正常回 200。
  const events = body.events || [];
  const results = await Promise.allSettled(events.map(processEvent));
  for (const r of results) if (r.status === "rejected") console.error("Webhook event failed", r.reason);

  return Response.json({ ok: true, received: events.length });
}
