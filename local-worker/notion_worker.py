import os
import re
import sys
import time
import json
import html
import shutil
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta

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

VIDEO_MAX_RETRIES = int(os.getenv("VIDEO_MAX_RETRIES") or "3")
VIDEO_RETRY_COOLDOWN_HOURS = int(
    os.getenv("VIDEO_RETRY_COOLDOWN_HOURS") or "6"
)

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


def get_number_property(prop):
    value = prop.get("number")
    return value if isinstance(value, (int, float)) else None


def get_date_start(prop):
    value = prop.get("date") or {}
    return value.get("start")


def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def update_page_properties(page_id, properties):
    return notion_request(
        "PATCH",
        f"https://api.notion.com/v1/pages/{page_id}",
        {"properties": properties},
    )


def fetch_page(page_id):
    return notion_request("GET", f"https://api.notion.com/v1/pages/{page_id}")


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


def query_many(filters, sorts=None, page_size=100):
    body = {
        "page_size": page_size,
        "filter": {"and": filters},
    }
    if sorts:
        body["sorts"] = sorts
    data = notion_request(
        "POST",
        f"https://api.notion.com/v1/data_sources/{NOTION_DATA_SOURCE_ID}/query",
        body,
    )
    return data.get("results", [])


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
    candidates = query_many(
        [
            {
                "or": [
                    {"property": "逐字稿狀態", "select": {"equals": "待處理"}},
                    {"property": "逐字稿狀態", "select": {"equals": "失敗"}},
                    {"property": "逐字稿狀態", "select": {"equals": "無法下載"}},
                ]
            },
            {"property": "內容類型", "select": {"equals": "影片"}},
            {"property": "是否重複", "checkbox": {"equals": False}},
            {"property": "原始連結", "url": {"is_not_empty": True}},
        ],
        [{"property": "收藏日期", "direction": "ascending"}],
    )

    now = datetime.now(timezone.utc)
    for page in candidates:
        props = page.get("properties", {})
        status = get_select_property(props.get("逐字稿狀態", {}))
        retries = int(get_number_property(props.get("AI重試次數", {})) or 0)

        if status == "待處理":
            return page
        if retries >= VIDEO_MAX_RETRIES:
            continue

        last = parse_iso(get_date_start(props.get("最後重試時間", {})))
        if last is None:
            return page
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if now - last.astimezone(timezone.utc) >= timedelta(
            hours=VIDEO_RETRY_COOLDOWN_HOURS
        ):
            return page

    return None


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
            block_type = block.get("type") or ""
            if block_type in {"heading_1", "heading_2"}:
                break
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
                "rich_text": [
                    {"type": "text", "text": {"content": "影片逐字稿"}}
                ]
            },
        },
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": f"文字來源：{source_name}"},
                    }
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
                    "rich_text": [
                        {"type": "text", "text": {"content": chunk}}
                    ]
                },
            }
        )

    for i in range(0, len(blocks), 80):
        notion_request(
            "PATCH",
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            {"children": blocks[i:i + 80]},
        )


def replace_transcript_section(page_id, transcript, source_name):
    """
    只替換「影片逐字稿」區段，不碰頁面其他內容。
    用於 --reprocess 或 AI 校正既有逐字稿。
    """
    blocks = get_page_blocks(page_id)
    start = None
    end = None

    for i, block in enumerate(blocks):
        if block_plain_text(block).strip() == "影片逐字稿":
            start = i
            break

    if start is not None:
        end = len(blocks)
        for i in range(start + 1, len(blocks)):
            if blocks[i].get("type") in {"heading_1", "heading_2"}:
                end = i
                break

        for block in blocks[start:end]:
            notion_request(
                "DELETE",
                f"https://api.notion.com/v1/blocks/{block['id']}",
            )

    append_transcript_to_page(page_id, transcript, source_name)


