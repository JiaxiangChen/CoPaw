import { request } from "../request";
import { buildAuthHeaders } from "../authHeaders";
import type { ChatArchivePage, ChatHistory, ChatSpec } from "../types";
import type { FeedbackRecord } from "../types/feedback";

// Types
export interface OverviewStats {
  online_users: number;
  online_user_ids: string[];
  total_users: number;
  it_users: number;  // IT人员数量（80/IT开头的用户ID）
  business_users: number;  // 业务人员数量（其他用户）
  model_distribution: ModelUsage[];
  total_tokens: number;
  input_tokens: number;
  output_tokens: number;
  total_sessions: number;
  total_conversations: number;
  total_skill_calls: number;  // 技能调用总次数
  // 客户点击统计
  plan_customers: number;      // 查看方案客户数
  insight_customers: number;   // 去洞察客户数
  phone_customers: number;     // 去电访客户数
  avg_duration_ms: number;
  top_tools: ToolUsage[];
  top_skills: SkillUsage[];
  top_mcp_tools: MCPToolUsage[];
  mcp_servers: MCPServerUsage[];
  daily_trend: DailyStats[];
  growth_stats?: {
    callsGrowth?: number | null;
    tokensGrowth?: number | null;
    sessionGrowth?: number | null;
    userGrowth?: number | null;
    cronGrowth?: number | null;
    planCustomersGrowth?: number | null;
  };
  branch_breakdown: OverviewBranchBreakdown;
}

export interface BranchMetricItem {
  bbk_id: string;
  bbk_name: string;
  value: number;
  percent: number;
}

export interface OverviewBranchBreakdown {
  users: BranchMetricItem[];
  conversations: BranchMetricItem[];
  sessions: BranchMetricItem[];
  tokens: BranchMetricItem[];
  skills: BranchMetricItem[];
  cron_tasks: BranchMetricItem[];
  customers: BranchMetricItem[];
}

export interface TaskStatusBreakdown {
  success: number;
  failed: number;
  running: number;
}

export interface TaskStatusSummary {
  total_tasks: number;
  success: number; // status='success' AND async_status='success'
  running: number; // status='success' AND async_status IS NULL
  failed: number;
  cancelled: number;
  read_count: number;
}

export interface ModelUsage {
  model_name: string;
  count: number;
  total_tokens: number;
  input_tokens: number;
  output_tokens: number;
}

export interface ToolUsage {
  tool_name: string;
  count: number;
  avg_duration_ms: number;
  error_count: number;
}

export interface SkillUsage {
  skill_name: string;
  skill_id?: string;
  cn_name?: string;
  skill_description?: string;
  count: number;
  avg_duration_ms: number;
}

/** 技能展示名优先返回中文 cn_name（自动 trim），回退原始 skill_name. */
export const displaySkillName = (skill: SkillUsage): string => {
  const cn = skill.cn_name?.trim();
  return cn || skill.skill_name;
};

export interface MCPToolUsage {
  tool_name: string;
  mcp_server: string;
  count: number;
  avg_duration_ms: number;
  error_count: number;
}

export interface MCPServerUsage {
  server_name: string;
  tool_count: number;
  total_calls: number;
  avg_duration_ms: number;
  error_count: number;
  tools: MCPToolUsage[];
}

export interface MCPSummary {
  total_calls: number;
  error_count: number;
  server_count: number;
}

export interface ErrorSummary {
  total_errors: number;
  model_errors: number;
  tool_errors: number;
  model_error_codes: ModelErrorCodeCount[];
}

export interface ModelErrorCodeCount {
  code: string;
  count: number;
}

export interface ErrorItem {
  trace_id: string;
  span_id: string;
  event_type: string;
  error: string;
  user_id: string;
  user_name?: string;
  bbk_id?: string;
  session_id: string;
  session_name?: string;
  model_name?: string;
  tool_name?: string;
  mcp_server?: string;
  start_time: string;
  duration_ms?: number;
  input_tokens?: number;
  output_tokens?: number;
}

export interface ErrorListResponse {
  items: ErrorItem[];
  total: number;
}

export interface DailyStats {
  date: string;
  total_users: number;
  active_users: number;
  total_tokens: number;
  session_count: number;
}

