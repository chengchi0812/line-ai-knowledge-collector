import os
import re
import sys
import time
import json
import subprocess
from pathlib import Path
from datetime import datetime

import requests
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

BASE_DIR = Path.home() / "local-ai-transcriber"
JOBS_DIR = BASE_DIR / "jobs"
TRANSCRIBER = BASE_DIR / "transcribe_url.py"

NOTION_API_KEY = os.getenv("NOTION_API_KEY")
NOTION_DATA_SOURCE_ID = os.getenv("NOTION_DATA_SOURCE_ID")
NOTION_VERSION = "2026-03-11"

AI_BASE_URL = (os.getenv("AI_BASE_URL") or "").rstrip("/")
AI_API_KEY = os.getenv("AI_API_KEY")
AI_MODEL = os.getenv("AI_MODEL") or "GPT-OSS-120b"

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


def get_text_property(prop):
    items = prop.get("rich_text") or prop.get("title") or []
    return "".join(item.get("plain_text", "") for item in items)


def get_select_property(prop):
    value = prop.get("select")
    return value.get("name") if value else None


def get_checkbox_property(prop):
    return bool(prop.get("checkbox"))


def update_page_properties(page_id, properties):
    return notion_request(
        "PATCH",
        f"https://api.notion.com/v1/pages/{page_id}",
        {"properties": properties},
    )


def query_one(filters, sorts=None):
    body = {
        "page_size": 1,
        "filter": {"and": filters},
    }
    if sorts:
        body["sorts"] = sorts
    data = notion_request(
        "POST",
        f"https://api.notion.com/v1/data_sources/{NOTION_DATA_SOURCE_ID}/query",
        body,
    )
    results = data.get("results", [])
    return results[0] if results else None


def get_ai_pending_job():
    return query_one(
        [
            {"property": "逐字稿狀態", "select": {"equals": "完成"}},
            {"property": "AI處理完成", "checkbox": {"equals": False}},
            {"property": "是否重複", "checkbox": {"equals": False}},
            {"property": "內容類型", "select": {"equals": "影片"}},
        ],
        [{"property": "收藏日期", "direction": "ascending"}],
    )


def get_transcription_job():
    return query_one(
        [
            {"property": "逐字稿狀態", "select": {"equals": "待處理"}},
            {"property": "內容類型", "select": {"equals": "影片"}},
            {"property": "是否重複", "checkbox": {"equals": False}},
            {"property": "原始連結", "url": {"is_not_empty": True}},
        ],
        [{"property": "收藏日期", "direction": "ascending"}],
    )


def get_next_job():
    # 先處理「已有逐字稿、只差 AI」的項目，避免重跑 Whisper。
    job = get_ai_pending_job()
    if job:
        return "ai_only", job
    job = get_transcription_job()
    if job:
        return "transcribe", job
    return None, None


def block_plain_text(block):
    block_type = block.get("type")
    payload = block.get(block_type, {}) if block_type else {}
    rich_text = payload.get("rich_text") or []
    return "".join(x.get("plain_text", "") for x in rich_text)


def get_page_blocks(page_id):
    blocks = []
    cursor = None
    while True:
        url = f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100"
        if cursor:
            url += f"&start_cursor={cursor}"
        data = notion_request("GET", url)
        blocks.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return blocks


def read_transcript_from_page(page_id):
    blocks = get_page_blocks(page_id)
    lines = []
    started = False
    for block in blocks:
        text = block_plain_text(block).strip()
        if not text:
            continue
        if text == "影片逐字稿":
            started = True
            continue
        if started:
            if text.startswith("文字來源："):
                continue
            lines.append(text)
    return "\n".join(lines).strip()


def page_has_transcript(page_id):
    return bool(read_transcript_from_page(page_id))


def transcript_chunks(text, max_chars=1700):
    chunks = []
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = line
    if current:
        chunks.append(current)
    return chunks


def append_transcript_to_page(page_id, transcript, source_name):
    if page_has_transcript(page_id):
        print("Notion 頁面已有逐字稿，略過重複寫入。")
        return

    blocks = [
        {
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "rich_text": [{"type": "text", "text": {"content": "影片逐字稿"}}]
            },
        },
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {"type": "text", "text": {"content": f"文字來源：{source_name}"}}
                ]
            },
        },
    ]

    for chunk in transcript_chunks(transcript):
        blocks.append(
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": chunk}}]
                },
            }
        )

    for i in range(0, len(blocks), 80):
        notion_request(
            "PATCH",
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            {"children": blocks[i:i + 80]},
        )