def run_transcriber(video_url):
    before = (
        {p.name for p in JOBS_DIR.iterdir() if p.is_dir()}
        if JOBS_DIR.exists()
        else set()
    )

    result = subprocess.run(
        [sys.executable, str(TRANSCRIBER), video_url],
        cwd=str(BASE_DIR),
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"transcribe_url.py 執行失敗，exit code={result.returncode}"
        )

    after_dirs = [
        p
        for p in JOBS_DIR.iterdir()
        if p.is_dir() and p.name not in before
    ]

    if not after_dirs:
        raise RuntimeError("找不到這次新產生的 job 資料夾")

    job_dir = max(after_dirs, key=lambda p: p.stat().st_mtime)
    transcript_file = job_dir / "transcript.txt"
    metadata_file = job_dir / "metadata.json"

    if not transcript_file.exists() or not metadata_file.exists():
        raise RuntimeError("找不到 transcript.txt 或 metadata.json")

    transcript = transcript_file.read_text(
        encoding="utf-8",
        errors="ignore",
    )

    metadata = json.loads(
        metadata_file.read_text(encoding="utf-8")
    )

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

    chinese_count = len(
        re.findall(r"[\u4e00-\u9fff]", transcript)
    )
    english_words = len(
        re.findall(r"\b[A-Za-z]{2,}\b", transcript)
    )

    if chinese_count >= 30 and english_words >= 15:
        return "中英混合"

    if language.startswith("zh") or chinese_count >= 20:
        return "中文"

    if language.startswith("en") or (
        english_words >= 20 and chinese_count < 20
    ):
        return "英文"

    return "其他"


def safe_json(text):
    cleaned = re.sub(
        r"```(?:json)?|```",
        "",
        text,
        flags=re.I,
    ).strip()

    match = re.search(
        r"\{[\s\S]*\}",
        cleaned,
    )

    return json.loads(
        match.group(0) if match else cleaned
    )


