# LINE AI 個人知識庫 Collector

把你原本「看到東西就轉傳到自己的 LINE 群組」的習慣保留下來，後端自動完成：

LINE 群組 → Webhook → 網址/附件擷取 → AI 分類摘要 → Notion「AI 個人知識庫」

## 已支援

- LINE 群組文字、網址
- Instagram / Facebook / TikTok / YouTube / 一般 Web 網址來源辨識
- 一般網頁 title / description / 可讀文字片段擷取（社群網站抓不到時會降級為只保存網址）
- 圖片、影片、音訊、PDF、一般檔案：20MB 內收到當下直接備份到 Notion
- AI 自動：標題、摘要、收藏原因、主分類、標籤、應用情境、重要度、狀態、可能關聯
- AI API 未設定或失敗時，仍會先建立 Notion 收件資料，不會丟失收藏
- LINE Webhook HMAC-SHA256 簽章驗證
- 預設只接受 group chat 訊息，不接陌生人私訊
- 可用 LINE_ALLOWED_GROUP_ID 再鎖定唯一群組

## 需要的環境變數

複製 `.env.example` 為 `.env.local`：

- `LINE_CHANNEL_SECRET`
- `LINE_CHANNEL_ACCESS_TOKEN`
- `NOTION_API_KEY`
- `NOTION_DATA_SOURCE_ID=344dad85-d683-494d-9eac-d4624159a0de`
- `AI_BASE_URL`
- `AI_API_KEY`
- `AI_MODEL`
- `LINE_ALLOWED_GROUP_ID`（選填，第一次測試後建議補上）

### WiRouter（OpenAI-compatible）範例

```env
AI_BASE_URL=https://wirouter.wiadvance.com/inference/v1
AI_API_KEY=你的_WIROUTER_API_KEY
AI_MODEL=GPT-OSS-120b
```

## Notion API 權限

ChatGPT 裡連接的 Notion App 與部署在 Vercel 的後端不是同一組憑證。Vercel 後端需要一組 Notion internal connection / personal access token，並對「AI 個人知識庫」授權 Content access（至少 Read content + Insert content；若要後續更新則加 Update content）。

目前資料來源 ID 已建立：

`344dad85-d683-494d-9eac-d4624159a0de`

## LINE 設定

1. 建立 LINE Official Account / Messaging API channel。
2. 開啟「Allow bot to join group chats」。
3. 取得 Channel secret 與 Channel access token。
4. 部署到 Vercel 後，把 Webhook URL 設為：
   `https://你的網域/api/line/webhook`
5. 開啟 Use webhook，按 Verify。
6. 把 Official Account 邀進你的個人收藏群組。
7. 先丟一則網址測試。

同一個 LINE 群組同時間只能有一個 LINE Official Account。

## Vercel 部署

```bash
npm install
npm run typecheck
npm run build
```

將專案推到 GitHub 並 Import 到 Vercel，或在專案資料夾用 Vercel CLI 部署。把 `.env.example` 中的環境變數放到 Vercel Project Settings → Environment Variables。

部署後先開：

`https://你的網域/api/health`

所有 `configured` 都應為 `true`（`ai` 若還沒設定可以暫時 false，系統仍會保存資料）。

## 安全設計

- 一律先以原始 request body 驗證 `x-line-signature`，成功後才 JSON parse。
- 僅接受 group chat 訊息。
- 建議第一次成功後將 groupId 填入 `LINE_ALLOWED_GROUP_ID`。
- 網頁自動擷取拒絕 localhost、常見私有 IP、非 80/443 自訂 port。
- 任何 API 金鑰只放 Vercel 環境變數，不進 Git。

## 下一階段

- PDF / Word / PPT 內容解析後再摘要
- 圖片 OCR / Vision
- URL 去重與相似內容偵測
- 每週 LINE 回顧摘要
- Supabase 向量搜尋與自然語言問答
