import os
import re
import sys
import time
import json
import html
import io
import zipfile
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from openai import OpenAI

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None

load_dotenv()

BASE_DIR = Path.home() / "local-ai-transcriber"
NOTION_API_KEY = os.getenv("NOTION_API_KEY")
NOTION_DATA_SOURCE_ID = os.getenv("NOTION_DATA_SOURCE_ID")
NOTION_VERSION = "2026-03-11"

AI_BASE_URL = (os.getenv("AI_BASE_URL") or "").rstrip("/")
AI_API_KEY = os.getenv("AI_API_KEY")
AI_MODEL = os.getenv("AI_MODEL") or "GPT-OSS-120b"

MAX_RETRIES = int(os.getenv("RECOVERY_MAX_RETRIES") or "3")
RETRY_COOLDOWN_HOURS = int(os.getenv("RECOVERY_COOLDOWN_HOURS") or "6")
LOOP_SECONDS = int(os.getenv("RECOVERY_LOOP_SECONDS") or "90")
COOKIE_BROWSER = os.getenv("COOKIE_BROWSER") or "chrome"

if not NOTION_API_KEY:
    raise RuntimeError("找不到 NOTION_API_KEY")
if not NOTION_DATA_SOURCE_ID:
    raise RuntimeError("找不到 NOTION_DATA_SOURCE_ID")
if not AI_BASE_URL or not AI_API_KEY:
    raise RuntimeError("找不到 AI_BASE_URL 或 AI_API_KEY")

HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}

ai_client = OpenAI(base_url=AI_BASE_URL, api_key=AI_API_KEY)

CATEGORIES = [
    "政策與政府計畫",
    "AI／科技工具",
    "產業案例與趨勢",
    "簡報與視覺素材",
    "工作方法／Prompt／範本",
    "個人生活與興趣",
    "暫存待判斷",
]
TAGS = ["AI", "政策", "工具", "產業", "簡報", "研究", "生活"]
APPLICATIONS = ["工作", "政策研究", "簡報", "學習", "生活", "娛樂", "待判斷"]
STATUSES = ["待看", "深入研究", "可應用"]

SOCIAL_DOMAINS = {
    "instagram.com",
    "www.instagram.com",
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "fb.watch",
    "tiktok.com",
    "www.tiktok.com",
    "vt.tiktok.com",
}

PLACEHOLDER_MARKERS = [
    "【待補內容】",
    "【未擷取】",
    "目前無法取得原始連結",
    "未取得正文",
    "未取得 Facebook",
    "未取得影片正文",
    "等待後續補抓",
    "內容尚未取得",
    "已先保存到知識庫",
]

NOISY_MARKERS = [
    "ytBootstrapConfig",
    "EXPERIMENT_FLAGS",
    "window.ytplayer",
    "ytcfg.set",
    "CLIENT_CANARY_STATE",
]


def now_iso():
    return datetime.now().astimezone().isoformat()


def notion_request(method, url, body=None, retries=3):
    last_error = None
    for attempt in range(retries):
        try:
            response = requests.request(
                method,
                url,
                headers=HEADERS,
                json=body,
                timeout=60,
            )
            if response.ok:
                return response.json() if response.text else {}
            last_error = RuntimeError(
                f"Notion API Error {response.status_code}: {response.text[:1000]}"
            )
            if response.status_code not in {429, 500, 502, 503, 504}:
                raise last_error
        except Exception as exc:
            last_error = exc
        if attempt < retries - 1:
            time.sleep(2 ** attempt)
    raise last_error or RuntimeError("Notion API request failed")


def fetch_page(page_id):
    return notion_request("GET", f"https://api.notion.com/v1/pages/{page_id}")


def update_page_properties(page_id, properties):
    return notion_request(
        "PATCH",
        f"https://api.notion.com/v1/pages/{page_id}",
        {"properties": properties},
    )


def get_text_property(prop):
    items = prop.get("rich_text") or prop.get("title") or []
    return "".join(item.get("plain_text", "") for item in items)


def get_select_property(prop):
    value = prop.get("select")
    return value.get("name") if value else None


def get_number_property(prop):
    value = prop.get("number")
    return value if isinstance(value, (int, float)) else None


def get_date_start(prop):
    value = prop.get("date") or {}
    return value.get("start")


