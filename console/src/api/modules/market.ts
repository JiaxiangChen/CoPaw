import { request } from "../request";
import { mergeHeaders } from "../mergeHeaders";
import { getApiUrl } from "../config";
import type { FileContentResponse, FileTreeNode, MySkill } from "./mySkills";
import type { DistributionRecord, RecallResponse } from "../types";

export interface MarketSkill {
  item_id: string;
  skill_id?: string | null;
  name: string;
  skill_name?: string;
  chinese_name?: string;
  description: string;
  version: string;
  creator_id: string;
  creator_name: string;
  category_id: number | null;
  bbk_ids: string[];
  status: "active" | "inactive";
  created_at: string | null;
  updated_at: string | null;
  call_count: number;
  user_count: number;
  version_unchanged?: boolean;
  // 新增字段：是否纳入统计
  include_in_statistics?: boolean;
}

export interface MarketSkillDetail extends MarketSkill {
  user_stats: Array<{
    user_id: string;
    user_name: string;
    call_count: number;
  }>;
}

export interface MarketExpert {
  item_id: string;
  name: string;
  description: string;
  version: string;
  creator_id: string;
  creator_name: string;
  category_id: number | null;
  bbk_ids: string[];
  status: "active" | "inactive";
  created_at: string | null;
  updated_at: string | null;
  version_unchanged?: boolean;
}

export interface MarketExpertDetail extends MarketExpert {
  versions: ExpertVersion[];
  definition: Record<string, unknown>;
}

export interface ExpertVersion {
  version_id: string;
  created_at: string;
  created_by: string;
  created_by_name: string;
  description: string;
  signature: string;
  is_current: boolean;
  is_initial: boolean;
}

export interface ExpertVersions {
  expert_name: string;
  versions: ExpertVersion[];
}

export interface ExpertOperationResult {
  user_id: string;
  success: boolean;
  definition_id?: string | null;
  reason?: string | null;
}

export interface ExpertDistributionRequest {
  target_type: "all" | "bbk_id" | "user_id";
  target_values: string[];
}

export interface ExpertDistributionResponse {
  item_id: string;
  distributed_count: number;
  conflict_count: number;
  results: ExpertOperationResult[];
}

export interface ExpertRecallResponse {
  item_id: string;
  recalled_count: number;
  failed_count: number;
  results: ExpertOperationResult[];
}

export interface PublishExpertRequest {
  definition_id: string;
  agent_id: string;
  category_id?: number;
  bbk_ids: string[];
  overwrite: boolean;
}

// 更新统计配置请求
export interface UpdateStatisticsConfigRequest {
  include_in_statistics: boolean;
}

// 更新统计配置响应
export interface UpdateStatisticsConfigResponse {
  success: boolean;
  message?: string;
}

// 用户技能状态
export interface UserSkillStatus {
  tenant_id: string;
  tenant_name: string | null;
  bbk_id: string | null;
  status: "first_time" | "update" | "conflict";
  current_version?: string;
}

// 分发预览响应
export interface DistributionPreviewResponse {
  skill_version: string;
  users: UserSkillStatus[];
  distributed_user_ids: string[];
}

export interface Category {
  id: number;
  source_id: string;
  name: string;
  sort_order: number;
}

export interface PublishSkillRequest {
  name: string;
  description: string;
  creator_id: string;
  creator_name: string;
  category_id?: number;
  bbk_ids?: string[];
  skill_json: Record<string, unknown>;
  skill_md?: string;
  // 可选：指定用户技能目录名，用于同步整个目录
  skill_name?: string;
  agent_id?: string;
  overwrite?: boolean;
  // 用户工作区版本号，用于版本快照的 source_user_version
  source_user_version?: string;
  // 同步模式：直接传递用户已有的 skill_id 和 cn_name，无需再解析
  skill_id?: string;
  cn_name?: string;
  // 是否纳入统计
  include_in_statistics?: boolean;
}

export interface DistributeRequest {
  target_type: "all" | "bbk_id" | "user_id";
  target_values: string[];
}

export interface DistributeConflictItem {
  user_id: string;
  skill_name: string;
  reason: string;
}

export interface DistributeResponse {
  task_id: string;
  status: string;
  reused?: boolean;
  distributed_count?: number;
  conflict_count?: number;
  conflicts?: DistributeConflictItem[];
  item_id?: string;
}

export interface DownloadBinaryResponse {
  blob: Blob;
  filename: string | null;
}

