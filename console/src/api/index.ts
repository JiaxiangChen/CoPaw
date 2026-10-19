export * from "./types";

export { request } from "./request";

export { getApiUrl, getApiToken } from "./config";

import { rootApi } from "./modules/root";
import { channelApi } from "./modules/channel";
import { heartbeatApi } from "./modules/heartbeat";
import { cronJobApi } from "./modules/cronjob";
import { chatApi, sessionApi } from "./modules/chat";
import { envApi } from "./modules/env";
import { providerApi } from "./modules/provider";
import { skillApi } from "./modules/skill";
import { agentApi } from "./modules/agent";
import { agentsApi } from "./modules/agents";
import { workspaceApi } from "./modules/workspace";
import { localModelApi } from "./modules/localModel";
import { mcpApi } from "./modules/mcp";
import { tokenUsageApi } from "./modules/tokenUsage";
import { toolsApi } from "./modules/tools";
import { securityApi } from "./modules/security";
import { userTimezoneApi } from "./modules/userTimezone";
import { instanceApi } from "./modules/instance";
import { marketApi } from "./modules/market";
import { mySkillsApi } from "./modules/mySkills";
import { myMcpApi } from "./modules/myMcp";
import { marketMcpApi } from "./modules/marketMcp";
import { feedbackApi } from "./modules/feedback";
import { approvalApi } from "./modules/approval";
import { htmlPreviewEventsApi } from "./modules/htmlPreviewEvents";
import { systemCheckApi } from "./modules/systemCheck";
import { skillReadinessApi } from "./modules/skillReadiness";
import { sourceToolsApi } from "./modules/sourceTools";
import { expertsApi } from "./modules/experts";

export const api = {
  // Root
  ...rootApi,

  // Channels
  ...channelApi,

  // Heartbeat
  ...heartbeatApi,

  // Cron Jobs
  ...cronJobApi,

  // Chats
  ...chatApi,

  // Sessions（Legacy aliases）
  ...sessionApi,

  // Environment Variables
  ...envApi,

  // Providers
  ...providerApi,

  // Agent
  ...agentApi,

  // Skills
  ...skillApi,

  // Workspace
  ...workspaceApi,

  // Local Models
  ...localModelApi,

  // MCP Clients
  ...mcpApi,

  // Token Usage
  ...tokenUsageApi,
  // Tools
  ...toolsApi,

  // Security
  ...securityApi,

  // User Timezone
  ...userTimezoneApi,

  // Instance Management
  ...instanceApi,

  // Market
  ...marketApi,

  // My Skills
  ...mySkillsApi,

  // My MCP
  ...myMcpApi,

  // Market MCP
  ...marketMcpApi,

  // Feedback
  ...feedbackApi,

  // Approvals
  ...approvalApi,

  // HTML Preview Events
  ...htmlPreviewEventsApi,

  // System Check
  ...systemCheckApi,

  // Skill Readiness
  ...skillReadinessApi,
  // Source-owned custom built-in tools
  ...sourceToolsApi,
  ...expertsApi,
};

export default api;

// Export individual APIs for direct access
export { agentsApi };
export * from "./modules/market";
export * from "./modules/mySkills";
export * from "./modules/myMcp";
export * from "./modules/marketMcp";
export * from "./modules/skillReadiness";
export * from "./modules/approval";
export * from "./modules/snapAi";