export interface UserStats {
  user_id: string;
  model_usage: ModelUsage[];
  total_tokens: number;
  input_tokens: number;
  output_tokens: number;
  total_sessions: number;
  total_conversations: number;
  avg_duration_ms: number;
  tools_used: ToolUsage[];
  skills_used: SkillUsage[];
  mcp_tools_used: MCPToolUsage[];
}

export interface UserListItem {
  user_id: string;
  user_name?: string;
  bbk_id?: string;
  total_sessions: number;
  total_conversations: number;
  total_tokens: number;
  last_active: string | null;
  // 四种口径统计字段
  manual_calls: number;
  cron_executions: number;
  cron_success: number;
  cron_reads: number;
}

export interface TraceListItem {
  trace_id: string;
  user_id: string;
  user_name?: string;
  bbk_id?: string;
  session_id: string;
  channel: string;
  start_time: string;
  duration_ms: number | null;
  total_tokens: number;
  total_input_tokens: number;
  total_output_tokens: number;
  model_name: string | null;
  status: string;
  skills_count: number;
  feedback?: FeedbackRecord | null;
  feedback_content?: string | null;
  feedback_updated_at?: string | null;
}

export interface SessionListItem {
  session_id: string;
  session_name?: string;
  user_id: string;
  user_name?: string;
  bbk_id?: string;
  channel: string;
  total_traces: number;
  total_tokens: number;
  total_skills: number;
  first_active: string | null;
  last_active: string | null;
}

export type SessionResourceFilter =
  | { type: "model"; name: string }
  | { type: "skill"; name: string }
  | { type: "mcp_tool"; name: string; mcp_server: string };

export interface SessionStats {
  session_id: string;
  user_id: string;
  channel: string;
  model_usage: ModelUsage[];
  total_tokens: number;
  input_tokens: number;
  output_tokens: number;
  total_traces: number;
  avg_duration_ms: number;
  tools_used: ToolUsage[];
  skills_used: SkillUsage[];
  mcp_tools_used: MCPToolUsage[];
  first_active: string | null;
  last_active: string | null;
}

export interface TraceDetail {
  trace: Trace;
  feedback?: FeedbackRecord | null;
  spans: Span[];
  llm_duration_ms: number;
  tool_duration_ms: number;
  tools_called: ToolCall[];
}

export interface SessionRoundTrace {
  trace_id: string;
  round_number: number;
  start_time: string;
  duration_ms: number | null;
  model_name: string | null;
  status: string;
  user_message: string | null;
  model_output: string | null;
  input_tokens: number;
  output_tokens: number;
  tools_used: string[];
  error: string | null;
  is_error_round: boolean;
}

export interface SessionTracesResponse {
  session_id: string;
  session_name: string | null;
  user_id: string;
  user_name: string | null;
  traces: SessionRoundTrace[];
  total_rounds: number;
  error_round_index: number | null;
}

export interface Trace {
  trace_id: string;
  user_id: string;
  user_name?: string;
  bbk_id?: string;
  session_id: string;
  session_name?: string;
  channel: string;
  start_time: string;
  end_time: string | null;
  duration_ms: number | null;
  model_name: string | null;
  total_input_tokens: number;
  total_output_tokens: number;
  tools_used: string[];
  skills_used: string[];
  status: string;
  error: string | null;
  user_message: string | null;
  model_output: string | null;
}

export interface Span {
  span_id: string;
  trace_id: string;
  name: string;
  event_type: string;
  start_time: string;
  end_time: string | null;
  duration_ms: number | null;
  model_name: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  tool_name: string | null;
  skill_name: string | null;
  tool_input: Record<string, unknown> | null;
  tool_output: string | null;
  error: string | null;
  user_name?: string;
  bbk_id?: string;
}

export interface ToolCall {
  tool_name: string;
  tool_input: Record<string, unknown> | null;
  tool_output: string | null;
  duration_ms: number | null;
  error: string | null;
}

// Timeline types for hierarchical display
export interface ToolCallInSkill {
  span_id: string;
  tool_name: string;
  mcp_server: string | null;
  start_time: string;
  end_time: string | null;
  duration_ms: number;
  status: string;
  error: string | null;
  skill_weight: number | null;
}

