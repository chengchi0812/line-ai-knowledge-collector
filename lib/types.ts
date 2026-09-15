export type SourcePlatform =
  | "LINE" | "Instagram" | "Facebook" | "TikTok" | "YouTube" | "Web" | "檔案" | "其他";

export type ContentType =
  | "文章" | "影片" | "圖片" | "PDF" | "檔案" | "工具" | "案例" | "貼文" | "其他";

export type MainCategory =
  | "政策與政府計畫" | "AI／科技工具" | "產業案例與趨勢" | "簡報與視覺素材"
  | "工作方法／Prompt／範本" | "個人生活與興趣" | "暫存待判斷";

export type KnowledgeStatus =
  | "收件匣" | "待看" | "已讀" | "深入研究" | "可應用" | "已處理" | "封存" | "建議刪除";

export type CaptureStatus = "成功" | "部分" | "失敗" | "未擷取";

export type AIResult = {
  title: string;
  summary: string;
  why: string;
  category: MainCategory;
  tags: Array<"AI" | "政策" | "工具" | "產業" | "簡報" | "研究" | "生活">;
  applications: Array<"工作" | "政策研究" | "簡報" | "學習" | "生活" | "娛樂" | "待判斷">;
  status: Extract<KnowledgeStatus, "待看" | "深入研究" | "可應用" | "建議刪除">;
  importance: 1 | 2 | 3 | 4 | 5;
  related: string;
};

export type StoredItem = {
  title: string;
  originalTitle?: string;
  sourcePlatform: SourcePlatform;
  contentType: ContentType;
  originalUrl?: string;
  originalNote: string;
  snapshot: string;
  messageId: string;
  captureStatus: CaptureStatus;
  ai: AIResult;
  notionFileUploadId?: string;
  collectedAt?: string;
};