def rich_text_prop(text, limit=1900):
    value = (text or "")[:limit]
    return {
        "rich_text": [
            {"type": "text", "text": {"content": value}}
        ] if value else []
    }


def rich_text_prop_long(text, limit=7000):
    value = (text or "")[:limit]
    return {
        "rich_text": [
            {
                "type": "text",
                "text": {"content": value[i:i + 1800]},
            }
            for i in range(0, len(value), 1800)
        ]
    }


def query_candidates():
    results = []
    cursor = None

    while True:
        body = {
            "page_size": 100,
            "filter": {
                "and": [
                    {"property": "AI處理完成", "checkbox": {"equals": False}},
                    {"property": "是否重複", "checkbox": {"equals": False}},
                    {"property": "內容類型", "select": {"does_not_equal": "影片"}},
                ]
            },
            "sorts": [
                {"property": "收藏日期", "direction": "descending"}
            ],
        }
        if cursor:
            body["start_cursor"] = cursor

        data = notion_request(
            "POST",
            f"https://api.notion.com/v1/data_sources/{NOTION_DATA_SOURCE_ID}/query",
            body,
        )
        results.extend(data.get("results", []))

        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if len(results) >= 500:
            break

    return results


def clean_text(text):
    if not text:
        return ""
    value = html.unescape(str(text))
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"<script[\s\S]*?</script>", " ", value, flags=re.I)
    value = re.sub(r"<style[\s\S]*?</style>", " ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = value.replace("\\n", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def is_noisy(text):
    sample = (text or "")[:6000]
    if any(marker in sample for marker in NOISY_MARKERS):
        return True
    symbol_count = sum(sample.count(ch) for ch in ["{", "}", "\\", '"'])
    return len(sample) > 1000 and symbol_count / max(1, len(sample)) > 0.08


def is_placeholder(text):
    value = (text or "").strip()
    if not value:
        return True
    if any(marker in value for marker in PLACEHOLDER_MARKERS):
        # 若除了提示字之外還有大量正文，仍可使用。
        return len(value) < 500
    return False


def meaningful(text, min_chars=80):
    value = clean_text(text)
    if len(value) < min_chars:
        return False
    if is_noisy(value) or is_placeholder(value):
        return False
    return True


def existing_material(page):
    props = page.get("properties", {})
    platform_original = get_text_property(props.get("平台原文", {}))
    snapshot = get_text_property(props.get("內容快照", {}))
    note = get_text_property(props.get("原始備註", {}))
    title = get_text_property(props.get("標題", {}))

    parts = []
    for label, text in [
        ("平台原文", platform_original),
        ("內容快照", snapshot),
    ]:
        cleaned = clean_text(text)
        if meaningful(cleaned):
            parts.append(f"【{label}】\n{cleaned[:7000]}")

    note_clean = clean_text(note)
    if note_clean and not re.fullmatch(r"https?://\S+", note_clean):
        parts.append(f"【使用者備註】\n{note_clean[:1500]}")

    if title:
        parts.append(f"【既有標題】\n{clean_text(title)[:500]}")

    material = "\n\n".join(parts).strip()
    return material if len(material) >= 100 else ""


def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def retry_eligible(page):
    props = page.get("properties", {})
    retries = int(get_number_property(props.get("AI重試次數", {})) or 0)
    if retries >= MAX_RETRIES:
        return False

    last = parse_iso(get_date_start(props.get("最後重試時間", {})))
    if not last:
        return True

    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)

    return datetime.now(timezone.utc) - last.astimezone(timezone.utc) >= timedelta(
        hours=RETRY_COOLDOWN_HOURS
    )


def select_next_job():
    candidates = query_candidates()

    # 1) 已有可用內容者優先，不需要重新抓取。
    for page in candidates:
        status = get_select_property(page.get("properties", {}).get("擷取狀態", {}))
        if status in {"成功", "部分"} and existing_material(page):
            return "ready", page

    # 2) 再處理需要重新補抓者；最多 3 次，失敗後有冷卻時間。
    for page in candidates:
        if retry_eligible(page):
            return "recover", page

    return None, None


def social_cookie_args(url):
    host = (urlparse(url).hostname or "").lower()
    if host in SOCIAL_DOMAINS or any(host.endswith("." + d) for d in SOCIAL_DOMAINS):
        return ["--cookies-from-browser", COOKIE_BROWSER]
    return []