def clean_reference_text(text):
    if not text:
        return ""

    value = html.unescape(str(text))
    value = re.sub(
        r"<br\s*/?>",
        "\n",
        value,
        flags=re.I,
    )
    value = re.sub(
        r"<[^>]+>",
        " ",
        value,
    )
    value = value.replace("\\n", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def noisy_snapshot(text):
    sample = (text or "")[:5000]
    markers = [
        "ytBootstrapConfig",
        "EXPERIMENT_FLAGS",
        "window.ytplayer",
        "ytcfg.set",
        "function(",
        "CLIENT_CANARY_STATE",
    ]

    if any(marker in sample for marker in markers):
        return True

    # 大量程式符號通常代表抓到頁面殼層，而不是可用正文。
    symbol_count = sum(
        sample.count(ch)
        for ch in ["{", "}", "\\", "\""]
    )

    return (
        len(sample) > 1000
        and symbol_count / max(1, len(sample)) > 0.08
    )


def build_reference_context(page, metadata=None):
    """
    建立「可驗證參考文字」：
    - yt-dlp 取得的影片標題 / uploader
    - Notion 原始標題
    - LINE 原始備註
    - 平台 caption / description（若雲端已抓到）
    垃圾 HTML / YouTube JS 殼層會排除。
    """
    props = page.get("properties", {})

    current_title = get_text_property(
        props.get("標題", {})
    )
    original_note = get_text_property(
        props.get("原始備註", {})
    )
    snapshot = get_text_property(
        props.get("內容快照", {})
    )
    platform_original = get_text_property(
        props.get("平台原文", {})
    )

    parts = []

    if metadata:
        meta_title = clean_reference_text(
            metadata.get("title") or ""
        )
        uploader = clean_reference_text(
            metadata.get("uploader") or ""
        )

        if meta_title:
            parts.append(
                f"平台 Metadata 標題：{meta_title}"
            )
        if uploader:
            parts.append(
                f"平台作者／頻道：{uploader}"
            )

    if current_title:
        parts.append(
            f"Notion 既有標題：{clean_reference_text(current_title)}"
        )

    note = clean_reference_text(original_note)
    if note and not re.fullmatch(r"https?://\S+", note):
        parts.append(
            f"使用者原始備註：{note[:2000]}"
        )

    # 優先使用專門保存的「平台原文」；舊資料沒有時才退回內容快照。
    source_text = platform_original or snapshot
    source_clean = clean_reference_text(source_text)
    if (
        source_clean
        and not noisy_snapshot(source_clean)
    ):
        parts.append(
            f"平台原始文字／Caption：{source_clean[:5000]}"
        )

    return "\n\n".join(parts)[:8000]


def split_transcript_for_correction(
    transcript,
    max_chars=12000,
):
    chunks = []
    current = []

    current_len = 0

    for line in transcript.splitlines():
        add_len = len(line) + 1

        if current and current_len + add_len > max_chars:
            chunks.append("\n".join(current))
            current = []
            current_len = 0

        current.append(line)
        current_len += add_len

    if current:
        chunks.append("\n".join(current))

    return chunks


def correct_transcript_chunk(
    chunk,
    reference_context,
    index,
    total,
):
    system = """你是 ASR 逐字稿校正器。使用繁體中文（台灣用語）。

任務只限「校正明顯的語音辨識錯字與專有名詞」，不是改寫文章。

規則：
1. 每一行的時間戳（例如 [00:12]）必須原封不動保留。
2. 不可刪除句子、不可增加原影片未說的內容、不可摘要。
3. 句子順序不可改。
4. 一般口語、贅字、語法即使不漂亮也盡量保留。
5. 品牌、產品、工具、模型、人名、公司、地名、英文技術詞若 ASR 拼錯，可依「可驗證參考文字」校正。
6. 若平台原始 Caption / Metadata 明確寫出專有名詞，該拼法優先於 ASR 音譯。
7. 參考文字只能用來校正片中實際說到的詞，不能把參考文字中沒說出口的資訊硬加進逐字稿。
8. 不確定就維持原文。
9. 只輸出校正後逐字稿，不要說明修改原因，不要 Markdown code fence。"""

    user = f"""可驗證參考文字：
{reference_context or '（沒有額外參考文字）'}

這是逐字稿第 {index}/{total} 段：
{chunk}"""

    response = ai_client.chat.completions.create(
        model=AI_MODEL,
        temperature=0,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )

    corrected = (
        response.choices[0].message.content
        or ""
    ).strip()

    if not corrected:
        return chunk

    # 基本防呆：校正後仍應保有大部分原時間戳。
    original_times = re.findall(
        r"\[\d{1,2}:\d{2}(?::\d{2})?\]",
        chunk,
    )
    corrected_times = re.findall(
        r"\[\d{1,2}:\d{2}(?::\d{2})?\]",
        corrected,
    )

    if (
        original_times
        and len(corrected_times)
        < max(1, int(len(original_times) * 0.85))
    ):
        print(
            "⚠️ 專有名詞校正結果破壞時間戳，"
            "本段保留原始逐字稿。"
        )
        return chunk

    return corrected


def correct_transcript(
    transcript,
    reference_context,
):
    """
    利用平台 caption / metadata 修正 ASR 專有名詞。
    沒有可信參考文字時，直接保留原逐字稿。
    """
    if not reference_context.strip():
        print(
            "沒有可用的平台參考文字，"
            "略過專有名詞校正。"
        )
        return transcript

    chunks = split_transcript_for_correction(
        transcript
    )

    corrected_parts = []

    for i, chunk in enumerate(
        chunks,
        start=1,
    ):
        print(
            f"AI 專有名詞校正 {i}/{len(chunks)}..."
        )
        corrected_parts.append(
            correct_transcript_chunk(
                chunk,
                reference_context,
                i,
                len(chunks),
            )
        )

    return "\n".join(corrected_parts).strip()


def normalize_ai_result(data, title_hint):
    category = (
        data.get("category")
        if data.get("category") in CATEGORIES
        else "暫存待判斷"
    )

    tags = [
        x
        for x in (data.get("tags") or [])
        if x in TAGS
    ][:5]

    apps = [
        x
        for x in (data.get("applications") or [])
        if x in APPLICATIONS
    ][:5]

    status = (
        data.get("status")
        if data.get("status") in STATUSES
        else "待看"
    )

    try:
        importance = max(
            1,
            min(
                5,
                int(
                    data.get(
                        "importance",
                        3,
                    )
                ),
            ),
        )
    except Exception:
        importance = 3

    return {
        "title": str(
            data.get("title")
            or title_hint
            or "影片收藏"
        )[:120],
        "summary": str(
            data.get("summary")
            or ""
        )[:1800],
        "snapshot": str(
            data.get("snapshot")
            or ""
        )[:1800],
        "why": str(
            data.get("why")
            or ""
        )[:1000],
        "category": category,
        "tags": tags,
        "applications": apps or ["待判斷"],
        "status": status,
        "importance": importance,
        "related": str(
            data.get("related")
            or ""
        )[:1000],
    }


def summarize_chunk(
    chunk,
    index,
    total,
    reference_context,
):
    system = """你是影音逐字稿整理助理。使用繁體中文（台灣用語）。
只整理已提供的影片內容，不猜測。
保留重要人物、數字、主張、事件與可應用重點。
若逐字稿中的英文專有名詞與平台 Metadata / Caption 拼法衝突，
專有名詞拼法以可驗證的平台原文為優先，但不可添加影片沒說的事實。"""

    response = ai_client.chat.completions.create(
        model=AI_MODEL,
        temperature=0.1,
        messages=[
            {
                "role": "system",
                "content": system,
            },
            {
                "role": "user",
                "content": (
                    f"可驗證參考文字：\n"
                    f"{reference_context or '（無）'}\n\n"
                    f"這是長影片逐字稿第 {index}/{total} 段，"
                    f"請濃縮成 500-900 字重點：\n\n{chunk}"
                ),
            },
        ],
    )

    return (
        response.choices[0].message.content
        or ""
    )


def prepare_ai_material(
    transcript,
    reference_context,
):
    if len(transcript) <= 30000:
        return transcript

    chunks = split_transcript_for_correction(
        transcript,
        max_chars=24000,
    )

    summaries = []

    for i, chunk in enumerate(
        chunks,
        start=1,
    ):
        print(
            f"AI 長逐字稿分段整理 {i}/{len(chunks)}..."
        )

        summaries.append(
            summarize_chunk(
                chunk,
                i,
                len(chunks),
                reference_context,
            )
        )

    return "\n\n".join(
        f"【第 {i} 段摘要】\n{text}"
        for i, text in enumerate(
            summaries,
            start=1,
        )
    )


def analyze_transcript(
    page,
    transcript,
    metadata=None,
):
    props = page.get("properties", {})

    title_hint = get_text_property(
        props.get("標題", {})
    )

    source_platform = (
        get_select_property(
            props.get("來源平台", {})
        )
        or "其他"
    )

    original_note = get_text_property(
        props.get("原始備註", {})
    )

    original_url = (
        props.get("原始連結", {})
        or {}
    ).get("url") or ""

    reference_context = build_reference_context(
        page,
        metadata,
    )

    material = prepare_ai_material(
        transcript,
        reference_context,
    )

    system = """你是個人影音知識庫整理助理。使用繁體中文（台灣用語），只輸出 JSON。

請以「影片逐字稿」為內容事實的主要依據；平台 Metadata、Caption、使用者備註是輔助參考。

資料衝突規則：
1. 影片是否真的提到某件事，以逐字稿為準，不得把 Caption 中未在影片出現的資訊硬加進去。
2. 品牌名、產品名、工具名、模型名、人名、公司名、地名、英文技術詞的「拼法」若衝突，優先採用平台 Metadata / 原始 Caption 中可驗證的正式拼法。
3. 不可把 ASR 音譯錯誤當成新的工具、品牌或人物。
4. 不確定的專有名詞不要自行創造名稱。
5. 使用者主動收藏即代表有保存意圖，不得建議刪除。

固定主分類只能擇一：
政策與政府計畫、AI／科技工具、產業案例與趨勢、簡報與視覺素材、工作方法／Prompt／範本、個人生活與興趣、暫存待判斷。

標籤只能從：
AI、政策、工具、產業、簡報、研究、生活。

應用情境只能從：
工作、政策研究、簡報、學習、生活、娛樂、待判斷。

status 只能是：
待看、深入研究、可應用。

importance 為 1-5。
snapshot 請用 3-6 點精煉內容重點，以換行分隔。

請輸出：
{"title":"","summary":"","snapshot":"","why":"","category":"","tags":[],"applications":[],"status":"","importance":3,"related":""}"""

    user = f"""來源平台：{source_platform}
原始標題：{title_hint or '（無）'}
原始備註：{original_note or '（無）'}
原始連結：{original_url or '（無）'}

【可驗證的平台參考文字】
{reference_context or '（無）'}

【影片逐字稿／長影片分段摘要】
{material}"""

    response = ai_client.chat.completions.create(
        model=AI_MODEL,
        temperature=0.1,
        messages=[
            {
                "role": "system",
                "content": system,
            },
            {
                "role": "user",
                "content": user,
            },
        ],
    )

    content = (
        response.choices[0].message.content
    )

    if not content:
        raise RuntimeError(
            "WiRouter AI 回傳空內容"
        )

    return normalize_ai_result(
        safe_json(content),
        title_hint,
    )


def rich_text_prop(text):
    return {
        "rich_text": [
            {
                "type": "text",
                "text": {
                    "content": (
                        text
                        or ""
                    )[:1900]
                },
            }
        ]
    }


def rich_text_prop_long(text, limit=7000):
    value = (text or "")[:limit]
    return {
        "rich_text": [
            {
                "type": "text",
                "text": {
                    "content": value[i:i + 1800]
                },
            }
            for i in range(0, len(value), 1800)
        ]
    }


def ensure_platform_original(page):
    """
    舊資料可能沒有「平台原文」。
    在 AI 覆寫內容快照前，把目前可用的來源文字保存一份。
    """
    props = page.get("properties", {})
    existing = get_text_property(
        props.get("平台原文", {})
    )
    if existing:
        return existing

    snapshot = get_text_property(
        props.get("內容快照", {})
    )
    cleaned = clean_reference_text(snapshot)

    if (
        not cleaned
        or noisy_snapshot(cleaned)
    ):
        return ""

    update_page_properties(
        page["id"],
        {
            "平台原文": rich_text_prop_long(
                snapshot
            )
        },
    )

    # 同步更新本地 page 物件，讓本輪即可使用。
    page.setdefault(
        "properties",
        {}
    )["平台原文"] = rich_text_prop_long(
        snapshot
    )

    print(
        "已保存平台原文，避免 AI 摘要覆蓋原始 Caption。"
    )

    return snapshot


def write_ai_result(
    page_id,
    result,
):
    update_page_properties(
        page_id,
        {
            "標題": {
                "title": [
                    {
                        "type": "text",
                        "text": {
                            "content": result[
                                "title"
                            ][:120]
                        },
                    }
                ]
            },
            "AI 摘要": rich_text_prop(
                result["summary"]
            ),
            "內容快照": rich_text_prop(
                result["snapshot"]
            ),
            "為什麼值得留": rich_text_prop(
                result["why"]
            ),
            "主分類": {
                "select": {
                    "name": result["category"]
                }
            },
            "標籤": {
                "multi_select": [
                    {"name": x}
                    for x in result["tags"]
                ]
            },
            "應用情境": {
                "multi_select": [
                    {"name": x}
                    for x in result[
                        "applications"
                    ]
                ]
            },
            "狀態": {
                "select": {
                    "name": result["status"]
                }
            },
            "重要度": {
                "number": result[
                    "importance"
                ]
            },
            "可能關聯": rich_text_prop(
                result["related"]
            ),
            "AI處理完成": {
                "checkbox": True
            },
        },
    )


def save_corrected_copy(
    job_dir,
    raw_transcript,
    corrected_transcript,
):
    raw_file = job_dir / "transcript_raw.txt"
    corrected_file = (
        job_dir / "transcript_corrected.txt"
    )

    raw_file.write_text(
        raw_transcript,
        encoding="utf-8",
    )

    corrected_file.write_text(
        corrected_transcript,
        encoding="utf-8",
    )

    return raw_file, corrected_file


def process_ai_only(page):
    page_id = page["id"]

    transcript = read_transcript_from_page(
        page_id
    )

    if not transcript:
        raise RuntimeError(
            "逐字稿狀態為完成，但 Notion Page Body 找不到逐字稿"
        )

    print(
        "已有逐字稿，先做專有名詞校正，"
        "再進行 WiRouter AI 整理，不重跑 Whisper。"
    )

    ensure_platform_original(page)

    reference_context = build_reference_context(
        page,
        metadata=None,
    )

    corrected = correct_transcript(
        transcript,
        reference_context,
    )

    source_name = (
        get_select_property(
            page.get(
                "properties",
                {}
            ).get(
                "文字來源",
                {},
            )
        )
        or "Local Whisper"
    )

    if corrected != transcript:
        replace_transcript_section(
            page_id,
            corrected,
            source_name,
        )

    result = analyze_transcript(
        page,
        corrected,
        metadata=None,
    )

    write_ai_result(
        page_id,
        result,
    )

    print(
        f"✅ AI 整理完成：{result['title']}"
    )


def process_transcription(page):
    page_id = page["id"]
    props = page["properties"]

    title = get_text_property(
        props.get("標題", {})
    )

    video_url = (
        props.get(
            "原始連結",
            {},
        )
        or {}
    ).get("url")

    if not video_url:
        raise RuntimeError(
            "Notion 項目沒有原始連結"
        )

    print("\n==============================")
    print("開始處理 Notion 影片")
    print("==============================")
    print("標題：", title)
    print("URL：", video_url)

    update_page_properties(
        page_id,
        {
            "逐字稿狀態": {
                "select": {
                    "name": "處理中"
                }
            }
        },
    )

    try:
        (
            raw_transcript,
            metadata,
            job_dir,
        ) = run_transcriber(
            video_url
        )

        source_name = map_text_source(
            metadata.get("text_source")
        )

        ensure_platform_original(page)

        reference_context = build_reference_context(
            page,
            metadata,
        )

        corrected_transcript = correct_transcript(
            raw_transcript,
            reference_context,
        )

        save_corrected_copy(
            job_dir,
            raw_transcript,
            corrected_transcript,
        )

        language_name = detect_language(
            corrected_transcript,
            metadata,
        )

        duration = metadata.get(
            "duration_seconds"
        )

        append_transcript_to_page(
            page_id,
            corrected_transcript,
            source_name,
        )

        now = (
            datetime.now()
            .astimezone()
            .isoformat()
        )

        props_update = {
            "逐字稿狀態": {
                "select": {
                    "name": "完成"
                }
            },
            "文字來源": {
                "select": {
                    "name": source_name
                }
            },
            "逐字稿語言": {
                "select": {
                    "name": language_name
                }
            },
            "轉錄時間": {
                "date": {
                    "start": now
                }
            },
            "擷取狀態": {
                "select": {
                    "name": "成功"
                }
            },
            "AI處理完成": {
                "checkbox": False
            },
            "AI最後錯誤": rich_text_prop(""),
        }

        if duration is not None:
            props_update["影片時長"] = {
                "number": float(duration)
            }

        update_page_properties(
            page_id,
            props_update,
        )

        result = analyze_transcript(
            page,
            corrected_transcript,
            metadata,
        )

        write_ai_result(
            page_id,
            result,
        )

        print(
            "\n✅ 逐字稿 + 專有名詞校正 + AI 整理全部完成"
        )
        print(
            "文字來源：",
            source_name,
        )
        print(
            "語言：",
            language_name,
        )
        print(
            "AI 標題：",
            result["title"],
        )
        print(
            "Job：",
            job_dir,
        )

    except Exception as exc:
        print(
            "\n❌ 處理失敗：",
            exc,
        )

        # 若逐字稿已成功完成但 AI 失敗，
        # 不應把轉錄狀態改回失敗。
        try:
            refreshed = fetch_page(
                page_id
            )
            current = get_select_property(
                refreshed.get(
                    "properties",
                    {},
                ).get(
                    "逐字稿狀態",
                    {},
                )
            )
        except Exception:
            current = None

        if current != "完成":
            error_text = str(
                exc
            ).lower()

            download_markers = [
                "unable to extract",
                "unsupported url",
                "login required",
                "video unavailable",
                "http error",
                "download",
            ]

            status = (
                "無法下載"
                if any(
                    x in error_text
                    for x in download_markers
                )
                else "失敗"
            )

            refreshed_props = refreshed.get("properties", {}) if refreshed else props
            retries = int(
                get_number_property(
                    refreshed_props.get("AI重試次數", {})
                )
                or 0
            )
            now = datetime.now().astimezone().isoformat()

            update_page_properties(
                page_id,
                {
                    "逐字稿狀態": {
                        "select": {
                            "name": status
                        }
                    },
                    "AI處理完成": {"checkbox": False},
                    "AI重試次數": {"number": retries + 1},
                    "最後重試時間": {"date": {"start": now}},
                    "AI最後錯誤": rich_text_prop(str(exc)[:1800]),
                },
            )

        raise


def reprocess_page(page_id_or_url):
    page_id = page_id_or_url.strip()

    match = re.search(
        r"([0-9a-fA-F]{32})",
        page_id.replace("-", ""),
    )

    if match:
        compact = match.group(1)
        page_id = (
            f"{compact[0:8]}-"
            f"{compact[8:12]}-"
            f"{compact[12:16]}-"
            f"{compact[16:20]}-"
            f"{compact[20:32]}"
        )

    page = fetch_page(page_id)

    transcript = read_transcript_from_page(
        page["id"]
    )

    if not transcript:
        raise RuntimeError(
            "指定頁面沒有既有逐字稿，請使用正常 Queue 處理。"
        )

    print(
        "重新校正既有逐字稿，不重跑 Whisper..."
    )

    ensure_platform_original(page)

    reference_context = build_reference_context(
        page,
        metadata=None,
    )

    corrected = correct_transcript(
        transcript,
        reference_context,
    )

    source_name = (
        get_select_property(
            page.get(
                "properties",
                {},
            ).get(
                "文字來源",
                {},
            )
        )
        or "Local Whisper"
    )

    if corrected != transcript:
        replace_transcript_section(
            page["id"],
            corrected,
            source_name,
        )

    result = analyze_transcript(
        page,
        corrected,
        metadata=None,
    )

    write_ai_result(
        page["id"],
        result,
    )

    update_page_properties(
        page["id"],
        {
            "逐字稿狀態": {
                "select": {
                    "name": "完成"
                }
            },
            "AI處理完成": {
                "checkbox": True
            },
        },
    )

    print(
        "✅ 既有影片已重新做專有名詞校正與 AI 整理"
    )
    print(
        "AI 標題：",
        result["title"],
    )


def peek():
    mode, page = get_next_job()

    if not page:
        print(
            "目前沒有待處理影片或待 AI 整理項目。"
        )
        return

    props = page["properties"]

    print(
        "找到待處理項目 ✅"
    )
    print(
        "模式：",
        (
            "只做 AI"
            if mode == "ai_only"
            else "轉錄 + AI"
        ),
    )
    print(
        "標題：",
        get_text_property(
            props.get(
                "標題",
                {},
            )
        ),
    )
    print(
        "來源平台：",
        get_select_property(
            props.get(
                "來源平台",
                {},
            )
        ),
    )
    print(
        "URL：",
        (
            props.get(
                "原始連結",
                {},
            )
            or {}
        ).get("url"),
    )
    print(
        "這只是 Peek，尚未修改 Notion。"
    )


def run_once():
    mode, page = get_next_job()

    if not page:
        print(
            "目前沒有待處理影片或待 AI 整理項目。"
        )
        return

    if mode == "ai_only":
        process_ai_only(page)
    else:
        process_transcription(page)


def run_loop():
    print(
        "Local AI Worker 已啟動。"
    )

    while True:
        try:
            mode, page = get_next_job()

            if page:
                if mode == "ai_only":
                    process_ai_only(
                        page
                    )
                else:
                    process_transcription(
                        page
                    )
            else:
                print(
                    "沒有待處理工作，60 秒後再檢查..."
                )
                time.sleep(60)

        except KeyboardInterrupt:
            print(
                "Worker 已停止。"
            )
            break

        except Exception as exc:
            print(
                "Worker 發生錯誤：",
                exc,
            )
            time.sleep(60)


def print_usage():
    print(
        "用法：\n"
        "python notion_worker.py --peek\n"
        "python notion_worker.py --once\n"
        "python notion_worker.py --loop\n"
        "python notion_worker.py --reprocess <Notion Page ID 或 URL>"
    )


if __name__ == "__main__":
    mode = (
        sys.argv[1]
        if len(sys.argv) >= 2
        else "--peek"
    )

    if mode == "--peek":
        peek()

    elif mode == "--once":
        run_once()

    elif mode == "--loop":
        run_loop()

    elif mode == "--reprocess":
        if len(sys.argv) < 3:
            print_usage()
            sys.exit(1)
        reprocess_page(
            sys.argv[2]
        )

    else:
        print_usage()
