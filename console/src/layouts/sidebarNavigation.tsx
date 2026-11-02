import type { ReactNode } from "react";
import type { MenuProps } from "antd";
import type { TFunction } from "i18next";
import {
  SparkChatTabFill,
  SparkWifiLine,
  SparkDateLine,
  SparkLocalFileLine,
  SparkModePlazaLine,
  SparkInternetLine,
  SparkModifyLine,
  SparkBrowseLine,
  SparkToolLine,
  SparkBarChartLine,
  SparkSearchLine,
  SparkFileTxtLine,
  SparkRefreshLine,
} from "@agentscope-ai/icons";
import {
  ChartColumn,
  CirclePlay,
  PencilLine,
  Settings,
  ShieldCheck,
  SearchCheck,
  Store,
  Wrench,
  Puzzle,
  SlidersHorizontal,
} from "lucide-react";
import { KEY_TO_PATH } from "./constants";

interface SidebarContext {
  isSuperManager: boolean;
  manager: boolean;
  hideChat: boolean;
  source: string | null;
  space: string | null;
  bbk: string | null;
}

interface MenuDefinition {
  key: string;
  label: ReactNode;
  icon: ReactNode;
  visible?: boolean;
  children?: MenuDefinition[];
  collapsedPath?: string;
}

interface CollapsedNavItem {
  key: string;
  label: ReactNode;
  icon: ReactNode;
  path: string;
}

function getSidebarVisibility(context: SidebarContext, collapsed: boolean) {
  const canManage = context.isSuperManager || context.manager;
  const isRMassistSpace = context.space === "RM_ASSISTANCE";
  const showTaskCenter = !isRMassistSpace || context.bbk === "100";

  return {
    // Preserve the existing mode differences; collapsing still shows these two entries.
    showChat: collapsed || !context.hideChat,
    showBusinessOverview: collapsed || !context.hideChat,
    showMessages: !context.hideChat,
    showSourceManagement: canManage,
    showSystemCheck: canManage,
    showSkillConfig: context.source === "RMASSIST",
    showGovernanceDashboards: canManage && showTaskCenter,
    showClawDashboards: isRMassistSpace,
    showTaskCenter,
    showExpandedOnly: !collapsed,
  };
}

function toMenuItems(definitions: MenuDefinition[]): MenuProps["items"] {
  return definitions
    .filter((item) => item.visible !== false)
    .map(({ key, label, icon, children }) => ({
      key,
      label,
      icon,
      ...(children ? { children: toMenuItems(children) } : {}),
    }));
}

function toCollapsedNavItems(
  definitions: MenuDefinition[],
): CollapsedNavItem[] {
  return definitions
    .filter((item) => item.visible !== false)
    .flatMap((item) =>
      item.children
        ? toCollapsedNavItems(item.children)
        : [
            {
              key: item.key,
              label: item.label,
              icon: item.icon,
              path: item.collapsedPath ?? KEY_TO_PATH[item.key],
            },
          ],
    );
}

