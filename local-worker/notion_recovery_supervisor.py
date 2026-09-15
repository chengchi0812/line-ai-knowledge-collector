import sys
import time

import notion_recovery_worker as recovery


def query_one(body):
    data = recovery.notion_request(
        "POST",
        f"https://api.notion.com/v1/data_sources/{recovery.NOTION_DATA_SOURCE_ID}/query",
        body,
    )
    results = data.get("results", [])
    return results[0] if results else None


def active_video_job():
    return query_one(
        {
            "page_size": 1,
            "filter": {
                "and": [
                    {"property": "內容類型", "select": {"equals": "影片"}},
                    {"property": "是否重複", "checkbox": {"equals": False}},
                    {
                        "or": [
                            {"property": "逐字稿狀態", "select": {"equals": "待處理"}},
                            {"property": "逐字稿狀態", "select": {"equals": "處理中"}},
                            {
                                "and": [
                                    {"property": "逐字稿狀態", "select": {"equals": "完成"}},
                                    {"property": "AI處理完成", "checkbox": {"equals": False}},
                                ]
                            },
                        ]
                    },
                ]
            },
            "sorts": [
                {"property": "收藏日期", "direction": "descending"}
            ],
        }
    )


def video_recovery_candidates():
    results = []
    cursor = None
    while True:
        body = {
            "page_size": 100,
            "filter": {
                "and": [
                    {"property": "內容類型", "select": {"equals": "影片"}},
                    {"property": "是否重複", "checkbox": {"equals": False}},
                    {"property": "AI處理完成", "checkbox": {"equals": False}},
                    {
                        "or": [
                            {"property": "逐字稿狀態", "select": {"equals": "失敗"}},
                            {"property": "逐字稿狀態", "select": {"equals": "無法下載"}},
                            {"property": "逐字稿狀態", "select": {"is_empty": True}},
                        ]
                    },
                ]
            },
            "sorts": [
                {"property": "收藏日期", "direction": "descending"}
            ],
        }
        if cursor:
            body["start_cursor"] = cursor

        data = recovery.notion_request(
            "POST",
            f"https://api.notion.com/v1/data_sources/{recovery.NOTION_DATA_SOURCE_ID}/query",
            body,
        )
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if len(results) >= 500:
            break
    return results


def next_video_recovery():
    if active_video_job():
        return None
    for page in video_recovery_candidates():
        if recovery.retry_eligible(page):
            return page
    return None


def reset_video_for_retry(page):
    props = page.get("properties", {})
    current = int(
        recovery.get_number_property(
            props.get("AI重試次數", {})
        )
        or 0
    )

    recovery.update_page_properties(
        page["id"],
        {
            "逐字稿狀態": {"select": {"name": "待處理"}},
            "AI重試次數": {"number": current + 1},
            "最後重試時間": {"date": {"start": recovery.now_iso()}},
            "AI最後錯誤": recovery.rich_text_prop(""),
        },
    )

    title = recovery.get_text_property(props.get("標題", {}))
    print("♻️ 已將影片重新排入 Queue：", title)


def choose_action():
    active = active_video_job()
    if active:
        return "video_active", active

    retry_video = next_video_recovery()
    if retry_video:
        return "video_retry", retry_video

    mode, page = recovery.select_next_job()
    if page:
        return f"nonvideo_{mode}", page

    return None, None


def peek():
    action, page = choose_action()
    if not page:
        print("目前沒有待處理或可重試項目。")
        return

    props = page.get("properties", {})
    title = recovery.get_text_property(props.get("標題", {}))
    content_type = recovery.get_select_property(props.get("內容類型", {}))

    if action == "video_active":
        print("目前影片主 Worker 已有工作，Recovery Supervisor 先等待。")
    elif action == "video_retry":
        print("找到可重新排程的失敗影片 ✅")
    elif action == "nonvideo_ready":
        print("找到已有內容、只差 AI 的非影片項目 ✅")
    else:
        print("找到需要補抓內容的非影片項目 ✅")

    print("內容類型：", content_type)
    print("標題：", title)
    print("AI重試次數：", int(recovery.get_number_property(props.get("AI重試次數", {})) or 0))


def run_once():
    action, page = choose_action()
    if not page:
        print("目前沒有待處理或可重試項目。")
        return

    if action == "video_active":
        print("影片主 Worker 正在處理／等待處理，Supervisor 本輪不搶資源。")
        return

    if action == "video_retry":
        reset_video_for_retry(page)
        return

    if action == "nonvideo_ready":
        recovery.process_ready(page)
    else:
        recovery.process_recovery(page)


def run_loop():
    print("Notion Recovery Supervisor 已啟動。")
    print("優先順序：影片主 Queue → 失敗影片重試 → 非影片 AI/補抓。")

    while True:
        try:
            action, page = choose_action()

            if not page:
                print(f"沒有 Recovery 工作，{recovery.LOOP_SECONDS} 秒後再檢查...")
                time.sleep(recovery.LOOP_SECONDS)
                continue

            if action == "video_active":
                print("影片主 Worker 有工作，Supervisor 暫停一輪避免並行。")
                time.sleep(recovery.LOOP_SECONDS)
                continue

            if action == "video_retry":
                reset_video_for_retry(page)
                time.sleep(recovery.LOOP_SECONDS)
                continue

            if action == "nonvideo_ready":
                recovery.process_ready(page)
            else:
                recovery.process_recovery(page)

            time.sleep(5)

        except KeyboardInterrupt:
            print("Recovery Supervisor 已停止。")
            break
        except Exception as exc:
            print("Recovery Supervisor 本輪錯誤：", exc)
            time.sleep(recovery.LOOP_SECONDS)


def usage():
    print(
        "用法：\n"
        "python notion_recovery_supervisor.py --peek\n"
        "python notion_recovery_supervisor.py --once\n"
        "python notion_recovery_supervisor.py --loop"
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
        usage()
