import { analyzeWithAI } from "../../../../lib/ai";
import { detectContentTypeFromMessage, detectPlatform, extractUrls, fetchWebSnapshot } from "../../../../lib/extract";
import { triggerHistoryJob } from "../../../../lib/history-job";
import { downloadLineContent, replyLine, verifyLineSignature } from "../../../../lib/line";
import { createKnowledgePage, uploadFileToNotion } from "../../../../lib/notion";
import type { CaptureStatus } from "../../../../lib/types";

export const runtime = "nodejs";
export const maxDuration = 60;

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

async function processEvent(event: LineEvent, origin: string) {
  if (event.type !== "message" || !event.message) return;
  if (event.source?.type !== "group") return;

  const allowedGroup = process.env.LINE_ALLOWED_GROUP_ID;
  if (allowedGroup && event.source.groupId !== allowedGroup) return;

  const msg = event.message;
  const userText = msg.type === "text" ? (msg.text || "") : "";
  const urls = extractUrls(userText);
  const originalUrl = urls[0];
  const sourcePlatform = msg.type === "file" ? "檔案" : detectPlatform(originalUrl);
  const contentType = detectContentTypeFromMessage(msg.type, msg.fileName, originalUrl);

  if (msg.type === "file" && msg.fileName?.toLowerCase().endsWith(".txt")) {
    if (!event.source.groupId) return;
    await replyLine(event.replyToken, `已收到 LINE 歷史聊天紀錄：${msg.fileName}\n系統會自動分批處理整份資料，你只需要上傳這一次。完成後會在本群組回報總結果。`);
    await triggerHistoryJob(origin, {
      messageId: msg.id,
      fileName: msg.fileName,
      groupId: event.source.groupId,
      offset: 0,
      created: 0,
      skipped: 0,
      failed: 0,
    });
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

  const origin = new URL(req.url).origin;
  const events = body.events || [];
  const results = await Promise.allSettled(events.map((event) => processEvent(event, origin)));
  for (const r of results) if (r.status === "rejected") console.error("Webhook event failed", r.reason);

  return Response.json({ ok: true, received: events.length });
}
