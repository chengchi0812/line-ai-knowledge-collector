from pathlib import Path
import shutil
import sys

BASE = Path.home() / "local-ai-transcriber"
FILES = [BASE / "notion_worker.py", BASE / "notion_recovery_worker.py"]

TITLE_RULES = '''標題規則：
1. title 是「知識庫閱讀標題」，目的是讓使用者一眼知道內容在講什麼；原始平台標題另存於「原始標題」，不要把兩者混在一起。
2. 原始標題若已清楚具體，保留核心意思並精簡即可；若原始標題過長、只有 hashtag、聳動、模糊或只是收藏日期，才依正文／逐字稿重新命名。
3. 優先使用「人物／品牌／公司 + 核心事件、觀點或方法」或「主題 + 具體結論／方法／爭議」結構。
4. 中文標題以約 15-32 個中文字為主，資訊要具體、有辨識度；不要為了長度硬塞字。
5. 避免空泛或重複句型，例如「影片分享」「內容整理」「重點摘要」「值得關注」「相關資訊」；也不要連續把不同內容都寫成「某某談……」。
6. 人名、品牌、公司、數字與結論必須有原始標題、正文、Caption、Metadata 或逐字稿支持，不可自行補充。
7. 若內容不足以判斷，保留清楚的原始標題；真的沒有可辨識內容時才用【待補內容】。
'''

HELPERS = r'''
def title_is_generic(title):
    value = clean_reference_text(title or "").strip()
    if not value:
        return True
    lowered = value.lower()
    if "【待補內容】" in value or "【未擷取】" in value:
        return True
    exact = {
        "youtube", "youtube 影片", "youtube 影片（未取得標題）", "youtube 影片 (未取得標題)",
        "tiktok", "tiktok 收藏", "instagram", "instagram 收藏", "facebook", "facebook 收藏",
        "line 收藏", "待整理收藏", "收藏內容",
    }
    if lowered in exact:
        return True
    if re.fullmatch(r"(?:youtube|tiktok|instagram|facebook|web|line)[ ｜_-]*(?:影片|收藏)?[ ｜_-]*\d{4}-\d{2}-\d{2}(?:（\d+）)?", lowered, flags=re.I):
        return True
    return False


def save_original_title(page, candidate):
    cleaned = clean_reference_text(candidate or "").strip()
    if not cleaned or title_is_generic(cleaned):
        return ""

    props = page.get("properties", {})
    existing = get_text_property(props.get("原始標題", {}))
    if existing and not title_is_generic(existing):
        return existing

    value = cleaned[:1900]
    prop = {
        "rich_text": [
            {"type": "text", "text": {"content": value}}
        ]
    }
    update_page_properties(page["id"], {"原始標題": prop})
    page.setdefault("properties", {})["原始標題"] = prop
    print("已保存原始標題：", value[:120])
    return value

'''

RECOVERY_HELPERS = r'''
def title_is_generic(title):
    value = clean_text(title or "").strip()
    if not value:
        return True
    lowered = value.lower()
    if "【待補內容】" in value or "【未擷取】" in value:
        return True
    exact = {
        "youtube", "youtube 影片", "youtube 影片（未取得標題）", "youtube 影片 (未取得標題)",
        "tiktok", "tiktok 收藏", "instagram", "instagram 收藏", "facebook", "facebook 收藏",
        "line 收藏", "待整理收藏", "收藏內容",
    }
    if lowered in exact:
        return True
    if re.fullmatch(r"(?:youtube|tiktok|instagram|facebook|web|line)[ ｜_-]*(?:影片|收藏)?[ ｜_-]*\d{4}-\d{2}-\d{2}(?:（\d+）)?", lowered, flags=re.I):
        return True
    return False

'''


def replace_once(text, old, new, label):
    if new in text:
        return text
    if old not in text:
        raise RuntimeError(f"找不到預期程式片段：{label}")
    return text.replace(old, new, 1)


