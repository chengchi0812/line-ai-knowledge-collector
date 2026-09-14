import net from "node:net";
import type { CaptureStatus, ContentType, SourcePlatform } from "./types";

function trimUrl(raw: string): string {
  return raw.replace(/[.,;!?，。；！]+$/, "");
}

export function extractUrls(text: string): string[] {
  const explicitRe = /https?:\/\/[^\s<>"')\]]+/gi;
  const explicit = (text.match(explicitRe) ?? []).map(trimUrl);

  // Old LINE exports sometimes contain a useful domain without http(s), e.g. "tool.vercel.app".
  // Search only after removing explicit URLs so the hostname inside https://... is not captured twice.
  const remainder = text.replace(explicitRe, " ");
  const bareRe = /(?<![@\w])(?:[a-z0-9-]+\.)+(?:com|tw|org|net|app|ai|io|dev|gov|edu|me)(?:\/[^\s<>"')\]]*)?/gi;
  const bare = (remainder.match(bareRe) ?? []).map((u) => `https://${trimUrl(u)}`);

  return [...new Set([...explicit, ...bare])];
}

export function detectPlatform(url?: string): SourcePlatform {
  if (!url) return "LINE";
  try {
    const h = new URL(url).hostname.toLowerCase().replace(/^www\./, "");
    if (h === "instagram.com" || h.endsWith(".instagram.com")) return "Instagram";
    if (h === "facebook.com" || h === "fb.com" || h === "fb.watch" || h.endsWith(".facebook.com")) return "Facebook";
    if (h === "tiktok.com" || h.endsWith(".tiktok.com")) return "TikTok";
    if (h === "youtube.com" || h === "youtu.be" || h.endsWith(".youtube.com")) return "YouTube";
    return "Web";
  } catch {
    return "其他";
  }
}

export function detectContentTypeFromMessage(messageType: string, fileName?: string, url?: string): ContentType {
  if (messageType === "image") return "圖片";
  if (messageType === "video" || messageType === "audio") return "影片";
  if (messageType === "file") {
    if (fileName?.toLowerCase().endsWith(".pdf")) return "PDF";
    return "檔案";
  }
  if (url) {
    const p = detectPlatform(url);
    if (["Instagram", "Facebook", "TikTok", "YouTube"].includes(p)) return "影片";
    return "文章";
  }
  return "貼文";
}

function isBlockedIPv4(ip: string): boolean {
  const p = ip.split(".").map(Number);
  if (p.length !== 4) return true;
  const [a, b] = p;
  return a === 10 || a === 127 || a === 0 ||
    (a === 169 && b === 254) ||
    (a === 172 && b >= 16 && b <= 31) ||
    (a === 192 && b === 168) ||
    (a === 100 && b >= 64 && b <= 127) ||
    a >= 224;
}

export function isSafePublicUrl(raw: string): boolean {
  try {
    const u = new URL(raw);
    if (u.protocol !== "http:" && u.protocol !== "https:") return false;
    if (u.port && u.port !== "80" && u.port !== "443") return false;
    const h = u.hostname.toLowerCase();
    if (h === "localhost" || h.endsWith(".local") || h.endsWith(".internal")) return false;
    const ipType = net.isIP(h);
    if (ipType === 4 && isBlockedIPv4(h)) return false;
    if (ipType === 6 && (h === "::1" || h.startsWith("fc") || h.startsWith("fd") || h.startsWith("fe80"))) return false;
    return true;
  } catch {
    return false;
  }
}

function decodeEntities(s: string): string {
  return s
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;/gi, "'");
}

export async function fetchWebSnapshot(url: string): Promise<{ title: string; excerpt: string; status: CaptureStatus }> {
  if (!isSafePublicUrl(url)) {
    return { title: "", excerpt: "網址未自動擷取（安全性限制）；原始連結已保留", status: "未擷取" };
  }

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 8000);
  try {
    const res = await fetch(url, {
      redirect: "follow",
      signal: ctrl.signal,
      headers: { "user-agent": "Mozilla/5.0 (compatible; PersonalKnowledgeBot/1.0)" },
    });
    if (!res.ok) {
      return { title: "", excerpt: `網頁回應 ${res.status}；原始連結已保留待後續補抓`, status: "失敗" };
    }

    const ct = res.headers.get("content-type") || "";
    if (!ct.includes("text/html") && !ct.includes("text/plain")) {
      return { title: "", excerpt: `已取得連結，但內容類型 ${ct || "未知"} 目前未直接解析`, status: "部分" };
    }

    const html = (await res.text()).slice(0, 180_000);
    const title = decodeEntities((html.match(/<title[^>]*>([\s\S]*?)<\/title>/i)?.[1] || "").replace(/\s+/g, " ").trim());
    const description = decodeEntities(
      html.match(/<meta[^>]+(?:name|property)=["'](?:description|og:description)["'][^>]+content=["']([^"']*)["']/i)?.[1] ||
      html.match(/<meta[^>]+content=["']([^"']*)["'][^>]+(?:name|property)=["'](?:description|og:description)["']/i)?.[1] || ""
    );
    const plain = decodeEntities(html
      .replace(/<script[\s\S]*?<\/script>/gi, " ")
      .replace(/<style[\s\S]*?<\/style>/gi, " ")
      .replace(/<[^>]+>/g, " ")
      .replace(/\s+/g, " ")
      .trim());
    const excerpt = (description || plain).slice(0, 6500);

    if (!excerpt) {
      return { title, excerpt: "僅取得網址或頁面殼層，未取得可摘要文字；原始連結已保留", status: "失敗" };
    }
    return { title, excerpt, status: "成功" };
  } catch {
    return { title: "", excerpt: "網頁目前無法自動讀取；原始連結已保留待後續補抓", status: "失敗" };
  } finally {
    clearTimeout(timer);
  }
}