export interface SkillCallTimeline {
  span_id: string;
  skill_name: string;
  start_time: string;
  end_time: string | null;
  duration_ms: number;
  confidence: number;
  trigger_reason: string;
  tools: ToolCallInSkill[];
  total_tool_calls: number;
  tool_duration_ms: number;
}

export interface TimelineEvent {
  event_type: string;
  span_id: string | null;
  start_time: string;
  end_time: string | null;
  duration_ms: number;
  skill_name: string | null;
  confidence: number | null;
  trigger_reason: string | null;
  tool_name: string | null;
  mcp_server: string | null;
  model_name: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  children: TimelineEvent[];
}

export interface TraceDetailWithTimeline {
  trace: Trace;
  feedback?: FeedbackRecord | null;
  spans: Span[];
  timeline: TimelineEvent[];
  skill_invocations: SkillCallTimeline[];
  llm_duration_ms: number;
  tool_duration_ms: number;
  skill_duration_ms: number;
  total_skills: number;
  total_tools: number;
  total_llm_calls: number;
}

export interface UserMessageItem {
  trace_id: string;
  user_id: string;
  user_name?: string;
  bbk_id?: string;
  session_id: string;
  channel: string;
  user_message: string | null;
  model_name: string | null;
  start_time: string;
  duration_ms: number | null;
}