def patch_video_worker(path):
    text = path.read_text(encoding="utf-8")

    marker = "def build_reference_context(page, metadata=None):\n"
    if "def save_original_title(page, candidate):" not in text:
        if marker not in text:
            raise RuntimeError("notion_worker.py 找不到 build_reference_context")
        text = text.replace(marker, HELPERS + marker, 1)

    text = replace_once(
        text,
        '''    current_title = get_text_property(\n        props.get("標題", {})\n    )\n    original_note = get_text_property(''',
        '''    current_title = get_text_property(\n        props.get("標題", {})\n    )\n    original_title = get_text_property(\n        props.get("原始標題", {})\n    )\n    original_note = get_text_property(''',
        "video reference original title",
    )

    text = replace_once(
        text,
        '''    if current_title:\n        parts.append(\n            f"Notion 既有標題：{clean_reference_text(current_title)}"\n        )\n''',
        '''    if original_title:\n        parts.append(\n            f"平台原始標題：{clean_reference_text(original_title)}"\n        )\n\n    if current_title:\n        parts.append(\n            f"Notion 目前閱讀標題：{clean_reference_text(current_title)}"\n        )\n''',
        "video reference labels",
    )

    text = replace_once(
        text,
        '''    title_hint = get_text_property(\n        props.get("標題", {})\n    )\n''',
        '''    current_title = get_text_property(\n        props.get("標題", {})\n    )\n    original_title = get_text_property(\n        props.get("原始標題", {})\n    )\n    title_hint = original_title or current_title\n''',
        "video analyze title hint",
    )

    old_rules = '''5. 使用者主動收藏即代表有保存意圖，不得建議刪除。\n\n固定主分類只能擇一：'''
    new_rules = '''5. 使用者主動收藏即代表有保存意圖，不得建議刪除。\n\n''' + TITLE_RULES + '''\n固定主分類只能擇一：'''
    text = replace_once(text, old_rules, new_rules, "video title prompt")

    text = replace_once(
        text,
        '''來源平台：{source_platform}\n原始標題：{title_hint or '（無）'}\n原始備註：{original_note or '（無）'}''',
        '''來源平台：{source_platform}\n平台原始標題：{original_title or '（無）'}\n目前知識庫標題：{current_title or '（無）'}\n原始備註：{original_note or '（無）'}''',
        "video prompt source title",
    )

    text = replace_once(
        text,
        '''        source_name = map_text_source(\n            metadata.get("text_source")\n        )\n\n        ensure_platform_original(page)''',
        '''        save_original_title(\n            page,\n            metadata.get("title") or "",\n        )\n\n        source_name = map_text_source(\n            metadata.get("text_source")\n        )\n\n        ensure_platform_original(page)''',
        "video save metadata title",
    )

    path.write_text(text, encoding="utf-8")


