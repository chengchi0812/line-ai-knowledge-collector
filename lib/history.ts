import crypto from "node:crypto";
import { extractUrls } from "./extract";

export type HistoricalKnowledgeItem = {
  historyId: string;
  url?: string;
  note: string;
  rawText: string;
  sender: string;
  collectedAt?: string;
};

type HistoryMessage = {
  date?: string;
  time?: string;
  sender: string;
  text: string;
};

function decodeHistory(buffer: Buffer): string {
  if (buffer.length >= 2 && buffer[0] === 0xff && buffer[1] === 0xfe) {
    return new TextDecoder("utf-16le").decode(buffer.subarray(2));
  }
  if (buffer.length >= 2 && buffer[0] === 0xfe && buffer[1] === 0xff) {
    return new TextDecoder("utf-16be").decode(buffer.subarray(2));
  }
  return new TextDecoder("utf-8").decode(buffer).replace(/^\uFEFF/, "");
}

function normalizeDateLine(line: string): string | undefined {
  const m = line.trim().match(/^(\d{4})[\/.\-](\d{1,2})[\/.\-](\d{1,2})(?:\([^)]*\)|（[^）]*）)?\s*$/);
  if (!m) return undefined;
  const [, y, mo, d] = m;
  return `${y}-${mo.padStart(2, "0")}-${d.padStart(2, "0")}`;
}

