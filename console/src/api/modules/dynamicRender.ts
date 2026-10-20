import { request } from "../request";

export interface TemplateInfo {
  templateName: string;
  templateId: number;
  /** 模板标志：'main' 为普通模板，'no_query' 为静态模板（无需获取数据，直接渲染） */
  templateFlag?: string;
}

export interface TemplateListResponse {
  data: TemplateInfo[];
}

export interface TemplateContentResponse {
  file_name: string;
  content: string;
}

export interface RecordDataRequest {
  resultId: string;
  templateId: string;
}

export interface RecordDataResponse {
  TRACE_ID: string;
  CRON_JOB_ID: string;
  custUid: string,
  custName: string,
  [key: string]: unknown;
}

export interface ClawFilePlanItem {
  skillId: string;
  skillName: string;
  templateId: number;
  resultId: string;
  sortOrder: number;
  key?: string;
}

export interface ClawFilePlanResponse {
  data: ClawFilePlanItem[];
}

// Dynamic Render API
export const dynamicRenderApi = {
  getTemplateList: () =>
    request<TemplateListResponse>("/template/file-templates", {
      method: "GET",
    }),

  getTemplateContent: (fileName: string) =>
    request<TemplateContentResponse>(
      `/assets/text/read?file_name=${fileName}`,
      {
        method: "GET",
      },
    ),

  getRecordData: (resultId: string, templateId: string) =>
    request<RecordDataResponse>("/template/result", {
      method: "POST",
      body: JSON.stringify({
        resultId,
        templateId: parseInt(templateId),
      }),
    }),

  // 通过 idKey 获取 resultId
  queryIdKey: (params: { idKey: string; userId?: string | null; bbkOrgId?: string | null; templateName?: string | null }) =>
    request<Record<string, unknown>>("/template/query-id-key", {
      method: "POST",
      body: JSON.stringify(params),
    }),

  // 获取客户的所有报告模板列表
  getAllClawFilePlan: (params: {
    sapId: string;
    bbkOrgId: string;
    custUid: string;
    pageSource: string;
    platformSource: string;
    actvId?: string;
    actvTyp?: string;
  }) =>
    request<ClawFilePlanResponse>("/wpclaw/get-all-claw-file-plan", {
      method: "POST",
      body: JSON.stringify(params),
    }),
};