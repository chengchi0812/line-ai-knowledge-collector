import type { AIResult, CaptureStatus, ContentType, SourcePlatform } from "./types";

const fallback: AIResult = {
  title: "待整理收藏",
  summary: "已先保存到知識庫，等待 AI 進一步整理。",
  why: "你主動收藏的內容，先保留供後續回顧。",
  category: "暫存待判斷",
  tags: [],
  applications: ["待判斷"],
  status: "待看",
  importance: 2,
  related: "",
};

function uncapturedFallback(input: {
  sourcePlatform: SourcePlatform;
  collectedAt?: string;
  pageTitle?: string;
  fileName?: string;
  userText: string;
}): AIResult {
  const date = input.collectedAt?.slice(0, 10);
  const source = input.sourcePlatform || "Web";
  const titleHint = input.pageTitle || input.fileName;
  return {
    title: titleHint || `【待補內容】${source} 收藏${date ? `｜${date}` : ""}`,
    summary: "目前無法從原始連結取得正文、字幕或可驗證內容；此筆為你主動轉傳收藏，已保留原始連結與收藏時間，等待後續補抓。",
    why: "主動轉傳本身代表收藏意圖；不因平台登入限制、反爬、短網址失效、安全性限制或暫時無法擷取而刪除。",
    category: "暫存待判斷",
    tags: [],
    applications: ["待判斷"],
    status: "待看",
    importance: 3,
    related: "待後續補抓；若日後取得原始內容，應更新此筆既有紀錄，而不是另建一筆。",
  };
}

function safeJson(text: string): unknown {
  const cleaned = text.replace(/```json|```/gi, "").trim();
  const m = cleaned.match(/\{[\s\S]*\}/);
  return JSON.parse(m?.[0] || cleaned);
}

function normalize(x: any, titleHint: string): AIResult {
  const categories = ["政策與政府計畫", "AI／科技工具", "產業案例與趨勢", "簡報與視覺素材", "工作方法／Prompt／範本", "個人生活與興趣", "暫存待判斷"];
  const tags = ["AI", "政策", "工具", "產業", "簡報", "研究", "生活"];
  const apps = ["工作", "政策研究", "簡報", "學習", "生活", "娛樂", "待判斷"];
  const statuses = ["待看", "深入研究", "可應用"];
  const importance = Math.max(1, Math.min(5, Number(x.importance) || 2)) as 1|2|3|4|5;
  return {
    title: String(x.title || titleHint || fallback.title).replace(/\s+/g, " ").trim().slice(0, 120),
    summary: String(x.summary || fallback.summary).slice(0, 1800),
    why: String(x.why || fallback.why).slice(0, 1000),
    category: categories.includes(x.category) ? x.category : "暫存待判斷",
    tags: Array.isArray(x.tags) ? [...new Set(x.tags.filter((v: string) => tags.includes(v)))].slice(0, 5) : [],
    applications: Array.isArray(x.applications) ? [...new Set(x.applications.filter((v: string) => apps.includes(v)))].slice(0, 5) : ["待判斷"],
    status: statuses.includes(x.status) ? x.status : "待看",
    importance,
    related: String(x.related || "").slice(0, 1000),
  } as AIResult;
}

export async function analyzeWithAI(input: {
  userText: string;
  url?: string;
  pageTitle?: string;
  snapshot: string;
  sourcePlatform: SourcePlatform;
  contentType: ContentType;
  fileName?: string;
  captureStatus?: CaptureStatus;
  collectedAt?: string;
}): Promise<{ result: AIResult; usedAI: boolean }> {
  if (input.url && (input.captureStatus === "失敗" || input.captureStatus === "未擷取")) {
    return { result: uncapturedFallback(input), usedAI: false };
  }

  const base = process.env.AI_BASE_URL?.replace(/\/$/, "");
  const key = process.env.AI_API_KEY;
  const model = process.env.AI_MODEL;
  const titleHint = input.pageTitle || input.fileName || input.userText.slice(0, 80) || "待整理收藏";
  if (!base || !key || !model) return { result: { ...fallback, title: titleHint }, usedAI: false };

  const system = `你是個人知識庫整理助理。使用繁體中文（台灣用語），只輸出 JSON。\n
使用者會把 LINE 裡收藏的網址、文字、圖片或檔案存入知識庫。你要幫忙降噪，不要只是重述。\n
重要原則：凡是使用者主動轉傳到這個收藏群組的內容，都視為具有收藏意圖。不得因內容價值暫時無法判斷、資訊不完整或看似不重要而建議刪除；不確定時請標記為「待看」或「暫存待判斷」。\n
標題規則：\n1. title 是「知識庫閱讀標題」，目的是讓使用者一眼知道內容在講什麼；原始平台標題會另外保存，不需要逐字照抄。\n2. 原始標題若已清楚具體，保留核心意思並精簡即可；若原始標題過長、只有 hashtag、聳動、模糊或只是收藏日期，才依正文／摘要重新命名。\n3. 優先使用「人物／品牌／公司 + 核心事件、觀點或方法」或「主題 + 具體結論／方法／爭議」結構。\n4. 中文標題以約 15-32 個中文字為主，資訊要具體、有辨識度；不要為了長度硬塞字。\n5. 避免空泛或重複句型，例如「影片分享」「內容整理」「重點摘要」「值得關注」「相關資訊」；也不要連續把不同內容都寫成「某某談……」。\n6. 人名、品牌、公司、數字與結論必須有原始標題、正文、Caption、檔案內容或使用者文字支持，不可自行補充。\n7. 若實際內容不足以判斷，保留清楚的原始標題；真的沒有可辨識內容時才用【待補內容】。\n
固定主分類只能擇一：政策與政府計畫、AI／科技工具、產業案例與趨勢、簡報與視覺素材、工作方法／Prompt／範本、個人生活與興趣、暫存待判斷。\n
標籤只能從：AI、政策、工具、產業、簡報、研究、生活。\n
應用情境只能從：工作、政策研究、簡報、學習、生活、娛樂、待判斷。\n
status 只能是：待看、深入研究、可應用。\n
importance 為 1-5。\n
請輸出：{"title":"","summary":"","why":"","category":"","tags":[],"applications":[],"status":"","importance":3,"related":""}`;

  const user = `來源平台：${input.sourcePlatform}\n內容類型：${input.contentType}\n原始文字：${input.userText || "（無）"}\n網址：${input.url || "（無）"}\n原始／平台標題：${input.pageTitle || "（無）"}\n檔名：${input.fileName || "（無）"}\n可取得內容：${input.snapshot.slice(0, 7000)}`;

  try {
    const res = await fetch(`${base}/chat/completions`, {
      method: "POST",
      headers: { Authorization: `Bearer ${key}`, "content-type": "application/json" },
      body: JSON.stringify({
        model,
        messages: [{ role: "system", content: system }, { role: "user", content: user }],
        temperature: 0.2,
      }),
    });
    if (!res.ok) throw new Error(`AI API ${res.status}: ${(await res.text()).slice(0, 500)}`);
    const data = await res.json();
    const content = data?.choices?.[0]?.message?.content;
    if (!content) throw new Error("AI returned empty content");
    return { result: normalize(safeJson(content), titleHint), usedAI: true };
  } catch (err) {
    console.error("AI analysis failed", err);
    return { result: { ...fallback, title: titleHint }, usedAI: false };
  }
}