def run_json_command(args, timeout=90):
    completed = subprocess.run(
        args,
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "command failed")[-1500:])
    return json.loads(completed.stdout)


def recover_with_ytdlp(url):
    args = ["yt-dlp", "--no-playlist", "--skip-download", "-J"]
    args += social_cookie_args(url)
    args.append(url)
    data = run_json_command(args)

    parts = []
    for label, key in [
        ("標題", "title"),
        ("作者／頻道", "uploader"),
        ("描述／Caption", "description"),
    ]:
        value = clean_text(data.get(key) or "")
        if value:
            parts.append(f"【{label}】\n{value[:6000]}")

    text = "\n\n".join(parts).strip()
    if len(text) < 80:
        raise RuntimeError("yt-dlp 有取得 Metadata，但內容不足以可靠整理")

    return text, "部分"


def first_meta(html_text, patterns):
    for pattern in patterns:
        match = re.search(pattern, html_text, flags=re.I | re.S)
        if match:
            return clean_text(match.group(1))
    return ""


def recover_web(url):
    response = requests.get(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/153.0 Safari/537.36"
            )
        },
        timeout=30,
        allow_redirects=True,
    )
    response.raise_for_status()

    content_type = (response.headers.get("content-type") or "").lower()
    if "text" not in content_type and "html" not in content_type and "json" not in content_type:
        raise RuntimeError(f"網址回傳非文字內容：{content_type or 'unknown'}")

    raw = response.text[:2_000_000]
    title = first_meta(raw, [r"<title[^>]*>([\s\S]*?)</title>"])
    og_title = first_meta(raw, [
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
    ])
    description = first_meta(raw, [
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']description["\']',
    ])

    body = clean_text(raw)
    if is_noisy(body):
        body = ""

    parts = []
    if og_title or title:
        parts.append(f"【網頁標題】\n{og_title or title}")
    if description:
        parts.append(f"【網頁描述】\n{description[:2500]}")
    if body and len(body) >= 120:
        parts.append(f"【網頁正文】\n{body[:10000]}")

    text = "\n\n".join(parts).strip()
    if len(text) < 100:
        raise RuntimeError("網頁可讀文字不足")

    return text, "成功" if len(body) >= 500 else "部分"


def get_attachment_url(page):
    files = page.get("properties", {}).get("附件", {}).get("files") or []
    if not files:
        return None, None

    item = files[0]
    name = item.get("name") or "attachment"
    if item.get("type") == "file":
        return name, (item.get("file") or {}).get("url")
    if item.get("type") == "external":
        return name, (item.get("external") or {}).get("url")
    return name, None


def extract_docx(data):
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        xml = zf.read("word/document.xml").decode("utf-8", errors="ignore")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    return clean_text(xml)


def recover_attachment(page):
    name, url = get_attachment_url(page)
    if not url:
        raise RuntimeError("此筆附件目前沒有可下載的 Notion 檔案網址")

    response = requests.get(url, timeout=60)
    response.raise_for_status()
    data = response.content
    ext = Path(name or "").suffix.lower()

    if ext in {".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml", ".html", ".htm"}:
        text = data.decode("utf-8", errors="ignore")
    elif ext == ".docx":
        text = extract_docx(data)
    elif ext == ".pdf":
        if PdfReader is None:
            raise RuntimeError("尚未安裝 pypdf，無法解析 PDF")
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:80])
    else:
        raise RuntimeError(f"附件格式目前尚未支援自動解析：{ext or name}")

    text = clean_text(text)
    if len(text) < 100:
        raise RuntimeError("附件文字內容不足")

    return f"【附件：{name}】\n{text[:12000]}", "成功"


def recover_content(page):
    props = page.get("properties", {})
    content_type = get_select_property(props.get("內容類型", {})) or "其他"
    source_platform = get_select_property(props.get("來源平台", {})) or "其他"
    url = (props.get("原始連結", {}) or {}).get("url") or ""

    if content_type in {"檔案", "PDF"} or source_platform == "檔案":
        try:
            return recover_attachment(page)
        except Exception as attachment_error:
            if not url:
                raise attachment_error

    errors = []

    if url and source_platform in {"Instagram", "Facebook", "TikTok"}:
        try:
            return recover_with_ytdlp(url)
        except Exception as exc:
            errors.append(f"yt-dlp: {exc}")

    if url:
        try:
            return recover_web(url)
        except Exception as exc:
            errors.append(f"web: {exc}")

    if url and source_platform not in {"Instagram", "Facebook", "TikTok"}:
        try:
            return recover_with_ytdlp(url)
        except Exception as exc:
            errors.append(f"yt-dlp fallback: {exc}")

    raise RuntimeError("；".join(errors) if errors else "沒有可重新擷取的網址或附件")


