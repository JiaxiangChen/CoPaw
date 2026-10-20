import { Layout } from "antd";
import { Routes, Route, useLocation, Navigate } from "react-router-dom";
import { useEffect } from "react";
// useIframeStore: 获取父窗口传递的 hideMenu 参数
import { useIframeStore } from "../../stores/iframeStore";
import { useChatPresentationStore } from "../../stores/chatPresentationStore";
import { useSourceSystemConfigStore } from "../../stores/sourceSystemConfigStore";
import { DEFAULT_SOURCE_ID } from "../../constants/identity";

import Sidebar from "../Sidebar";
import Header from "../Header";
import ConsoleCronBubble from "../../components/ConsoleCronBubble";
import styles from "../index.module.less";
import Chat from "../../pages/Chat";
import ChannelsPage from "../../pages/Control/Channels";
import SessionsPage from "../../pages/Control/Sessions";
import CronJobsPage from "../../pages/Control/CronJobs";
import SchedulerCronJobsPage from "../../pages/Control/SchedulerCronJobs";
import FeaturedCasesPage from "../../pages/Control/FeaturedCases";
import ScenarioPresetsPage from "../../pages/Control/ScenarioPresets";
import GreetingPage from "../../pages/Control/Greeting";
import HeartbeatPage from "../../pages/Control/Heartbeat";
import HookManagementPage from "../../pages/Control/HookManagement";
import AgentConfigPage from "../../pages/Agent/Config";
import SystemConfigPage from "../../pages/SystemConfigPage";
import SystemCheckPage from "../../pages/SystemCheck";
import SkillsPage from "../../pages/Agent/Skills";
import SkillPoolPage from "../../pages/Agent/SkillPool";
import ToolsPage from "../../pages/Agent/Tools";
import WorkspacePage from "../../pages/Agent/Workspace";
import MCPPage from "../../pages/Agent/MCP";
import ModelsPage from "../../pages/Settings/Models";
import EnvironmentsPage from "../../pages/Settings/Environments";
import SecurityPage from "../../pages/Settings/Security";
import TokenUsagePage from "../../pages/Settings/TokenUsage";
import VoiceTranscriptionPage from "../../pages/Settings/VoiceTranscription";
import AgentsPage from "../../pages/Settings/Agents";
import SkillConfigPage from "../../pages/Settings/SkillConfig";
import AnalyticsPage from "../../pages/Analytics";
import InstancePage from "../../pages/Instance";
import MonitorPage from "../../pages/Monitor";
import ContinuousIterationPage from "../../pages/Harness/ContinuousIteration";
// ==================== 测试页面 (用于验证新功能) ====================
import TestDownloadCardPage from "../../pages/TestDownloadCard";
import TestFileManagerDrawerPage from "../../pages/TestFileManagerDrawer";
import TestUserDetailModalPage from "../../pages/TestUserDetailModal";
import ReportViewPage from "../../pages/ReportView";
// ==================== 测试页面结束 ====================
import MarketPage from "../../pages/Market";
import MySkillsPage from "../../pages/MySkills";
import MyMCPPage from "../../pages/MyMCP";
import WPlusSopWorkspace from "../../pages/WPlusSopWorkspace";
import ExpertsPage from "../../pages/Experts";
import ExpertCommunityPage from "../../pages/ExpertCommunity";

import { useDynamicRender } from "@/components/agentscope-chat/DynamicRenderContext";

const { Content } = Layout;

const pathToKey: Record<string, string> = {
  "/chat": "chat",
  "/channels": "channels",
  "/sessions": "sessions",
  "/cron-jobs": "cron-jobs",
  "/scheduler-cron-jobs": "scheduler-cron-jobs",
  "/greeting-management": "greeting-management",
  "/featured-cases-management": "featured-cases-management",
  "/scenario-presets-management": "scenario-presets-management",
  "/heartbeat": "heartbeat",
  "/hook-management": "hook-management",
  "/skills": "skills",
  "/skill-pool": "skill-pool",
  "/tools": "tools",
  "/mcp": "mcp",
  "/workspace": "workspace",
  "/agents": "agents",
  "/models": "models",
  "/environments": "environments",
  "/agent-config": "agent-config",
  "/system-config-page": "system-config-page",
  "/system-check": "system-check",
  "/security": "security",
  "/token-usage": "token-usage",
  "/voice-transcription": "voice-transcription",
  "/skill-config": "skill-config",
  "/analytics/users": "analytics-users",
  "/analytics/sessions": "analytics-sessions",
  "/analytics/messages": "analytics-messages",
  "/analytics/traces": "analytics-traces",
  "/analytics/business-overview": "analytics-business-overview",
  "/analytics/claw-data-overview": "analytics-claw-data-overview",
  "/analytics/cron-job-overview": "analytics-cron-job-overview",
  "/analytics/continuous-governance": "analytics-continuous-governance",
  "/monitor/tasks": "monitor-task-center",
  "/monitor/cron-batch-dispatch": "monitor-cron-batch-dispatch",
  "/monitor/cron-batch-dispatch-new": "monitor-cron-batch-dispatch-new",
  "/instance/overview": "instance-overview",
  "/instance/instances": "instance-instances",
  "/instance/allocations": "instance-allocations",
  "/instance/operation-logs": "instance-operation-logs",
  "/continuous-iteration": "continuous-iteration",
  "/market": "market",
  "/my-skills": "my-skills",
  "/my-mcp": "my-mcp",
  "/experts": "experts",
  "/expert-community": "expert-community",
};