function _extractFilenameFromDisposition(
  disposition: string | null,
): string | null {
  if (!disposition) return null;
  const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8Match?.[1]) {
    try {
      return decodeURIComponent(utf8Match[1]);
    } catch {
      return utf8Match[1];
    }
  }
  const plainMatch = disposition.match(/filename="?([^"]+)"?/i);
  return plainMatch?.[1] ?? null;
}

async function _downloadBinary(
  path: string,
  options: RequestInit,
): Promise<DownloadBinaryResponse> {
  const response = await fetch(getApiUrl(path), options);
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return {
    blob: await response.blob(),
    filename: _extractFilenameFromDisposition(
      response.headers.get("content-disposition"),
    ),
  };
}

/**
 * Upload a skill zip file to workspace (market service)
 */
async function _uploadZipToMarket(
  endpoint: string,
  file: File,
  headers: Record<string, string>,
  options?: {
    enable?: boolean;
    overwrite?: boolean;
    target_name?: string;
    rename_map?: Record<string, string>;
    category_id?: number;
    cn_name?: string;
    skill_id?: string;
    bbk_ids?: string[];
    include_in_statistics?: boolean;
  },
): Promise<Record<string, unknown>> {
  const formData = new FormData();
  formData.append("file", file);

  const params = new URLSearchParams();
  if (options?.enable !== undefined) {
    params.set("enable", String(options.enable));
  }
  if (options?.overwrite !== undefined) {
    params.set("overwrite", String(options.overwrite));
  }
  if (options?.target_name) {
    params.set("target_name", options.target_name);
  }
  if (options?.rename_map && Object.keys(options.rename_map).length) {
    params.set("rename_map", JSON.stringify(options.rename_map));
  }
  if (options?.category_id !== undefined) {
    params.set("category_id", String(options.category_id));
  }
  if (options?.cn_name) {
    params.set("cn_name", options.cn_name);
  }
  if (options?.skill_id) {
    params.set("skill_id", options.skill_id);
  }
  if (options?.bbk_ids && options.bbk_ids.length > 0) {
    params.set("bbk_ids", options.bbk_ids.join(","));
  }
  if (options?.include_in_statistics !== undefined) {
    params.set("include_in_statistics", String(options.include_in_statistics));
  }
  const qs = params.toString();
  const url = getApiUrl(`${endpoint}${qs ? `?${qs}` : ""}`);

  const response = await fetch(url, {
    method: "POST",
    headers: new Headers(headers),
    body: formData,
  });

  if (!response.ok) {
    throw new Error(await response.text());
  }

  return await response.json();
}