def run_transcriber(video_url):
    before = {
        p.name for p in JOBS_DIR.iterdir() if p.is_dir()
    } if JOBS_DIR.exists() else set()

    result = subprocess.run(
        [sys.executable, str(TRANSCRIBER), video_url],
        cwd=str(BASE_DIR),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"transcribe_url.py 執行失敗，exit code={result.returncode}"
        )

    after_dirs = [
        p for p in JOBS_DIR.iterdir()
        if p.is_dir() and p.name not in before
    ]
    if not after_dirs:
        raise RuntimeError("找不到這次新產生的 job 資料夾")

    job_dir = max(after_dirs, key=lambda p: p.stat().st_mtime)
    transcript_file = job_dir / "transcript.txt"
    metadata_file = job_dir / "metadata.json"

    if not transcript_file.exists() or not metadata_file.exists():
        raise RuntimeError("找不到 transcript.txt 或 metadata.json")

    transcript = transcript_file.read_text(encoding="utf-8", errors="ignore")
    metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    return transcript, metadata, job_dir


def map_text_source(value):
    return {
        "manual_subtitle": "人工字幕",
        "automatic_subtitle": "平台自動字幕",
        "local_whisper": "Local Whisper",
    }.get(value, "Local Whisper")


def detect_language(transcript, metadata):
    language = (
        metadata.get("subtitle_language")
        or metadata.get("whisper_language")
        or metadata.get("original_language")
        or ""
    ).lower()
    chinese_count = len(re.findall(r"[\u4e00-\u9fff]", transcript))
    english_words = len(re.findall(r"\b[A-Za-z]{2,}\b", transcript))
    if chinese_count >= 30 and english_words >= 15:
        return "中英混合"
    if language.startswith("zh") or chinese_count >= 20:
        return "中文"
    if language.startswith("en") or (english_words >= 20 and chinese_count < 20):
        return "英文"
    return "其他"


def safe_json(text):
    cleaned = re.sub(r"```(?:json)?|```", "", text, flags=re.I).strip()
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
        "title": str(data.get("title") or title_hint or "影片收藏")[:120],
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


def summarize_chunk(chunk, index, total):
    response = ai_client.chat.completions.create(
        model=AI_MODEL,
        temperature=0.1,
        messages=[
            {
                "role": "system",
                "content": (
                    "你是影音逐字稿整理助理。使用繁體中文（台灣用語）。"
                    "只整理已提供內容，不猜測。保留重要人物、數字、主張、事件與可應用重點。"
                ),
            },
            {
                "role": "user",
                "content": f"這是長影片逐字稿第 {index}/{total} 段，請濃縮成 500-900 字重點：\n\n{chunk}",
            },
        ],
    )
    return response.choices[0].message.content or ""


def prepare_ai_material(transcript):
    # 長影片先分段摘要，避免把數小時逐字稿一次塞給模型。
    if len(transcript) <= 30000:
        return transcript

    chunk_size = 24000
    chunks = [transcript[i:i + chunk_size] for i in range(0, len(transcript), chunk_size)]
    summaries = []
    for i, chunk in enumerate(chunks, start=1):
        print(f"AI 長逐字稿分段整理 {i}/{len(chunks)}...")
        summaries.append(summarize_chunk(chunk, i, len(chunks)))
    return "\n\n".join(
        f"【第 {i} 段摘要】\n{text}"
        for i, text in enumerate(summaries, start=1)
    )