export default function MainLayout() {
  const location = useLocation();
  const currentPath = location.pathname;
  const selectedKey = pathToKey[currentPath] || "chat";

  // 动态渲染模版上下文
  const dynamicRender = useDynamicRender(); // 使用 useDynamicRender 钩子

  // 检测是否为 reportView 页面，如果是则跳过预加载所有模块
  const isReportViewPage = currentPath === "/reportView";

  // Global shell display control:
  // - iframe hideMenu === true hides Header and Sidebar
  // - origin=Y maps to hideMenu=true (see iframeMessage.ts)
  // - showContentOnly initializes a runtime presentation flag before React renders
  const hideMenu = useIframeStore((state) => state.hideMenu);
  const hideChat = useIframeStore((state) => state.hideChat);
  const showContentOnly = useChatPresentationStore(
    (state) => state.showContentOnly,
  );
  const activeSourceId =
    useIframeStore((state) => state.source) || DEFAULT_SOURCE_ID;
  const loadEffectiveConfig = useSourceSystemConfigStore(
    (state) => state.loadEffectiveConfig,
  );
  const hideGlobalShell = hideMenu || showContentOnly;
  const hideHeader = hideGlobalShell || activeSourceId === "ruice";

  useEffect(() => {
    loadEffectiveConfig(activeSourceId);
  }, [activeSourceId, loadEffectiveConfig]);

  // 初始化动态渲染模版（应用启动时预加载）
  useEffect(() => {
    dynamicRender.initialize({ skipPreload: isReportViewPage });
  }, [dynamicRender, isReportViewPage]);

  return (
    <Layout className={styles.mainLayout}>
      {!hideHeader && <Header />}
      <Layout>
        {!hideGlobalShell && (
          <Sidebar selectedKey={selectedKey} withoutHeader={hideHeader} />
        )}
        <Content
          className={`page-container${
            hideGlobalShell ? "" : " page-container--with-sidebar"
          }${isReportViewPage ? " page-container--no-rightPadding" : ""}${
            showContentOnly ? ` ${styles.contentOnlyContainer}` : ""
          }`}
        >
          {!showContentOnly && <ConsoleCronBubble />}
          <div
            className={`page-content${
              isReportViewPage ? " single-page-content" : ""
            }${showContentOnly ? ` ${styles.contentOnlyContent}` : ""}`}
          >
            <Routes>
              {hideChat ? (
                <Route path="/" element={<Navigate to="/market" replace />} />
              ) : (
                <Route path="/" element={<Navigate to="/chat" replace />} />
              )}
              <Route path="/chat/*" element={<Chat />} />
              <Route
                path="/wplus-sop/:sessionId"
                element={<WPlusSopWorkspace />}
              />
              <Route path="/channels" element={<ChannelsPage />} />
              <Route path="/sessions" element={<SessionsPage />} />
              <Route path="/cron-jobs" element={<CronJobsPage />} />
              <Route
                path="/scheduler-cron-jobs"
                element={<SchedulerCronJobsPage />}
              />
              <Route path="/greeting-management" element={<GreetingPage />} />
              <Route
                path="/featured-cases-management"
                element={<FeaturedCasesPage />}
              />
              <Route
                path="/scenario-presets-management"
                element={<ScenarioPresetsPage />}
              />
              <Route path="/heartbeat" element={<HeartbeatPage />} />
              <Route path="/hook-management" element={<HookManagementPage />} />
              <Route path="/skills" element={<SkillsPage />} />
              <Route path="/skill-pool" element={<SkillPoolPage />} />
              <Route path="/tools" element={<ToolsPage />} />
              <Route path="/mcp" element={<MCPPage />} />
              <Route path="/workspace" element={<WorkspacePage />} />
              <Route path="/agents" element={<AgentsPage />} />
              <Route path="/models" element={<ModelsPage />} />
              <Route path="/environments" element={<EnvironmentsPage />} />
              <Route path="/agent-config" element={<AgentConfigPage />} />
              <Route
                path="/system-config-page"
                element={<SystemConfigPage />}
              />
              <Route path="/system-check" element={<SystemCheckPage />} />
              <Route path="/security" element={<SecurityPage />} />
              <Route path="/token-usage" element={<TokenUsagePage />} />
              <Route
                path="/voice-transcription"
                element={<VoiceTranscriptionPage />}
              />
              <Route path="/skill-config" element={<SkillConfigPage />} />
              <Route path="/analytics/*" element={<AnalyticsPage />} />
              <Route path="/monitor/*" element={<MonitorPage />} />
              <Route path="/instance/*" element={<InstancePage />} />
              <Route
                path="/continuous-iteration"
                element={<ContinuousIterationPage />}
              />
              {/* ==================== 测试路由 ==================== */}
              <Route
                path="/test-download-card"
                element={<TestDownloadCardPage />}
              />
              <Route
                path="/test-file-manager-drawer"
                element={<TestFileManagerDrawerPage />}
              />
              <Route
                path="/test-user-detail-modal"
                element={<TestUserDetailModalPage />}
              />
              {/* ==================== 测试路由结束 ==================== */}
              <Route path="/reportView" element={<ReportViewPage />} />
              <Route path="/market" element={<MarketPage />} />
              <Route path="/my-skills" element={<MySkillsPage />} />
              <Route path="/my-mcp" element={<MyMCPPage />} />
              <Route path="/experts" element={<ExpertsPage />} />
              <Route
                path="/expert-community"
                element={<ExpertCommunityPage />}
              />
            </Routes>
          </div>
        </Content>
      </Layout>
    </Layout>
  );
}