def patch_recovery_worker(path):
    text = path.read_text(encoding="utf-8")

    marker = "def existing_material(page):\n"
    if "def title_is_generic(title):" not in text:
        if marker not in text:
            raise RuntimeError("notion_recovery_worker.py 找不到 existing_material")
        text = text.replace(marker, RECOVERY_HELPERS + marker, 1)

    text = replace_once(
        text,
        '''    title = get_text_property(props.get("標題", {}))\n\n    parts = []''',
        '''    title = get_text_property(props.get("標題", {}))\n    original_title = get_text_property(props.get("原始標題", {}))\n\n    parts = []''',
        "recovery material original title",
    )

    text = replace_once(
        text,
        '''    if title:\n        parts.append(f"【既有標題】\\n{clean_text(title)[:500]}")''',
        '''    if original_title:\n        parts.append(f"【平台原始標題】\\n{clean_text(original_title)[:500]}")\n    if title:\n        parts.append(f"【目前知識庫標題】\\n{clean_text(title)[:500]}")''',
        "recovery material title labels",
    )

    text = replace_once(
        text,
        '''    return text, "部分"\n\n\ndef first_meta''',
        '''    return text, "部分", clean_text(data.get("title") or "")\n\n\ndef first_meta''',
        "recovery ytdlp original title",
    )

    text = replace_once(
        text,
        '''    return text, "成功" if len(body) >= 500 else "部分"\n\n\ndef get_attachment_url''',
        '''    return text, "成功" if len(body) >= 500 else "部分", (og_title or title)\n\n\ndef get_attachment_url''',
        "recovery web original title",
    )

    text = replace_once(
        text,
        '''    return f"【附件：{name}】\\n{text[:12000]}", "成功"\n\n\ndef recover_content''',
        '''    return f"【附件：{name}】\\n{text[:12000]}", "成功", name\n\n\ndef recover_content''',
        "recovery attachment original title",
    )

    text = replace_once(
        text,
        '''    title_hint = get_text_property(props.get("標題", {}))\n    source_platform = get_select_property''',
        '''    current_title = get_text_property(props.get("標題", {}))\n    original_title = get_text_property(props.get("原始標題", {}))\n    title_hint = original_title or current_title\n    source_platform = get_select_property''',
        "recovery analyze title hint",
    )

    old_rules = '''請只根據提供的可驗證內容整理，不得猜測缺失資訊。使用者主動收藏即代表有保存意圖，不得建議刪除。\n\n固定主分類只能擇一：'''
    new_rules = '''請只根據提供的可驗證內容整理，不得猜測缺失資訊。使用者主動收藏即代表有保存意圖，不得建議刪除。\n\n''' + TITLE_RULES + '''\n固定主分類只能擇一：'''
    text = replace_once(text, old_rules, new_rules, "recovery title prompt")

    text = replace_once(
        text,
        '''內容類型：{content_type}\n原始標題：{title_hint or '（無）'}\n原始備註：{original_note or '（無）'}''',
        '''內容類型：{content_type}\n平台原始標題：{original_title or '（無）'}\n目前知識庫標題：{current_title or '（無）'}\n原始備註：{original_note or '（無）'}''',
        "recovery prompt source title",
    )

    text = replace_once(
        text,
        '''def write_success(page, result, recovered_text=None, capture_status=None):''',
        '''def write_success(page, result, recovered_text=None, capture_status=None, original_title=None):''',
        "recovery write success signature",
    )

    text = replace_once(
        text,
        '''    if recovered_text:\n        props["平台原文"] = rich_text_prop_long(recovered_text)\n    if capture_status:''',
        '''    if recovered_text:\n        props["平台原文"] = rich_text_prop_long(recovered_text)\n    if original_title and not title_is_generic(original_title):\n        existing_original = get_text_property(page.get("properties", {}).get("原始標題", {}))\n        if not existing_original or title_is_generic(existing_original):\n            props["原始標題"] = rich_text_prop(original_title)\n    if capture_status:''',
        "recovery write original title",
    )

    text = replace_once(
        text,
        '''        recovered_text, capture_status = recover_content(page)''',
        '''        recovered_text, capture_status, recovered_title = recover_content(page)''',
        "recovery unpack title",
    )

    text = replace_once(
        text,
        '''            recovered_text=recovered_text,\n            capture_status=capture_status,\n        )''',
        '''            recovered_text=recovered_text,\n            capture_status=capture_status,\n            original_title=recovered_title,\n        )''',
        "recovery save title",
    )

    path.write_text(text, encoding="utf-8")


def main():
    missing = [str(p) for p in FILES if not p.exists()]
    if missing:
        raise RuntimeError("找不到檔案：" + ", ".join(missing))

    for path in FILES:
        backup = path.with_suffix(path.suffix + ".before-title-policy")
        if not backup.exists():
            shutil.copy2(path, backup)

    patch_video_worker(FILES[0])
    patch_recovery_worker(FILES[1])

    print("標題政策已套用：")
    print("- 保存平台／文章原始標題到 Notion『原始標題』")
    print("- 『標題』改為方便瀏覽的 AI 閱讀標題")
    print("- 影片與非影片都使用相同標題規則")
    print("- 原始內容不足時仍保留【待補內容】")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("套用標題政策失敗：", exc)
        sys.exit(1)
