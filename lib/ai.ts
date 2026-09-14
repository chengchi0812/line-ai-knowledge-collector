import type { AIResult, ContentType, SourcePlatform } from "./types";

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

function safeJson(text: string): unknown {
  const cleaned = text.replace(/```json|```/gi, "").trim();
  const m = cleaned.match(/\{[\s\S]*\}/);
  return JSON.parse(m?.[0] || cleaned);
}

function normalize(x: any, titleHint: string): AIResult {
  const categories = ["政策與政府計畫", "AI／科技工具", "產業案例與趨勢", "簡報與視覺素材", "工作方法／Prompt／範本", "個人生活與興趣", "暫存待判斷"];
  const tags = ["AI", "政策", "工具", "產業", "簡報", "研究", "生活"];
  const apps = ["工作", "政策研究", "簡報", "學習", "生活", "娛樂", "待判斷"];
  const statuses = ["待看", "深入研究", "可應用", "建議刪除"];
  const importance = Math.max(1, Math.min(5, Number(x.importance) || 2)) as 1|2|3|4|5;
  return {
    title: String(x.title || titleHint || fallback.title).slice(0, 120),
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
}): Promise<{ result: AIResult; usedAI: boolean }> {
  const base = process.env.AI_BASE_URL?.replace(/\/$/, "");
  const key = process.env.AI_API_KEY;
  const model = process.env.AI_MODEL;
  const titleHint = input.pageTitle || input.fileName || input.userText.slice(0, 80) || "待整理收藏";
  if (!base || !key || !model) return { result: { ...fallback, title: titleHint }, usedAI: false };

  const system = `你是個人知識庫整理助理。使用繁體中文（台灣用語），只輸出 JSON。\n
使用者會把 LINE 裡收藏的網址、文字、圖片或檔案存入知識庫。你要幫忙降噪，不要只是重述。\n
固定主分類只能擇一：政策與政府計畫、AI／科技工具、產業案例與趨勢、簡報與視覺素材、工作方法／Prompt／範本、個人生活與興趣、暫存待判斷。\n
標籤只能從：AI、政策、工具、產業、簡報、研究、生活。\n
應用情境只能從：工作、政策研究、簡報、學習、生活、娛樂、待判斷。\n
status 只能是：待看、深入研究、可應用、建議刪除。\n
importance 為 1-5。\n
請輸出：{"title":"","summary":"","why":"","category":"","tags":[],"applications":[],"status":"","importance":3,"related":""}`;

  const user = `來源平台：${input.sourcePlatform}\n內容類型：${input.contentType}\n原始文字：${input.userText || "（無）"}\n網址：${input.url || "（無）"}\n網頁標題：${input.pageTitle || "（無）"}\n檔名：${input.fileName || "（無）"}\n可取得內容：${input.snapshot.slice(0, 7000)}`;

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