def safe_json(text):
    cleaned = re.sub(r"```(?:json)?|```", "", text or "", flags=re.I).strip()
    match = re.search(r"\{[\s\S]*\}", cleaned)
    return json.loads(match.group(0) if match else cleaned)


def normalize_ai_result(data, title_hint):
    category = data.get("category") if data.get("category") in CATEGORIES else "暫存待判斷"
    tags = [x for x in (data.get("tags") or []) if x in TAGS][:5]
    apps = [x for x in (data.get("applications") or []) if x in APPLICATIONS][:5]
    status = data.get("status") if data.get("status") in STATUSES else "待看"
    try:
        importance = max(1, min(5, int(data.get("importance", 3))))
    except Exception:
        importance = 3

    return {
        "title": str(data.get("title") or title_hint or "收藏內容")[:120],
        "summary": str(data.get("summary") or "")[:1800],
        "snapshot": str(data.get("snapshot") or "")[:1800],
        "why": str(data.get("why") or "")[:1000],
        "category": category,
        "tags": tags,
        "applications": apps or ["待判斷"],
        "status": status,
        "importance": importance,
        "related": str(data.get("related") or "")[:1000],
    }


def analyze_content(page, material):
    props = page.get("properties", {})
    title_hint = get_text_property(props.get("標題", {}))
    source_platform = get_select_property(props.get("來源平台", {})) or "其他"
    content_type = get_select_property(props.get("內容類型", {})) or "其他"
    original_note = get_text_property(props.get("原始備註", {}))
    original_url = (props.get("原始連結", {}) or {}).get("url") or ""

    system = """你是個人知識庫整理助理。使用繁體中文（台灣用語），只輸出 JSON。

請只根據提供的可驗證內容整理，不得猜測缺失資訊。使用者主動收藏即代表有保存意圖，不得建議刪除。

固定主分類只能擇一：政策與政府計畫、AI／科技工具、產業案例與趨勢、簡報與視覺素材、工作方法／Prompt／範本、個人生活與興趣、暫存待判斷。
標籤只能從：AI、政策、工具、產業、簡報、研究、生活。
應用情境只能從：工作、政策研究、簡報、學習、生活、娛樂、待判斷。
status 只能是：待看、深入研究、可應用。
importance 為 1-5。
snapshot 請用 3-6 點精煉重點，以換行分隔。

若內容仍不足以可靠判斷，category 使用「暫存待判斷」，不要虛構。
請輸出：{"title":"","summary":"","snapshot":"","why":"","category":"","tags":[],"applications":[],"status":"","importance":3,"related":""}"""

    user = f"""來源平台：{source_platform}
內容類型：{content_type}
原始標題：{title_hint or '（無）'}
原始備註：{original_note or '（無）'}
原始連結：{original_url or '（無）'}

【可驗證內容】
{material[:18000]}"""

    response = ai_client.chat.completions.create(
        model=AI_MODEL,
        temperature=0.1,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    content = response.choices[0].message.content or ""
    if not content:
        raise RuntimeError("WiRouter AI 回傳空內容")
    return normalize_ai_result(safe_json(content), title_hint)


def write_success(page, result, recovered_text=None, capture_status=None):
    props = {
        "標題": {
            "title": [
                {"type": "text", "text": {"content": result["title"][:120]}}
            ]
        },
        "AI 摘要": rich_text_prop(result["summary"]),
        "內容快照": rich_text_prop(result["snapshot"]),
        "為什麼值得留": rich_text_prop(result["why"]),
        "主分類": {"select": {"name": result["category"]}},
        "標籤": {"multi_select": [{"name": x} for x in result["tags"]]},
        "應用情境": {"multi_select": [{"name": x} for x in result["applications"]]},
        "狀態": {"select": {"name": result["status"]}},
        "重要度": {"number": result["importance"]},
        "可能關聯": rich_text_prop(result["related"]),
        "AI處理完成": {"checkbox": True},
        "AI最後錯誤": rich_text_prop(""),
    }

    if recovered_text:
        props["平台原文"] = rich_text_prop_long(recovered_text)
    if capture_status:
        props["擷取狀態"] = {"select": {"name": capture_status}}

    update_page_properties(page["id"], props)


def mark_attempt(page, error=None):
    props = page.get("properties", {})
    current = int(get_number_property(props.get("AI重試次數", {})) or 0)
    update = {
        "AI重試次數": {"number": current + 1},
        "最後重試時間": {"date": {"start": now_iso()}},
    }
    if error:
        update["AI最後錯誤"] = rich_text_prop(str(error)[:1800])
    update_page_properties(page["id"], update)


def process_ready(page):
    material = existing_material(page)
    if not material:
        raise RuntimeError("此筆看似已有內容，但實際沒有足夠可驗證文字")

    props = page.get("properties", {})
    print("\n==============================")
    print("非影片 AI 補整理")
    print("==============================")
    print("類型：", get_select_property(props.get("內容類型", {})))
    print("標題：", get_text_property(props.get("標題", {})))

    result = analyze_content(page, material)
    write_success(page, result)
    print("✅ AI 整理完成：", result["title"])


def process_recovery(page):
    props = page.get("properties", {})
    print("\n==============================")
    print("非影片 Recovery 補抓")
    print("==============================")
    print("類型：", get_select_property(props.get("內容類型", {})))
    print("來源：", get_select_property(props.get("來源平台", {})))
    print("標題：", get_text_property(props.get("標題", {})))
    print("URL：", (props.get("原始連結", {}) or {}).get("url"))

    try:
        recovered_text, capture_status = recover_content(page)
        if not meaningful(recovered_text, min_chars=80):
            raise RuntimeError("補抓後內容仍不足以可靠整理")

        result = analyze_content(page, recovered_text)
        write_success(
            page,
            result,
            recovered_text=recovered_text,
            capture_status=capture_status,
        )
        print("✅ Recovery + AI 完成：", result["title"])
    except Exception as exc:
        mark_attempt(page, error=exc)
        print("⚠️ Recovery 尚未成功：", exc)
        raise


def peek():
    mode, page = select_next_job()
    if not page:
        print("目前沒有符合條件的非影片 AI Recovery 項目。")
        return

    props = page.get("properties", {})
    print("找到非影片待處理項目 ✅")
    print("模式：", "直接 AI" if mode == "ready" else "補抓 + AI")
    print("內容類型：", get_select_property(props.get("內容類型", {})))
    print("來源平台：", get_select_property(props.get("來源平台", {})))
    print("標題：", get_text_property(props.get("標題", {})))
    print("URL：", (props.get("原始連結", {}) or {}).get("url"))
    print("AI重試次數：", int(get_number_property(props.get("AI重試次數", {})) or 0))
    print("這只是 Peek，尚未修改 Notion。")


def run_once():
    mode, page = select_next_job()
    if not page:
        print("目前沒有符合條件的非影片 AI Recovery 項目。")
        return
    if mode == "ready":
        process_ready(page)
    else:
        process_recovery(page)


def run_loop():
    print("Notion 全類型 Recovery Worker 已啟動（影片由原 Worker 處理）。")
    while True:
        try:
            mode, page = select_next_job()
            if page:
                if mode == "ready":
                    process_ready(page)
                else:
                    process_recovery(page)
            else:
                print(f"沒有非影片待處理工作，{LOOP_SECONDS} 秒後再檢查...")
                time.sleep(LOOP_SECONDS)
        except KeyboardInterrupt:
            print("Recovery Worker 已停止。")
            break
        except Exception as exc:
            print("Recovery Worker 本輪錯誤：", exc)
            time.sleep(LOOP_SECONDS)


def print_usage():
    print(
        "用法：\n"
        "python notion_recovery_worker.py --peek\n"
        "python notion_recovery_worker.py --once\n"
        "python notion_recovery_worker.py --loop"
    )


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) >= 2 else "--peek"
    if mode == "--peek":
        peek()
    elif mode == "--once":
        run_once()
    elif mode == "--loop":
        run_loop()
    else:
        print_usage()