export const marketApi = {
  listCategories: async (sourceId: string): Promise<Category[]> => {
    const opts = mergeHeaders({ "X-Source-Id": sourceId });
    return request<Category[]>("/market/categories", opts);
  },

  createCategory: async (sourceId: string, name: string): Promise<Category> => {
    const opts: RequestInit = {
      method: "POST",
      ...mergeHeaders({
        "Content-Type": "application/json",
        "X-Source-Id": sourceId,
      }),
      body: JSON.stringify({ name }),
    };
    return request<Category>("/market/categories", opts);
  },

  listMarketSkills: async (
    sourceId: string,
    categoryId?: number,
    bbkIds?: string,
  ): Promise<MarketSkill[]> => {
    let url = "/market/skills";
    const params = new URLSearchParams();
    if (categoryId !== undefined) {
      params.append("category_id", String(categoryId));
    }
    if (bbkIds !== undefined && bbkIds !== null) {
      params.append("bbk_ids", bbkIds);
    }
    if (params.toString()) {
      url += `?${params.toString()}`;
    }
    const opts = mergeHeaders({ "X-Source-Id": sourceId });
    return request<MarketSkill[]>(url, opts);
  },

  listMarketExperts: async (
    sourceId: string,
    options?: { categoryId?: number; bbkIds?: string[] },
  ): Promise<MarketExpert[]> => {
    const params = new URLSearchParams();
    if (options?.categoryId !== undefined) {
      params.set("category_id", String(options.categoryId));
    }
    if (options?.bbkIds?.length) {
      params.set("bbk_ids", options.bbkIds.join(","));
    }
    const query = params.toString();
    const opts = mergeHeaders({ "X-Source-Id": sourceId });
    return request<MarketExpert[]>(
      `/market/experts${query ? `?${query}` : ""}`,
      opts,
    );
  },

  publishExpert: async (
    sourceId: string,
    data: PublishExpertRequest,
  ): Promise<MarketExpert> => {
    return request<MarketExpert>("/market/experts", {
      method: "POST",
      ...mergeHeaders({
        "Content-Type": "application/json",
        "X-Source-Id": sourceId,
        "X-Manager": "true",
      }),
      body: JSON.stringify(data),
    });
  },

  getMarketExpert: async (
    sourceId: string,
    itemId: string,
  ): Promise<MarketExpertDetail> => {
    const opts = mergeHeaders({ "X-Source-Id": sourceId });
    return request<MarketExpertDetail>(`/market/experts/${itemId}`, opts);
  },

  listExpertVersions: async (
    sourceId: string,
    itemId: string,
  ): Promise<ExpertVersions> => {
    return request<ExpertVersions>(
      `/market/experts/${itemId}/versions`,
      mergeHeaders({ "X-Source-Id": sourceId }),
    );
  },

  restoreExpertVersion: async (
    sourceId: string,
    itemId: string,
    versionId: string,
  ): Promise<MarketExpert> => {
    return request<MarketExpert>(
      `/market/experts/${itemId}/versions/${encodeURIComponent(
        versionId,
      )}/restore`,
      {
        method: "POST",
        ...mergeHeaders({
          "X-Source-Id": sourceId,
          "X-Manager": "true",
        }),
      },
    );
  },

  installExpert: async (
    sourceId: string,
    itemId: string,
    userId: string,
    agentId: string,
  ): Promise<ExpertOperationResult> => {
    return request<ExpertOperationResult>(`/market/experts/${itemId}/install`, {
      method: "POST",
      ...mergeHeaders({
        "Content-Type": "application/json",
        "X-Source-Id": sourceId,
        "X-User-Id": userId,
      }),
      body: JSON.stringify({ agent_id: agentId }),
    });
  },

  distributeExpert: async (
    sourceId: string,
    itemId: string,
    data: ExpertDistributionRequest,
  ): Promise<ExpertDistributionResponse> => {
    return request<ExpertDistributionResponse>(
      `/market/experts/${itemId}/distribute`,
      {
        method: "POST",
        ...mergeHeaders({
          "Content-Type": "application/json",
          "X-Source-Id": sourceId,
          "X-Manager": "true",
        }),
        body: JSON.stringify(data),
      },
    );
  },

  getExpertDistributions: async (
    sourceId: string,
    itemId: string,
  ): Promise<DistributionRecord[]> => {
    return request<DistributionRecord[]>(
      `/market/experts/${itemId}/distributions`,
      mergeHeaders({ "X-Source-Id": sourceId, "X-Manager": "true" }),
    );
  },

  recallExpert: async (
    sourceId: string,
    itemId: string,
    targetUserIds?: string[],
  ): Promise<ExpertRecallResponse> => {
    return request<ExpertRecallResponse>(`/market/experts/${itemId}/recall`, {
      method: "POST",
      ...mergeHeaders({
        "Content-Type": "application/json",
        "X-Source-Id": sourceId,
        "X-Manager": "true",
      }),
      body: JSON.stringify({ target_user_ids: targetUserIds }),
    });
  },

  unpublishExpert: async (sourceId: string, itemId: string): Promise<void> => {
    const opts: RequestInit = {
      method: "DELETE",
      ...mergeHeaders({ "X-Source-Id": sourceId, "X-Manager": "true" }),
    };
    return request<void>(`/market/experts/${itemId}`, opts);
  },

  getSkillDetail: async (
    sourceId: string,
    itemId: string,
  ): Promise<MarketSkillDetail | null> => {
    const opts = mergeHeaders({ "X-Source-Id": sourceId });
    return request<MarketSkillDetail | null>(`/market/skills/${itemId}`, opts);
  },

  downloadSkill: async (
    sourceId: string,
    itemId: string,
  ): Promise<DownloadBinaryResponse> => {
    const opts = mergeHeaders({ "X-Source-Id": sourceId });
    return _downloadBinary(`/market/skills/${itemId}/download`, {
      method: "GET",
      headers: opts.headers,
    });
  },

  downloadSkillVersion: async (
    sourceId: string,
    itemId: string,
    versionId: string,
  ): Promise<DownloadBinaryResponse> => {
    const opts = mergeHeaders({ "X-Source-Id": sourceId });
    return _downloadBinary(
      `/market/skills/${itemId}/versions/${encodeURIComponent(
        versionId,
      )}/download`,
      {
        method: "GET",
        headers: opts.headers,
      },
    );
  },

  listSkillFiles: async (
    sourceId: string,
    itemId: string,
  ): Promise<FileTreeNode[]> => {
    const opts = mergeHeaders({ "X-Source-Id": sourceId });
    return request<FileTreeNode[]>(`/market/skills/${itemId}/files`, opts);
  },

  readSkillFile: async (
    sourceId: string,
    itemId: string,
    filePath: string,
  ): Promise<FileContentResponse> => {
    const opts = mergeHeaders({ "X-Source-Id": sourceId });
    const encodedPath = filePath
      .split("/")
      .map((segment) => encodeURIComponent(segment))
      .join("/");
    return request<FileContentResponse>(
      `/market/skills/${itemId}/files/${encodedPath}`,
      opts,
    );
  },

  publishSkill: async (
    sourceId: string,
    data: PublishSkillRequest,
  ): Promise<MarketSkill> => {
    const opts: RequestInit = {
      method: "POST",
      ...mergeHeaders({
        "Content-Type": "application/json",
        "X-Source-Id": sourceId,
        "X-Manager": "true",
      }),
      body: JSON.stringify(data),
    };
    return request<MarketSkill>("/market/skills", opts);
  },

  unpublishSkill: async (sourceId: string, itemId: string): Promise<void> => {
    const opts: RequestInit = {
      method: "DELETE",
      ...mergeHeaders({
        "X-Source-Id": sourceId,
        "X-Manager": "true",
      }),
    };
    return request<void>(`/market/skills/${itemId}`, opts);
  },

  deleteSkill: async (sourceId: string, itemId: string): Promise<void> => {
    const opts: RequestInit = {
      method: "DELETE",
      ...mergeHeaders({
        "X-Source-Id": sourceId,
        "X-Manager": "true",
      }),
    };
    return request<void>(`/market/skills/${itemId}/delete`, opts);
  },

  distributeSkill: async (
    sourceId: string,
    itemId: string,
    data: DistributeRequest,
  ): Promise<DistributeResponse> => {
    const opts: RequestInit = {
      method: "POST",
      ...mergeHeaders({
        "Content-Type": "application/json",
        "X-Source-Id": sourceId,
        "X-Manager": "true",
      }),
      body: JSON.stringify(data),
    };
    return request<DistributeResponse>(
      `/market/skills/${itemId}/distribute`,
      opts,
    );
  },

  parseSkillZip: async (
    sourceId: string,
    file: File,
    marketMode?: boolean,
  ): Promise<{
    skill_name?: string;
    cn_name?: string;
    skill_id?: string;
    description?: string;
    exists?: boolean;
    error?: string;
    skill_id_reused?: boolean;
    skill_id_conflict?: string;
    skill_id_used_count?: number;
    skill_id_used_by?: string[];
  }> => {
    const formData = new FormData();
    formData.append("file", file);

    let url = getApiUrl("/market/skills/parse-zip");
    if (marketMode) {
      url += "?market_mode=true";
    }
    const headers = Object.fromEntries(
      (
        mergeHeaders({
          "X-Source-Id": sourceId,
        }).headers as Headers
      ).entries(),
    );

    const response = await fetch(url, {
      method: "POST",
      headers: new Headers(headers),
      body: formData,
    });

    if (!response.ok) {
      const text = await response.text();
      return { error: text };
    }

    return await response.json();
  },

  uploadSkillToWorkspace: async (
    sourceId: string,
    file: File,
    options?: {
      enable?: boolean;
      overwrite?: boolean;
      target_name?: string;
      rename_map?: Record<string, string>;
      category_id?: number;
      cn_name?: string;
    },
  ): Promise<{
    imported: string[];
    count: number;
    enabled: boolean;
    name?: string;
    description?: string;
    skill_id?: string;
    cn_name?: string;
    conflicts?: Array<{
      reason: string;
      skill_name: string;
      original_name?: string;
      suggested_name: string;
    }>;
  }> => {
    const headers = Object.fromEntries(
      (
        mergeHeaders({
          "X-Source-Id": sourceId,
        }).headers as Headers
      ).entries(),
    );
    return _uploadZipToMarket(
      "/market/skills/upload",
      file,
      headers,
      options,
    ) as Promise<{
      imported: string[];
      count: number;
      enabled: boolean;
      name?: string;
      description?: string;
      skill_id?: string;
      cn_name?: string;
      conflicts?: Array<{
        reason: string;
        skill_name: string;
        suggested_name: string;
      }>;
    }>;
  },

  uploadSkillToMarket: async (
    sourceId: string,
    file: File,
    options?: {
      category_id?: number;
      overwrite?: boolean;
      cn_name?: string;
      skill_id?: string;
      bbk_ids?: string[];
      include_in_statistics?: boolean;
    },
  ): Promise<{
    imported: string[];
    count: number;
    enabled: boolean;
    name?: string;
    description?: string;
    skill_id?: string;
    conflicts?: Array<{
      skill_name: string;
      suggested_name: string;
    }>;
    version_unchanged?: boolean;
  }> => {
    const headers = Object.fromEntries(
      (
        mergeHeaders({
          "X-Source-Id": sourceId,
          "X-Manager": "true",
        }).headers as Headers
      ).entries(),
    );
    return _uploadZipToMarket(
      "/market/skills/publish-upload",
      file,
      headers,
      options,
    ) as Promise<{
      imported: string[];
      count: number;
      enabled: boolean;
      name?: string;
      description?: string;
      skill_id?: string;
      conflicts?: Array<{
        skill_name: string;
        suggested_name: string;
      }>;
      version_unchanged?: boolean;
    }>;
  },

  // 查询技能分发记录
  getSkillDistributions: async (
    sourceId: string,
    itemId: string,
    skillName?: string,
  ): Promise<DistributionRecord[]> => {
    const opts = mergeHeaders({
      "X-Source-Id": sourceId,
      "X-Manager": "true",
    });
    const params = skillName
      ? `?skill_name=${encodeURIComponent(skillName)}`
      : "";
    return request<DistributionRecord[]>(
      `/market/skills/${itemId}/distributions${params}`,
      opts,
    );
  },

  // 撤回已分发的技能
  recallSkill: async (
    sourceId: string,
    itemId: string,
    targetUserIds?: string[],
  ): Promise<RecallResponse> => {
    const opts: RequestInit = {
      method: "POST",
      ...mergeHeaders({
        "Content-Type": "application/json",
        "X-Source-Id": sourceId,
        "X-Manager": "true",
      }),
      body: JSON.stringify({ target_user_ids: targetUserIds }),
    };
    return request<RecallResponse>(`/market/skills/${itemId}/recall`, opts);
  },

  // 更新技能中文名
  updateSkillCnName: async (
    sourceId: string,
    itemId: string,
    data: {
      skill_id: string;
      chinese_name: string;
      sync_to_users?: boolean;
      target_user_ids?: string[];
    },
  ): Promise<{
    success: boolean;
    market_updated: boolean;
    synced_users: number;
    skipped_users: number;
    errors: Array<{ user_id: string; reason: string }>;
  }> => {
    const opts: RequestInit = {
      method: "PATCH",
      ...mergeHeaders({
        "Content-Type": "application/json",
        "X-Source-Id": sourceId,
        "X-Manager": "true",
      }),
      body: JSON.stringify(data),
    };
    return request(`/market/skills/${itemId}`, opts);
  },

  listUserMarketSkills: async (
    sourceId: string,
    userId: string,
  ): Promise<MySkill[]> => {
    const headers = {
      "X-Source-Id": sourceId,
      "X-User-Id": userId,
      "X-Tenant-Id": userId,
    };
    const [mine, received] = await Promise.all([
      request<MySkill[]>("/market/skills/mine", mergeHeaders(headers)),
      request<MySkill[]>("/market/skills/received", mergeHeaders(headers)),
    ]);
    const byName = new Map<string, MySkill>();
    for (const skill of [...(mine || []), ...(received || [])]) {
      if (!byName.has(skill.skill_name)) {
        byName.set(skill.skill_name, skill);
      }
    }
    return Array.from(byName.values());
  },

  // 获取分发预览
  getDistributionPreview: async (
    sourceId: string,
    itemId: string,
    tenantIds: string[],
  ): Promise<DistributionPreviewResponse> => {
    const opts: RequestInit = {
      method: "POST",
      ...mergeHeaders({
        "Content-Type": "application/json",
        "X-Source-Id": sourceId,
        "X-Manager": "true",
      }),
      body: JSON.stringify({
        source_id: sourceId,
        tenant_ids: tenantIds,
      }),
    };
    return request<DistributionPreviewResponse>(
      `/market/skills/${itemId}/distribution-preview`,
      opts,
    );
  },

  // 更新技能统计配置
  updateSkillStatisticsConfig: async (
    sourceId: string,
    itemId: string,
    data: UpdateStatisticsConfigRequest,
  ): Promise<UpdateStatisticsConfigResponse> => {
    const opts: RequestInit = {
      method: "PATCH",
      ...mergeHeaders({
        "Content-Type": "application/json",
        "X-Source-Id": sourceId,
        "X-Manager": "true",
      }),
      body: JSON.stringify(data),
    };
    return request<UpdateStatisticsConfigResponse>(
      `/market/skills/${itemId}/statistics`,
      opts,
    );
  },
};