function normalizeClock(raw: string): string | undefined {
  const s = raw.trim().replace(/\s+/g, " ");
  const m = s.match(/^(?:(上午|下午|AM|PM|am|pm)\s*)?(\d{1,2}):(\d{2})(?:\s*(上午|下午|AM|PM|am|pm))?$/);
  if (!m) return undefined;
  let hour = Number(m[2]);
  const minute = Number(m[3]);
  const ap = (m[1] || m[4] || "").toLowerCase();
  if (minute > 59 || hour > 23) return undefined;
  if (ap === "下午" || ap === "pm") {
    if (hour < 12) hour += 12;
  } else if (ap === "上午" || ap === "am") {
    if (hour === 12) hour = 0;
  }
  return `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
}

function parseMessages(text: string): HistoryMessage[] {
  const lines = text.replace(/\u0000/g, "").replace(/\r\n?/g, "\n").split("\n");
  const messages: HistoryMessage[] = [];
  let currentDate: string | undefined;

  for (const rawLine of lines) {
    const line = rawLine.replace(/\uFEFF/g, "");
    const dateOnly = normalizeDateLine(line);
    if (dateOnly) {
      currentDate = dateOnly;
      continue;
    }

    const parts = line.split("\t");
    if (parts.length >= 3) {
      let date = currentDate;
      let time = normalizeClock(parts[0]);
      const senderIndex = 1;

      const full = parts[0].trim().match(/^(\d{4}[\/.\-]\d{1,2}[\/.\-]\d{1,2})\s+(.+)$/);
      if (!time && full) {
        date = normalizeDateLine(full[1]);
        time = normalizeClock(full[2]);
      }

      if (time) {
        const sender = (parts[senderIndex] || "").trim();
        const msgText = parts.slice(senderIndex + 1).join("\t").trim();
        if (sender && msgText) messages.push({ date, time, sender, text: msgText });
        continue;
      }
    }

    const fullLine = line.match(/^(\d{4}[\/.\-]\d{1,2}[\/.\-]\d{1,2})\s+([^\t]+)\t([^\t]+)\t([\s\S]+)$/);
    if (fullLine) {
      const date = normalizeDateLine(fullLine[1]);
      const time = normalizeClock(fullLine[2]);
      if (date && time) messages.push({ date, time, sender: fullLine[3].trim(), text: fullLine[4].trim() });
      continue;
    }

    if (messages.length && line.trim() && !/^Time\s+Name\s+Text$/i.test(line.trim())) {
      messages[messages.length - 1].text += `\n${line.trim()}`;
    }
  }
  return messages;
}

function minuteOfDay(time?: string): number | undefined {
  if (!time) return undefined;
  const [h, m] = time.split(":").map(Number);
  return h * 60 + m;
}

function near(a: HistoryMessage, b: HistoryMessage): boolean {
  if (!a.date || !b.date || a.date !== b.date || a.sender !== b.sender) return false;
  const x = minuteOfDay(a.time);
  const y = minuteOfDay(b.time);
  return x !== undefined && y !== undefined && Math.abs(x - y) <= 10;
}

function redactSensitiveText(text: string): string {
  const credentialWord = /(密碼|密码|password|passwd|\bpwd\b|api[_\s-]?key|access[_\s-]?token|auth(?:orization)?[_\s-]?token|client[_\s-]?secret|channel[_\s-]?secret|bearer\s+token|token\s*[:=]|secret\s*[:=])/i;
  const accountHeading = /(管理員帳號|管理员账号|帳號|账号|login|username|user\s*name)/i;
  const financialContext = /(轉帳|转账|匯款|汇款|銀行|银行|帳戶|账户|信用卡|卡號|卡号|account\s*(?:number|no\.?))/i;
  const identityContext = /(身分證|身份證|身份证|passport|護照)/i;
  const highEntropy = /(?:\b\d{6,}:[A-Za-z0-9_-]{20,}\b|\b[A-Fa-f0-9]{32,}\b|\b[A-Za-z0-9_-]{40,}\b)/g;

  return text.split("\n").map((originalLine) => {
    let line = originalLine;

    if (credentialWord.test(line)) {
      const label = line.match(/^\s*([^:=：]{1,40})\s*[:=：]/)?.[1]?.trim();
      return label ? `${label}：[已隱去敏感憑證]` : "[已隱去敏感憑證]";
    }

    if (/\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b\s*\/\s*\S+/i.test(line)) {
      line = line.replace(/(\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b)\s*\/\s*\S+/gi, "$1 / [已隱去密碼]");
    }

    if (accountHeading.test(line) && /[:：]\s*\S+/.test(line)) {
      line = line.replace(/([:：])\s*\S+.*/, "$1 [已隱去帳號／憑證內容]");
    }

    if (financialContext.test(line)) {
      line = line
        .replace(/(帳號末\s*\d*\s*碼(?:為|是|[:：])?\s*)\d+/gi, "$1[已隱去]")
        .replace(/(轉帳|转账|匯款|汇款)([^\n]{0,20}?)(\d[\d,]*(?:\.\d+)?)\s*(元|TWD|NTD)/gi, "$1$2[已隱去金額]$4")
        .replace(/\b\d{12,19}\b/g, "[已隱去金融帳號]");
    }

    if (identityContext.test(line)) {
      line = line.replace(/\b[A-Z][12]\d{8}\b/gi, "[已隱去身分識別碼]");
    }

    line = line.replace(highEntropy, "[已隱去可能的敏感憑證]");
    return line;
  }).join("\n");
}

function cleanNote(text: string, urls: string[]): string {
  let out = redactSensitiveText(text);
  for (const url of urls) out = out.replaceAll(url, " ");
  return out.replace(/\s+/g, " ").trim();
}

function isNoise(text: string): boolean {
  const s = text.trim();
  if (!s) return true;
  if (/^(\[?(?:貼圖|圖片|照片|影片|語音訊息|檔案|Sticker|Photo|Image|Video|File)\]?|已收回訊息。?)$/i.test(s)) return true;
  if (/^(加入群組|離開群組|邀請.+加入群組)/.test(s)) return true;
  return false;
}

function toCollectedAt(msg: HistoryMessage): string | undefined {
  if (!msg.date) return undefined;
  const time = msg.time || "12:00";
  return `${msg.date}T${time}:00+08:00`;
}

function sanitizeUrl(raw: string): string {
  try {
    const u = new URL(raw);
    const secretKeys = /(^|_)(token|secret|password|passwd|pwd|api[_-]?key|access[_-]?token|auth|authorization|key)($|_)/i;
    for (const key of [...u.searchParams.keys()]) {
      if (secretKeys.test(key)) u.searchParams.set(key, "REDACTED");
    }
    return u.toString();
  } catch {
    return raw.trim();
  }
}

function canonicalUrl(raw: string): string {
  try {
    const u = new URL(sanitizeUrl(raw));
    u.hash = "";
    for (const key of [...u.searchParams.keys()]) {
      const k = key.toLowerCase();
      if (k.startsWith("utm_") || ["fbclid", "gclid", "igshid", "mc_cid", "mc_eid"].includes(k)) u.searchParams.delete(key);
    }
    return u.toString().replace(/\/$/, "");
  } catch {
    return raw.trim();
  }
}

function stableId(kind: string, value: string): string {
  return `history:${kind}:${crypto.createHash("sha1").update(value).digest("hex").slice(0, 20)}`;
}

export function parseLineHistoryBuffer(buffer: Buffer): { items: HistoricalKnowledgeItem[]; messageCount: number } {
  const messages = parseMessages(decodeHistory(buffer));
  const usedAsNote = new Set<number>();
  const urlMap = new Map<string, HistoricalKnowledgeItem>();

  for (let i = 0; i < messages.length; i++) {
    const msg = messages[i];
    const urls = extractUrls(msg.text);
    if (!urls.length) continue;

    const noteParts: string[] = [];
    const own = cleanNote(msg.text, urls);
    if (own && !isNoise(own)) noteParts.push(own);

    const prev = messages[i - 1];
    if (prev && !extractUrls(prev.text).length && !isNoise(prev.text) && near(prev, msg)) {
      noteParts.unshift(redactSensitiveText(prev.text.trim()));
      usedAsNote.add(i - 1);
    }
    const next = messages[i + 1];
    if (next && !extractUrls(next.text).length && !isNoise(next.text) && near(msg, next)) {
      noteParts.push(redactSensitiveText(next.text.trim()));
      usedAsNote.add(i + 1);
    }

    const note = [...new Set(noteParts)].join(" / ").slice(0, 1500);
    for (const originalUrl of urls) {
      const url = sanitizeUrl(originalUrl);
      const key = canonicalUrl(url);
      const existing = urlMap.get(key);
      if (existing) {
        if (note && !existing.note.includes(note)) existing.note = `${existing.note}${existing.note ? " / " : ""}${note}`.slice(0, 1500);
        continue;
      }
      urlMap.set(key, {
        historyId: stableId("url", key),
        url,
        note,
        rawText: redactSensitiveText(msg.text).replaceAll(originalUrl, url),
        sender: msg.sender,
        collectedAt: toCollectedAt(msg),
      });
    }
  }

  const items = [...urlMap.values()];

  // Preserve meaningful standalone notes, but redact credentials and sensitive identifiers first.
  for (let i = 0; i < messages.length; i++) {
    if (usedAsNote.has(i)) continue;
    const msg = messages[i];
    if (extractUrls(msg.text).length || isNoise(msg.text)) continue;
    const clean = redactSensitiveText(msg.text).replace(/\s+/g, " ").trim();
    if (clean.length < 8) continue;
    const seed = `${msg.date || ""}|${msg.time || ""}|${msg.sender}|${clean}`;
    items.push({
      historyId: stableId("text", seed),
      note: clean.slice(0, 1500),
      rawText: clean,
      sender: msg.sender,
      collectedAt: toCollectedAt(msg),
    });
  }

  items.sort((a, b) => (a.collectedAt || "").localeCompare(b.collectedAt || ""));
  return { items, messageCount: messages.length };
}