// API functions
export const tracingApi = {
  getOverview: async (
    startDate?: string,
    endDate?: string,
    bbkIds?: string,
    options?: {
      detail?: "full" | "summary";
      timeRange?: "day" | "week" | "month" | "custom";
    },
  ): Promise<OverviewStats> => {
    const params = new URLSearchParams();
    if (startDate) params.append("start_date", startDate);
    if (endDate) params.append("end_date", endDate);
    if (bbkIds) params.append("bbk_ids", bbkIds);
    if (options?.detail) params.append("detail", options.detail);
    if (options?.timeRange) params.append("time_range", options.timeRange);
    return request(`/monitor/tracing/overview?${params.toString()}`);
  },

  getUsers: async (
    page = 1,
    pageSize = 20,
    filters?: {
      user_id?: string;
      start_date?: string;
      end_date?: string;
      sort_by?: string;
      filter_user_type?: string;
      bbk_ids?: string;
      metric_type?: string;
    },
  ): Promise<{
    items: UserListItem[];
    total: number;
    page: number;
    page_size: number;
  }> => {
    const params = new URLSearchParams();
    params.append("page", page.toString());
    params.append("page_size", pageSize.toString());
    if (filters) {
      Object.entries(filters).forEach(([key, value]) => {
        // filter_user_type 需要传递 "all" 或 "filtered"
        if (key === "filter_user_type") {
          if (value) params.append(key, value);
        } else if (key === "metric_type") {
          // metric_type 需要传递口径类型
          if (value) params.append(key, value);
        } else if (value && value !== "all") {
          params.append(key, value);
        }
      });
    }
    return request(`/monitor/tracing/users?${params.toString()}`);
  },

  getUserStats: async (
    userId: string,
    startDate?: string,
    endDate?: string,
    bbkIds?: string,
  ): Promise<UserStats> => {
    const params = new URLSearchParams();
    if (startDate) params.append("start_date", startDate);
    if (endDate) params.append("end_date", endDate);
    if (bbkIds) params.append("bbk_ids", bbkIds);
    const query = params.toString() ? `?${params.toString()}` : "";
    return request(
      `/monitor/tracing/users/${encodeURIComponent(userId)}${query}`,
    );
  },

  getUserChats: async (
    userId: string,
    channel?: string,
  ): Promise<ChatSpec[]> => {
    const params = new URLSearchParams();
    params.append("user_id", userId);
    if (channel) params.append("channel", channel);
    const data = await request<unknown>(`/tracing/chats?${params.toString()}`);
    return Array.isArray(data) ? (data as ChatSpec[]) : [];
  },

  getUserChat: async (
    userId: string,
    chatId: string,
  ): Promise<ChatHistory> => {
    const params = new URLSearchParams();
    params.append("user_id", userId);
    return request<ChatHistory>(
      `/tracing/chats/${encodeURIComponent(chatId)}?${params.toString()}`,
    );
  },

  getUserChatHistory: async (
    userId: string,
    chatId: string,
    before?: string | null,
    limit = 20,
  ): Promise<ChatArchivePage> => {
    const params = new URLSearchParams({ user_id: userId, limit: String(limit) });
    if (before) params.set("before", before);
    return request<ChatArchivePage>(
      `/tracing/chats/${encodeURIComponent(chatId)}/history?${params.toString()}`,
    );
  },

  getTraces: async (
    page = 1,
    pageSize = 20,
    filters?: {
      user_id?: string;
      session_id?: string;
      status?: string;
      start_date?: string;
      end_date?: string;
      bbk_ids?: string;
      has_feedback?: boolean;
    },
  ): Promise<{
    items: TraceListItem[];
    total: number;
    page: number;
    page_size: number;
  }> => {
    const params = new URLSearchParams();
    params.append("page", page.toString());
    params.append("page_size", pageSize.toString());
    if (filters) {
      Object.entries(filters).forEach(([key, value]) => {
        if (value !== undefined && value !== "") {
          params.append(key, String(value));
        }
      });
    }
    return request(`/monitor/tracing/traces?${params.toString()}`);
  },

  getTraceDetail: async (traceId: string): Promise<TraceDetail> => {
    return request(`/monitor/tracing/traces/${traceId}`);
  },

  getSessionTraces: async (
    sessionId: string,
    errorTraceId?: string,
  ): Promise<SessionTracesResponse> => {
    const params = new URLSearchParams();
    if (errorTraceId) params.append("error_trace_id", errorTraceId);
    const query = params.toString() ? `?${params.toString()}` : "";
    return request(
      `/monitor/tracing/session/${encodeURIComponent(sessionId)}/traces${query}`,
    );
  },

  getModelUsage: async (
    startDate?: string,
    endDate?: string,
  ): Promise<{ models: ModelUsage[] }> => {
    const params = new URLSearchParams();
    if (startDate) params.append("start_date", startDate);
    if (endDate) params.append("end_date", endDate);
    const query = params.toString() ? `?${params.toString()}` : "";
    return request(`/monitor/tracing/models${query}`);
  },

  getToolUsage: async (
    startDate?: string,
    endDate?: string,
  ): Promise<{ tools: ToolUsage[] }> => {
    const params = new URLSearchParams();
    if (startDate) params.append("start_date", startDate);
    if (endDate) params.append("end_date", endDate);
    const query = params.toString() ? `?${params.toString()}` : "";
    return request(`/monitor/tracing/tools${query}`);
  },

  getSessions: async (
    page = 1,
    pageSize = 20,
    filters?: {
      user_id?: string;
      session_id?: string;
      start_date?: string;
      end_date?: string;
      bbk_ids?: string;
      has_error?: boolean;
      resource_type?: SessionResourceFilter["type"];
      resource_name?: string;
      mcp_server?: string;
    },
  ): Promise<{
    items: SessionListItem[];
    total: number;
    page: number;
    page_size: number;
  }> => {
    const params = new URLSearchParams();
    params.append("page", page.toString());
    params.append("page_size", pageSize.toString());
    if (filters) {
      Object.entries(filters).forEach(([key, value]) => {
        if (value !== undefined && value !== null && value !== "") {
          params.append(key, String(value));
        }
      });
    }
    return request(`/monitor/tracing/sessions?${params.toString()}`);
  },

  getSessionStats: async (
    sessionId: string,
    startDate?: string,
    endDate?: string,
    bbkIds?: string,
  ): Promise<SessionStats> => {
    const params = new URLSearchParams();
    if (startDate) params.append("start_date", startDate);
    if (endDate) params.append("end_date", endDate);
    if (bbkIds) params.append("bbk_ids", bbkIds);
    const query = params.toString() ? `?${params.toString()}` : "";
    return request(
      `/monitor/tracing/sessions/${encodeURIComponent(sessionId)}${query}`,
    );
  },

  getUserMessages: async (
    page = 1,
    pageSize = 20,
    filters?: {
      user_id?: string;
      session_id?: string;
      start_date?: string;
      end_date?: string;
      query?: string;
      bbk_ids?: string;
      exclude_cron_task_sessions?: boolean;
    },
  ): Promise<{
    items: UserMessageItem[];
    total: number;
    page: number;
    page_size: number;
  }> => {
    const params = new URLSearchParams();
    params.append("page", page.toString());
    params.append("page_size", pageSize.toString());
    if (filters) {
      Object.entries(filters).forEach(([key, value]) => {
        if (value) params.append(key, String(value));
      });
    }
    return request(`/monitor/tracing/user-messages?${params.toString()}`);
  },

  exportUserMessages: async (
    filters?: {
      user_id?: string;
      session_id?: string;
      start_date?: string;
      end_date?: string;
      query?: string;
      bbk_ids?: string;
      exclude_cron_task_sessions?: boolean;
    },
    format: string = "xlsx",
  ): Promise<Blob> => {
    const params = new URLSearchParams();
    params.append("format", format);
    if (filters) {
      Object.entries(filters).forEach(([key, value]) => {
        if (value) params.append(key, String(value));
      });
    }
    // Use the proper API URL and include authorization token
    const { getApiUrl } = await import("../config");
    const url = getApiUrl(
      `/monitor/tracing/user-messages/export?${params.toString()}`,
    );
    const headers = new Headers(buildAuthHeaders());
    const response = await fetch(url, { headers });
    if (!response.ok) {
      // Try to parse error message from response
      let errorMessage = `Export failed: ${response.status} ${response.statusText}`;
      try {
        const errorData = await response.json();
        console.error("Export error response:", errorData);
        if (errorData.detail) {
          errorMessage = errorData.detail;
        }
      } catch {
        // Ignore JSON parse error
      }
      throw new Error(errorMessage);
    }
    return response.blob();
  },

  // Timeline with skill hierarchy
  getTraceTimeline: async (
    traceId: string,
  ): Promise<TraceDetailWithTimeline> => {
    return request(`/monitor/tracing/traces/${traceId}/timeline`);
  },

  // Business Overview APIs
  getSources: async (
    startDate?: string,
    endDate?: string,
  ): Promise<{ sources: string[] }> => {
    const params = new URLSearchParams();
    if (startDate) params.append("start_date", startDate);
    if (endDate) params.append("end_date", endDate);
    const query = params.toString() ? `?${params.toString()}` : "";
    return request(`/monitor/tracing/sources${query}`);
  },

  getChannelDistribution: async (
    startDate?: string,
    endDate?: string,
  ): Promise<{
    platformUserDistribution: { name: string; value: number }[];
    platformCallDistribution: { name: string; value: number }[];
    totalPlatforms: number;
  }> => {
    const params = new URLSearchParams();
    if (startDate) params.append("start_date", startDate);
    if (endDate) params.append("end_date", endDate);
    const query = params.toString() ? `?${params.toString()}` : "";
    return request(`/monitor/tracing/channel-distribution${query}`);
  },

  getDailyTrend: async (
    startDate?: string,
    endDate?: string,
    bbkIds?: string,
  ): Promise<{
    trendData: {
      date: string;
      calls: number;
      tokens: number;
      users: number;
      read_tasks: number;
      plan_customers: number;
      insight_customers: number;
      phone_customers: number;
    }[];
  }> => {
    const params = new URLSearchParams();
    if (startDate) params.append("start_date", startDate);
    if (endDate) params.append("end_date", endDate);
    if (bbkIds) params.append("bbk_ids", bbkIds);
    const query = params.toString() ? `?${params.toString()}` : "";
    return request(`/monitor/tracing/daily-trend${query}`);
  },

  // 技能调用排行榜（分页）
  getHourlyTrend: async (
    startDate?: string,
    endDate?: string,
    bbkIds?: string,
  ): Promise<{
    trendData: {
      date: string;
      calls: number;
      tokens: number;
      users: number;
      read_tasks: number;
      plan_customers: number;
      insight_customers: number;
      phone_customers: number;
    }[];
  }> => {
    const params = new URLSearchParams();
    if (startDate) params.append("start_date", startDate);
    if (endDate) params.append("end_date", endDate);
    if (bbkIds) params.append("bbk_ids", bbkIds);
    const query = params.toString() ? `?${params.toString()}` : "";
    return request(`/monitor/tracing/hourly-trend${query}`);
  },

  getSkills: async (
    page = 1,
    pageSize = 10,
    filters?: {
      start_date?: string;
      end_date?: string;
      bbk_ids?: string;
    },
  ): Promise<{
    items: SkillUsage[];
    total: number;
    page: number;
    page_size: number;
  }> => {
    const params = new URLSearchParams();
    params.append("page", page.toString());
    params.append("page_size", pageSize.toString());
    if (filters) {
      Object.entries(filters).forEach(([key, value]) => {
        if (value) params.append(key, value);
      });
    }
    return request(`/monitor/tracing/skills?${params.toString()}`);
  },

  // 技能调用的对话列表（分页）
  getSkillTraces: async (
    skillName: string,
    page = 1,
    pageSize = 20,
    filters?: {
      start_date?: string;
      end_date?: string;
      bbk_ids?: string;
    },
  ): Promise<{
    items: TraceListItem[];
    total: number;
    page: number;
    page_size: number;
  }> => {
    const params = new URLSearchParams();
    params.append("page", page.toString());
    params.append("page_size", pageSize.toString());
    if (filters) {
      Object.entries(filters).forEach(([key, value]) => {
        if (value) params.append(key, value);
      });
    }
    return request(
      `/monitor/tracing/skills/${encodeURIComponent(
        skillName,
      )}/traces?${params.toString()}`,
    );
  },

  // MCP 服务调用排行榜（分页）
  getMCPServers: async (
    page = 1,
    pageSize = 10,
    filters?: {
      start_date?: string;
      end_date?: string;
      bbk_ids?: string;
    },
  ): Promise<{
    items: MCPServerUsage[];
    total: number;
    page: number;
    page_size: number;
  }> => {
    const params = new URLSearchParams();
    params.append("page", page.toString());
    params.append("page_size", pageSize.toString());
    if (filters) {
      Object.entries(filters).forEach(([key, value]) => {
        if (value) params.append(key, value);
      });
    }
    return request(`/monitor/tracing/mcp?${params.toString()}`);
  },

  // MCP 全局调用汇总统计
  getMCPSummary: async (
    filters?: {
      start_date?: string;
      end_date?: string;
      bbk_ids?: string;
    },
  ): Promise<MCPSummary> => {
    const params = new URLSearchParams();
    if (filters) {
      Object.entries(filters).forEach(([key, value]) => {
        if (value) params.append(key, value);
      });
    }
    const query = params.toString() ? `?${params.toString()}` : "";
    return request(`/monitor/tracing/mcp/summary${query}`);
  },

  // 定时任务执行汇总统计
  getTaskStatusSummary: async (
    filters?: {
      start_date?: string;
      end_date?: string;
      bbk_ids?: string;
    },
  ): Promise<TaskStatusSummary> => {
    const params = new URLSearchParams();
    if (filters) {
      Object.entries(filters).forEach(([key, value]) => {
        if (value) params.append(key, value);
      });
    }
    const query = params.toString() ? `?${params.toString()}` : "";
    return request(`/monitor/tracing/task-status/summary${query}`);
  },

  // 报错分析汇总统计
  getErrorSummary: async (
    filters?: {
      start_date?: string;
      end_date?: string;
      bbk_ids?: string;
    },
  ): Promise<ErrorSummary> => {
    const params = new URLSearchParams();
    if (filters) {
      Object.entries(filters).forEach(([key, value]) => {
        if (value) params.append(key, value);
      });
    }
    const query = params.toString() ? `?${params.toString()}` : "";
    return request(`/monitor/tracing/error/summary${query}`);
  },

  getErrorList: async (
    page: number = 1,
    pageSize: number = 10,
    filters?: {
      start_date?: string;
      end_date?: string;
      bbk_ids?: string;
      error_type?: string;
      search?: string;
    },
  ): Promise<ErrorListResponse> => {
    const params = new URLSearchParams();
    params.append("page", String(page));
    params.append("page_size", String(pageSize));
    if (filters) {
      Object.entries(filters).forEach(([key, value]) => {
        if (value) params.append(key, value);
      });
    }
    const query = params.toString() ? `?${params.toString()}` : "";
    return request(`/monitor/tracing/error/list${query}`);
  },
};
