import { analyzeWithAI } from "./ai";
import { detectContentTypeFromMessage, detectPlatform, fetchWebSnapshot } from "./extract";
import { parseLineHistoryBuffer, type HistoricalKnowledgeItem } from "./history";
import { signInternalBody } from "./internal";
import { downloadLineContent, pushLine } from "./line";
import { createKnowledgePage, findExistingHistoryIds } from "./notion";
import type { CaptureStatus } from "./types";

export type HistoryJobPayload = {
  messageId: string;
  fileName: string;
  groupId: string;
  offset: number;
  created: number;
  skipped: number;
  failed: number;
};

const BATCH_SIZE = 9;
const CONCURRENCY = 3;

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function mapLimit<T, R>(items: T[], concurrency: number, fn: (item: T) => Promise<R>): Promise<R[]> {
  const results = new Array<R>(items.length);
  let cursor = 0;
  async function worker() {
    while (true) {
      const index = cursor++;
      if (index >= items.length) return;
      results[index] = await fn(items[index]);
    }
  }
  await Promise.all(Array.from({ length: Math.min(concurrency, items.length) }, () => worker()));
  return results;
}

async function importOne(item: HistoricalKnowledgeItem, fileName: string) {
  let lastError: unknown;
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      let pageTitle = "";
      let snapshot = item.rawText;
      let captureStatus: CaptureStatus = "成功";

      if (item.url) {
        const web = await fetchWebSnapshot(item.url);
        pageTitle = web.title;
        snapshot = [item.note || item.rawText, web.excerpt].filter(Boolean).join("\n\n").slice(0, 7600);
        captureStatus = web.status;
        if (captureStatus === "失敗") {
          snapshot = [
            `【待補內容】目前無法取得原始連結正文／字幕，但保留此筆收藏。`,
            `原始連結：${item.url}`,
            item.note ? `當時備註：${item.note}` : "",
            `收藏時間：${item.collectedAt}`,
          ].filter(Boolean).join("\n");
        }
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
        captureStatus,
        collectedAt: item.collectedAt,
      });

      await createKnowledgePage({
        title: ai.result.title || pageTitle || userText.slice(0, 80) || "LINE 歷史收藏",
        originalTitle: pageTitle,
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
      return true;
    } catch (err) {
      lastError = err;
      console.error("History item import failed", item.historyId, attempt, err);
      if (attempt < 3) await sleep(attempt * 1500);
    }
  }
  console.error("History item permanently failed", item.historyId, lastError);
  return false;
}

export async function triggerHistoryJob(origin: string, payload: HistoryJobPayload): Promise<void> {
  const raw = JSON.stringify(payload);
  const res = await fetch(`${origin}/api/history/process`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-internal-signature": signInternalBody(raw),
    },
    body: raw,
  });
  if (!res.ok) throw new Error(`History job trigger failed: ${res.status} ${(await res.text()).slice(0, 300)}`);
}

export async function processHistoryJob(origin: string, payload: HistoryJobPayload): Promise<void> {
  const max = Number(process.env.MAX_FILE_BYTES || 20 * 1024 * 1024);
  const dl = await downloadLineContent(payload.messageId);
  if (dl.buffer.byteLength > max) {
    await pushLine(payload.groupId, `LINE 歷史匯入失敗：檔案超過 ${Math.round(max / 1024 / 1024)} MB 上限。`);
    return;
  }

  const parsed = parseLineHistoryBuffer(dl.buffer);
  if (!parsed.items.length) {
    await pushLine(payload.groupId, [
      `LINE 歷史匯入：已讀取 ${parsed.messageCount} 則訊息，但沒有找到可整理的網址或文字筆記。`,
      parsed.ignoredAutomationCount ? `自動通知／翻譯鏡像略過：${parsed.ignoredAutomationCount} 則` : "",
    ].filter(Boolean).join("\n"));
    return;
  }

  const chunk = parsed.items.slice(payload.offset, payload.offset + BATCH_SIZE);
  if (!chunk.length) {
    await pushLine(payload.groupId, [
      "LINE 歷史整理全部完成 ✅",
      `檔案：${payload.fileName}`,
      `解析訊息：${parsed.messageCount} 則`,
      `自動通知／翻譯鏡像略過：${parsed.ignoredAutomationCount} 則`,
      `知識候選：${parsed.items.length} 筆`,
      `新增：${payload.created} 筆`,
      `已存在略過：${payload.skipped} 筆`,
      `失敗：${payload.failed} 筆`,
      "註：原始內容暫時抓不到的連結也會保留，並標記為待補內容。",
    ].join("\n"));
    return;
  }

  const existing = await findExistingHistoryIds(chunk.map((item) => item.historyId));
  const pending = chunk.filter((item) => !existing.has(item.historyId));
  const results = await mapLimit(pending, CONCURRENCY, (item) => importOne(item, payload.fileName));
  const createdNow = results.filter(Boolean).length;
  const failedNow = results.length - createdNow;
  const nextOffset = payload.offset + chunk.length;

  const next: HistoryJobPayload = {
    ...payload,
    offset: nextOffset,
    created: payload.created + createdNow,
    skipped: payload.skipped + existing.size,
    failed: payload.failed + failedNow,
  };

  if (nextOffset < parsed.items.length) {
    await triggerHistoryJob(origin, next);
    return;
  }

  await pushLine(payload.groupId, [
    "LINE 歷史整理全部完成 ✅",
    `檔案：${payload.fileName}`,
    `解析訊息：${parsed.messageCount} 則`,
    `自動通知／翻譯鏡像略過：${parsed.ignoredAutomationCount} 則`,
    `知識候選：${parsed.items.length} 筆`,
    `新增：${next.created} 筆`,
    `已存在略過：${next.skipped} 筆`,
    `失敗：${next.failed} 筆`,
    "原始內容暫時抓不到的連結也已保留，並標記為待補內容。",
    next.failed ? "若有真正寫入失敗的項目，稍後重新上傳同一份檔案即可；系統會略過已完成資料。" : "整份聊天紀錄已處理完成。",
  ].join("\n"));
}