def analyze_transcript(page, transcript):
    props = page.get("properties", {})
    title_hint = get_text_property(props.get("標題", {}))
    source_platform = get_select_property(props.get("來源平台", {})) or "其他"
    original_note = get_text_property(props.get("原始備註", {}))
    original_url = (props.get("原始連結", {}) or {}).get("url") or ""

    material = prepare_ai_material(transcript)

    system = """你是個人影音知識庫整理助理。使用繁體中文（台灣用語），只輸出 JSON。
請根據影片逐字稿真正理解內容，不要只重述，也不要加入逐字稿未支持的事實。
使用者主動收藏即代表有保存意圖，不得建議刪除。
固定主分類只能擇一：政策與政府計畫、AI／科技工具、產業案例與趨勢、簡報與視覺素材、工作方法／Prompt／範本、個人生活與興趣、暫存待判斷。
標籤只能從：AI、政策、工具、產業、簡報、研究、生活。
應用情境只能從：工作、政策研究、簡報、學習、生活、娛樂、待判斷。
status 只能是：待看、深入研究、可應用。
importance 為 1-5。
snapshot 請用 3-6 點精煉內容重點，以換行分隔。
請輸出：{"title":"","summary":"","snapshot":"","why":"","category":"","tags":[],"applications":[],"status":"","importance":3,"related":""}"""

    user = f"""來源平台：{source_platform}
原始標題：{title_hint or '（無）'}
原始備註：{original_note or '（無）'}
原始連結：{original_url or '（無）'}

影片逐字稿／長影片分段摘要：
{material}"""

    response = ai_client.chat.completions.create(
        model=AI_MODEL,
        temperature=0.2,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("WiRouter AI 回傳空內容")
    return normalize_ai_result(safe_json(content), title_hint)


def rich_text_prop(text):
    return {
        "rich_text": [
            {"type": "text", "text": {"content": (text or "")[:1900]}}
        ]
    }


def write_ai_result(page_id, result):
    update_page_properties(
        page_id,
        {
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
        },
    )


def process_ai_only(page):
    page_id = page["id"]
    transcript = read_transcript_from_page(page_id)
    if not transcript:
        raise RuntimeError("逐字稿狀態為完成，但 Notion Page Body 找不到逐字稿")
    print("已有逐字稿，直接進行 WiRouter AI 整理，不重跑 Whisper。")
    result = analyze_transcript(page, transcript)
    write_ai_result(page_id, result)
    print(f"✅ AI 整理完成：{result['title']}")


def process_transcription(page):
    page_id = page["id"]
    props = page["properties"]
    title = get_text_property(props.get("標題", {}))
    video_url = (props.get("原始連結", {}) or {}).get("url")
    if not video_url:
        raise RuntimeError("Notion 項目沒有原始連結")

    print("\n==============================")
    print("開始處理 Notion 影片")
    print("==============================")
    print("標題：", title)
    print("URL：", video_url)

    update_page_properties(
        page_id,
        {"逐字稿狀態": {"select": {"name": "處理中"}}},
    )

    try:
        transcript, metadata, job_dir = run_transcriber(video_url)
        source_name = map_text_source(metadata.get("text_source"))
        language_name = detect_language(transcript, metadata)
        duration = metadata.get("duration_seconds")

        append_transcript_to_page(page_id, transcript, source_name)

        now = datetime.now().astimezone().isoformat()
        props_update = {
            "逐字稿狀態": {"select": {"name": "完成"}},
            "文字來源": {"select": {"name": source_name}},
            "逐字稿語言": {"select": {"name": language_name}},
            "轉錄時間": {"date": {"start": now}},
            "擷取狀態": {"select": {"name": "成功"}},
            "AI處理完成": {"checkbox": False},
        }
        if duration is not None:
            props_update["影片時長"] = {"number": float(duration)}
        update_page_properties(page_id, props_update)

        # 重新抓一次頁面資料不是必要；AI 只需要原始 properties + 新逐字稿。
        result = analyze_transcript(page, transcript)
        write_ai_result(page_id, result)

        print("\n✅ 逐字稿 + AI 整理全部完成")
        print("文字來源：", source_name)
        print("語言：", language_name)
        print("AI 標題：", result["title"])
        print("Job：", job_dir)

    except Exception as exc:
        print("\n❌ 處理失敗：", exc)
        # 如果逐字稿已經成功寫入並標成完成，AI 失敗不應把轉錄狀態改回失敗。
        try:
            refreshed = notion_request("GET", f"https://api.notion.com/v1/pages/{page_id}")
            current = get_select_property(refreshed.get("properties", {}).get("逐字稿狀態", {}))
        except Exception:
            current = None

        if current != "完成":
            error_text = str(exc).lower()
            download_markers = [
                "unable to extract", "unsupported url", "login required",
                "video unavailable", "http error", "download",
            ]
            status = "無法下載" if any(x in error_text for x in download_markers) else "失敗"
            update_page_properties(
                page_id,
                {"逐字稿狀態": {"select": {"name": status}}},
            )
        raise


def peek():
    mode, page = get_next_job()
    if not page:
        print("目前沒有待處理影片或待 AI 整理項目。")
        return
    props = page["properties"]
    print("找到待處理項目 ✅")
    print("模式：", "只做 AI" if mode == "ai_only" else "轉錄 + AI")
    print("標題：", get_text_property(props.get("標題", {})))
    print("來源平台：", get_select_property(props.get("來源平台", {})))
    print("URL：", (props.get("原始連結", {}) or {}).get("url"))
    print("這只是 Peek，尚未修改 Notion。")


def run_once():
    mode, page = get_next_job()
    if not page:
        print("目前沒有待處理影片或待 AI 整理項目。")
        return
    if mode == "ai_only":
        process_ai_only(page)
    else:
        process_transcription(page)


def run_loop():
    print("Local AI Worker 已啟動。")
    while True:
        try:
            mode, page = get_next_job()
            if page:
                if mode == "ai_only":
                    process_ai_only(page)
                else:
                    process_transcription(page)
            else:
                print("沒有待處理工作，60 秒後再檢查...")
                time.sleep(60)
        except KeyboardInterrupt:
            print("Worker 已停止。")
            break
        except Exception as exc:
            print("Worker 發生錯誤：", exc)
            time.sleep(60)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) >= 2 else "--peek"
    if mode == "--peek":
        peek()
    elif mode == "--once":
        run_once()
    elif mode == "--loop":
        run_loop()
    else:
        print(
            "用法：\n"
            "python notion_worker.py --peek\n"
            "python notion_worker.py --once\n"
            "python notion_worker.py --loop"
        )