export function createSidebarNavigation(
  t: TFunction,
  context: SidebarContext,
  collapsed: boolean,
) {
  const visibility = getSidebarVisibility(context, collapsed);
  const size = collapsed ? 18 : 16;
  const definitions: MenuDefinition[] = [
    {
      key: "chat",
      label: t("nav.chat"),
      icon: <SparkChatTabFill size={size} />,
      visible: visibility.showChat,
    },
    {
      key: "creation-center",
      label: t("nav.creationCenter"),
      icon: <PencilLine size={size} />,
      children: [
        {
          key: "my-skills",
          label: t("nav.mySkills"),
          icon: <Wrench size={size} />,
        },
        { key: "my-mcp", label: t("nav.myMcp"), icon: <Puzzle size={size} /> },
        {
          key: "experts",
          label: t("nav.myExperts"),
          icon: <SearchCheck size={size} />,
        },
        {
          key: "workspace",
          label: t("nav.workspace"),
          icon: <SparkLocalFileLine size={size} />,
        },
      ],
    },
    { key: "market", label: t("nav.market"), icon: <Store size={size} /> },
    {
      key: "expert-community",
      label: "专家社区",
      icon: <SearchCheck size={size} />,
      visible: visibility.showExpandedOnly,
    },
    {
      key: "run-center",
      label: t("nav.runCenter"),
      icon: <CirclePlay size={size} />,
      children: [
        {
          key: "cron-jobs",
          label: t("nav.cronJobs"),
          icon: <SparkDateLine size={size} />,
        },
        {
          key: "scheduler-cron-jobs",
          label: t("nav.cronJobsNew", "定时任务（新）"),
          icon: <SparkDateLine size={size} />,
        },
        {
          key: "channels",
          label: t("nav.channels"),
          icon: <SparkWifiLine size={size} />,
        },
        {
          key: "agent-config",
          label: t("nav.agentConfig"),
          icon: <SparkModifyLine size={size} />,
        },
        {
          key: "tools",
          label: t("nav.tools"),
          icon: <SparkToolLine size={size} />,
        },
        {
          key: "hook-management",
          label: t("nav.hookManagement"),
          icon: <ShieldCheck size={size} />,
          visible: visibility.showExpandedOnly,
        },
      ],
    },
    {
      key: "system-settings",
      label: t("nav.systemSettings"),
      icon: <Settings size={size} />,
      children: [
        {
          key: "models",
          label: t("nav.models"),
          icon: <SparkModePlazaLine size={size} />,
        },
        {
          key: "featured-cases-management",
          label: t("nav.featuredCasesManagement", "精选案例管理"),
          icon: <SparkFileTxtLine size={size} />,
        },
        {
          key: "scenario-presets-management",
          label: "场景预设管理",
          icon: <Settings size={size} />,
          visible: visibility.showSourceManagement,
        },
        {
          key: "environments",
          label: t("nav.environments"),
          icon: <SparkInternetLine size={size} />,
        },
        {
          key: "security",
          label: t("nav.security"),
          icon: <SparkBrowseLine size={size} />,
        },
        {
          key: "skill-config",
          label: t("nav.skillConfig", "Skill 配置"),
          icon: <SlidersHorizontal size={size} />,
          visible: visibility.showSkillConfig,
        },
        {
          key: "system-config-page",
          label: t("nav.currentSourceConfig", { defaultValue: "系统特性配置" }),
          icon: <SparkModifyLine size={size} />,
          visible: visibility.showSourceManagement,
        },
        {
          key: "system-check",
          label: t("nav.systemCheck", { defaultValue: "系统自检" }),
          icon: <SearchCheck size={size} />,
          visible: visibility.showSystemCheck,
        },
      ],
    },
    {
      key: "insight-center",
      label: t("nav.insightCenter"),
      icon: <ChartColumn size={size} />,
      children: [
        {
          key: "analytics-business-overview",
          label: t("nav.analyticsBusinessOverview", "运营看板"),
          icon: <SparkBarChartLine size={size} />,
          visible: visibility.showBusinessOverview,
        },
        {
          key: "analytics-continuous-governance",
          label: t("nav.analyticsContinuousGovernance", "质量工程看板"),
          icon: <SparkRefreshLine size={size} />,
          visible: visibility.showGovernanceDashboards,
        },
        {
          key: "monitor-cron-batch-dispatch",
          label: t("nav.monitorCronBatchDispatch", "批调度监控"),
          icon: <SparkRefreshLine size={size} />,
          visible: visibility.showGovernanceDashboards,
        },
        {
          key: "monitor-cron-batch-dispatch-new",
          label: t("nav.monitorCronBatchDispatchNew", "批调度看板（新）"),
          icon: <SparkRefreshLine size={size} />,
          visible: visibility.showGovernanceDashboards,
        },
        {
          key: "analytics-claw-data-overview",
          label: t("nav.analyticsClawDataOverview", "Claw数据看板"),
          icon: <SparkBarChartLine size={size} />,
          visible: visibility.showClawDashboards,
        },
        {
          key: "analytics-claw-skill-data-overview",
          label: t("nav.analyticsClawSkillDataOverview", "Claw技能运行看板"),
          icon: <SparkBarChartLine size={size} />,
          visible: visibility.showClawDashboards,
          // Only the collapsed sidebar currently has a route for this entry.
          collapsedPath: "/analytics/claw-skill-data-overview",
        },
        {
          key: "analytics-messages",
          label: t("nav.analyticsMessages", "Messages"),
          icon: <SparkSearchLine size={size} />,
          visible: visibility.showMessages,
        },
        {
          key: "monitor-task-center",
          label: t("nav.monitorTaskCenter", "异步任务中心"),
          icon: <SparkSearchLine size={size} />,
          visible: visibility.showTaskCenter,
        },
      ],
    },
    {
      key: "quality-engineering",
      label: t("nav.qualityEngineering"),
      icon: <ShieldCheck size={size} />,
      children: [
        {
          key: "continuous-iteration",
          label: t("nav.continuousIteration", "持续治理"),
          icon: <SparkRefreshLine size={size} />,
        },
      ],
    },
  ];

  return {
    menuItems: toMenuItems(definitions),
    collapsedNavItems: toCollapsedNavItems(definitions),
  };
}
