import {
  Layout,
  Menu,
  Button,
  Modal,
  Input,
  Form,
  Tooltip,
  type MenuProps,
} from "antd";
import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useAppMessage } from "../hooks/useAppMessage";
// ==================== 选择智能体 (Kun He) - 已注释 ====================
// import AgentSelector from "../components/AgentSelector";
// ==================== 选择智能体结束 (Kun He) - 已注释 ====================
import {
  SparkChatTabFill,
  SparkWifiLine,
  // SparkUserGroupLine,
  SparkDateLine,
  SparkLocalFileLine,
  SparkModePlazaLine,
  SparkInternetLine,
  SparkModifyLine,
  SparkBrowseLine,
  SparkToolLine,
  SparkExitFullscreenLine,
  SparkSearchUserLine,
  SparkMenuExpandLine,
  SparkMenuFoldLine,
  SparkBarChartLine,
  // SparkMessageLine,
  SparkSearchLine,
  SparkFileTxtLine,
  SparkRefreshLine,
} from "@agentscope-ai/icons";
import {
  ChartColumn,
  ChevronDown,
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
import { clearAuthToken } from "../api/config";
import { authApi } from "../api/modules/auth";
import { useIframeStore } from "../stores/iframeStore";
import styles from "./index.module.less";
import { useTheme } from "../contexts/ThemeContext";
import { KEY_TO_PATH, DEFAULT_OPEN_KEYS } from "./constants";

// ── Layout ────────────────────────────────────────────────────────────────

const { Sider } = Layout;

// ── Types ─────────────────────────────────────────────────────────────────

interface SidebarProps {
  selectedKey: string;
  withoutHeader?: boolean;
}

// ── Sidebar ───────────────────────────────────────────────────────────────

export default function Sidebar({
  selectedKey,
  withoutHeader = false,
}: SidebarProps) {
  const navigate = useNavigate();
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const { isDark } = useTheme();
  const isSuperManager = useIframeStore((state) => state.isSuperManager);
  const manager = useIframeStore((state) => state.manager);
  const hideChat = useIframeStore((state) => state.hideChat);
  const [authEnabled, setAuthEnabled] = useState(false);
  const [accountModalOpen, setAccountModalOpen] = useState(false);
  const [accountLoading, setAccountLoading] = useState(false);
  const [accountForm] = Form.useForm();
  const [collapsed, setCollapsed] = useState(false);
  const [openKeys, setOpenKeys] = useState<string[]>([...DEFAULT_OPEN_KEYS]);
  const canManageCurrentSourceConfig = isSuperManager || manager;
  const sourceId = useIframeStore((state) => state.source);
  const isRMassistSource = sourceId === "RMASSIST";
  const canUseSystemCheck = isSuperManager || manager;

  // ── Effects ──────────────────────────────────────────────────────────────

  useEffect(() => {
    authApi
      .getStatus()
      .then((res) => setAuthEnabled(res.enabled))
      .catch(() => {});
  }, []);

  // ── Handlers ──────────────────────────────────────────────────────────────

  const handleUpdateProfile = async (values: {
    currentPassword: string;
    newUsername?: string;
    newPassword?: string;
  }) => {
    const trimmedUsername = values.newUsername?.trim() || undefined;
    const trimmedPassword = values.newPassword?.trim() || undefined;

    if (values.newPassword && !trimmedPassword) {
      message.error(t("account.passwordEmpty"));
      return;
    }

    if (values.newUsername && !trimmedUsername) {
      message.error(t("account.usernameEmpty"));
      return;
    }

    if (!trimmedUsername && !trimmedPassword) {
      message.warning(t("account.nothingToUpdate"));
      return;
    }

    setAccountLoading(true);
    try {
      await authApi.updateProfile(
        values.currentPassword,
        trimmedUsername,
        trimmedPassword,
      );
      message.success(t("account.updateSuccess"));
      setAccountModalOpen(false);
      accountForm.resetFields();
      clearAuthToken();
      window.location.href = "/login";
    } catch (err: unknown) {
      const raw = err instanceof Error ? err.message : "";
      let msg = t("account.updateFailed");
      if (raw.includes("password is incorrect")) {
        msg = t("account.wrongPassword");
      } else if (raw.includes("Nothing to update")) {
        msg = t("account.nothingToUpdate");
      } else if (raw.includes("cannot be empty")) {
        msg = t("account.nothingToUpdate");
      } else if (raw) {
        msg = raw;
      }
      message.error(msg);
    } finally {
      setAccountLoading(false);
    }
  };

  // ── Collapsed nav items (all leaf pages) ──────────────────────────────

  const collapsedNavItems = [
    // 聊天
    {
      key: "chat",
      icon: <SparkChatTabFill size={18} />,
      path: "/chat",
      label: t("nav.chat"),
    },
    // 创作中心
    {
      key: "my-skills",
      icon: <Wrench size={18} />,
      path: "/my-skills",
      label: t("nav.mySkills"),
    },
    {
      key: "my-mcp",
      icon: <Puzzle size={18} />,
      path: "/my-mcp",
      label: t("nav.myMcp"),
    },
    {
      key: "experts",
      icon: <SearchCheck size={18} />,
      path: "/experts",
      label: t("nav.myExperts"),
    },
    {
      key: "workspace",
      icon: <SparkLocalFileLine size={18} />,
      path: "/workspace",
      label: t("nav.workspace"),
    },
    // 应用市场
    {
      key: "market",
      icon: <Store size={18} />,
      path: "/market",
      label: t("nav.market"),
    },
    // 运行中心
    {
      key: "cron-jobs",
      icon: <SparkDateLine size={18} />,
      path: "/cron-jobs",
      label: t("nav.cronJobs"),
    },
    {
      key: "scheduler-cron-jobs",
      icon: <SparkDateLine size={18} />,
      path: "/scheduler-cron-jobs",
      label: t("nav.cronJobsNew", "定时任务（新）"),
    },
    {
      key: "channels",
      icon: <SparkWifiLine size={18} />,
      path: "/channels",
      label: t("nav.channels"),
    },
    {
      key: "agent-config",
      icon: <SparkModifyLine size={18} />,
      path: "/agent-config",
      label: t("nav.agentConfig"),
    },
    {
      key: "tools",
      icon: <SparkToolLine size={18} />,
      path: "/tools",
      label: t("nav.tools"),
    },
    // 系统设置
    {
      key: "models",
      icon: <SparkModePlazaLine size={18} />,
      path: "/models",
      label: t("nav.models"),
    },
    {
      key: "featured-cases-management",
      icon: <SparkFileTxtLine size={18} />,
      path: "/featured-cases-management",
      label: t("nav.featuredCasesManagement", "精选案例管理"),
    },
    ...(canManageCurrentSourceConfig
      ? [
          {
            key: "scenario-presets-management",
            icon: <Settings size={18} />,
            path: "/scenario-presets-management",
            label: "场景预设管理",
          },
        ]
      : []),
    {
      key: "environments",
      icon: <SparkInternetLine size={18} />,
      path: "/environments",
      label: t("nav.environments"),
    },
    {
      key: "security",
      icon: <SparkBrowseLine size={18} />,
      path: "/security",
      label: t("nav.security"),
    },
    ...(isRMassistSource
      ? [
          {
            key: "skill-config",
            icon: <SlidersHorizontal size={18} />,
            path: "/skill-config",
            label: t("nav.skillConfig", "Skill 配置"),
          },
        ]
      : []),
    ...(canManageCurrentSourceConfig
      ? [
          {
            key: "system-config-page",
            icon: <SparkModifyLine size={18} />,
            path: "/system-config-page",
            label: t("nav.currentSourceConfig", {
              defaultValue: "系统特性配置",
            }),
          },
        ]
      : []),
    ...(canUseSystemCheck
      ? [
          {
            key: "system-check",
            icon: <SearchCheck size={18} />,
            path: "/system-check",
            label: t("nav.systemCheck", {
              defaultValue: "系统自检",
            }),
          },
        ]
      : []),
    // 洞察中心
    {
      key: "analytics-business-overview",
      icon: <SparkBarChartLine size={18} />,
      path: "/analytics/business-overview",
      label: t("nav.analyticsBusinessOverview", "运营看板"),
    },
    {
      key: "analytics-claw-data-overview",
      icon: <SparkBarChartLine size={18} />,
      path: "/analytics/claw-data-overview",
      label: t("nav.analyticsClawDataOverview", "Claw技能运行看板"),
    },
    ...(canManageCurrentSourceConfig
      ? [
          {
            key: "analytics-continuous-governance",
            icon: <SparkRefreshLine size={18} />,
            path: "/analytics/continuous-governance",
            label: t("nav.analyticsContinuousGovernance", "质量工程看板"),
          },
          {
            key: "monitor-cron-batch-dispatch",
            icon: <SparkRefreshLine size={18} />,
            path: "/monitor/cron-batch-dispatch",
            label: t("nav.monitorCronBatchDispatch", "批调度监控"),
          },
          {
            key: "monitor-cron-batch-dispatch-new",
            icon: <SparkRefreshLine size={18} />,
            path: "/monitor/cron-batch-dispatch-new",
            label: t("nav.monitorCronBatchDispatchNew", "批调度看板（新）"),
          },
        ]
      : []),
    {
      key: "analytics-messages",
      icon: <SparkSearchLine size={18} />,
      path: "/analytics/messages",
      label: t("nav.analyticsMessages", "Messages"),
    },
    {
      key: "monitor-task-center",
      icon: <SparkSearchLine size={18} />,
      path: "/monitor/tasks",
      label: t("nav.monitorTaskCenter", "异步任务中心"),
    },
    // {
    //   key: "analytics-users",
    //   icon: <SparkUserGroupLine size={18} />,
    //   path: "/analytics/users",
    //   label: t("nav.analyticsUsers", "Users"),
    // },
    // {
    //   key: "analytics-sessions",
    //   icon: <SparkMessageLine size={18} />,
    //   path: "/analytics/sessions",
    //   label: t("nav.analyticsSessions", "Sessions"),
    // },
    // {
    //   key: "analytics-traces",
    //   icon: <SparkFileTxtLine size={18} />,
    //   path: "/analytics/traces",
    //   label: t("nav.analyticsTraces", "Traces"),
    // },
    {
      key: "continuous-iteration",
      icon: <SparkRefreshLine size={18} />,
      path: "/continuous-iteration",
      label: t("nav.continuousIteration", "持续治理"),
    },
  ];

  // ── Menu items ────────────────────────────────────────────────────────────

  const menuItems: MenuProps["items"] = [
    // 1. 聊天（单独一级）
    !hideChat && {
      key: "chat",
      label: collapsed ? null : t("nav.chat"),
      icon: <SparkChatTabFill size={16} />,
    },
    // 2. 创作中心
    {
      key: "creation-center",
      label: collapsed ? null : t("nav.creationCenter"),
      icon: <PencilLine size={16} />,
      children: [
        {
          key: "my-skills",
          label: collapsed ? null : t("nav.mySkills"),
          icon: <Wrench size={16} />,
        },
        {
          key: "my-mcp",
          label: collapsed ? null : t("nav.myMcp"),
          icon: <Puzzle size={16} />,
        },
        {
          key: "experts",
          label: collapsed ? null : t("nav.myExperts"),
          icon: <SearchCheck size={16} />,
        },
        {
          key: "workspace",
          label: collapsed ? null : t("nav.workspace"),
          icon: <SparkLocalFileLine size={16} />,
        },
      ],
    },
    // 3. 应用市场（单独一级）
    {
      key: "market",
      label: collapsed ? null : t("nav.market"),
      icon: <Store size={16} />,
    },
    {
      key: "expert-community",
      label: collapsed ? null : "专家社区",
      icon: <SearchCheck size={16} />,
    },
    // 4. 运行中心
    {
      key: "run-center",
      label: collapsed ? null : t("nav.runCenter"),
      icon: <CirclePlay size={16} />,
      children: [
        {
          key: "cron-jobs",
          label: collapsed ? null : t("nav.cronJobs"),
          icon: <SparkDateLine size={16} />,
        },
        {
          key: "scheduler-cron-jobs",
          label: collapsed ? null : t("nav.cronJobsNew", "定时任务（新）"),
          icon: <SparkDateLine size={16} />,
        },
        {
          key: "channels",
          label: collapsed ? null : t("nav.channels"),
          icon: <SparkWifiLine size={16} />,
        },
        {
          key: "agent-config",
          label: collapsed ? null : t("nav.agentConfig"),
          icon: <SparkModifyLine size={16} />,
        },
        {
          key: "tools",
          label: collapsed ? null : t("nav.tools"),
          icon: <SparkToolLine size={16} />,
        },
        {
          key: "hook-management",
          label: collapsed ? null : t("nav.hookManagement"),
          icon: <ShieldCheck size={16} />,
        },
      ],
    },
    // 5. 系统设置
    {
      key: "system-settings",
      label: collapsed ? null : t("nav.systemSettings"),
      icon: <Settings size={16} />,
      children: [
        {
          key: "models",
          label: collapsed ? null : t("nav.models"),
          icon: <SparkModePlazaLine size={16} />,
        },
        {
          key: "featured-cases-management",
          label: collapsed
            ? null
            : t("nav.featuredCasesManagement", "精选案例管理"),
          icon: <SparkFileTxtLine size={16} />,
        },
        ...(canManageCurrentSourceConfig
          ? [
              {
                key: "scenario-presets-management",
                label: collapsed ? null : "场景预设管理",
                icon: <Settings size={16} />,
              },
            ]
          : []),
        {
          key: "environments",
          label: collapsed ? null : t("nav.environments"),
          icon: <SparkInternetLine size={16} />,
        },
        {
          key: "security",
          label: collapsed ? null : t("nav.security"),
          icon: <SparkBrowseLine size={16} />,
        },
        ...(isRMassistSource
          ? [
              {
                key: "skill-config",
                label: collapsed ? null : t("nav.skillConfig", "Skill 配置"),
                icon: <SlidersHorizontal size={16} />,
              },
            ]
          : []),
        ...(canManageCurrentSourceConfig
          ? [
              {
                key: "system-config-page",
                label: collapsed
                  ? null
                  : t("nav.currentSourceConfig", {
                      defaultValue: "系统特性配置",
                    }),
                icon: <SparkModifyLine size={16} />,
              },
            ]
          : []),
        ...(canUseSystemCheck
          ? [
              {
                key: "system-check",
                label: collapsed
                  ? null
                  : t("nav.systemCheck", {
                      defaultValue: "系统自检",
                    }),
                icon: <SearchCheck size={16} />,
              },
            ]
          : []),
      ],
    },
    // 6. 洞察中心
    {
      key: "insight-center",
      label: collapsed ? null : t("nav.insightCenter"),
      icon: <ChartColumn size={16} />,
      children: [
        !hideChat && {
          key: "analytics-business-overview",
          label: collapsed
            ? null
            : t("nav.analyticsBusinessOverview", "运营看板"),
          icon: <SparkBarChartLine size={16} />,
        },
        {
          key: "analytics-claw-data-overview",
          label: collapsed
            ? null
            : t("nav.analyticsClawDataOverview", "Claw技能运行看板"),
          icon: <SparkBarChartLine size={16} />,
        },
        ...(canManageCurrentSourceConfig
          ? [
              {
                key: "analytics-continuous-governance",
                label: collapsed
                  ? null
                  : t("nav.analyticsContinuousGovernance", "质量工程看板"),
                icon: <SparkRefreshLine size={16} />,
              },
              {
                key: "monitor-cron-batch-dispatch",
                label: collapsed
                  ? null
                  : t("nav.monitorCronBatchDispatch", "批调度监控"),
                icon: <SparkRefreshLine size={16} />,
              },
              {
                key: "monitor-cron-batch-dispatch-new",
                label: collapsed
                  ? null
                  : t("nav.monitorCronBatchDispatchNew", "批调度看板（新）"),
                icon: <SparkRefreshLine size={16} />,
              },
            ]
          : []),
        {
          key: "analytics-messages",
          label: collapsed ? null : t("nav.analyticsMessages", "Messages"),
          icon: <SparkSearchLine size={16} />,
        },
        {
          key: "monitor-task-center",
          label: collapsed ? null : t("nav.monitorTaskCenter", "异步任务中心"),
          icon: <SparkSearchLine size={16} />,
        },
        // {
        //   key: "analytics-users",
        //   label: collapsed ? null : t("nav.analyticsUsers", "Users"),
        //   icon: <SparkUserGroupLine size={16} />,
        // },
        // {
        //   key: "analytics-sessions",
        //   label: collapsed ? null : t("nav.analyticsSessions", "Sessions"),
        //   icon: <SparkMessageLine size={16} />,
        // },
        // {
        //   key: "analytics-traces",
        //   label: collapsed ? null : t("nav.analyticsTraces", "Traces"),
        //   icon: <SparkFileTxtLine size={16} />,
        // },
      ],
    },
    // 7. 质量工程
    {
      key: "quality-engineering",
      label: collapsed ? null : t("nav.qualityEngineering"),
      icon: <ShieldCheck size={16} />,
      children: [
        {
          key: "continuous-iteration",
          label: collapsed ? null : t("nav.continuousIteration", "持续治理"),
          icon: <SparkRefreshLine size={16} />,
        },
      ],
    },
  ];

  const renderExpandIcon = ({ isOpen }: { isOpen?: boolean }) => (
    <ChevronDown
      className={`${styles.submenuArrowIcon} ${
        isOpen ? styles.submenuArrowIconOpen : ""
      }`}
      size={14}
      strokeWidth={2.2}
    />
  );

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <Sider
      width={collapsed ? 72 : 240}
      className={`${styles.sider}${
        collapsed ? ` ${styles.siderCollapsed}` : ""
      }${isDark ? ` ${styles.siderDark}` : ""}${
        withoutHeader ? ` ${styles.siderWithoutHeader}` : ""
      }`}
    >
      {/* ==================== 选择智能体 (Kun He) - 已注释 ==================== */}
      {/* <div className={styles.agentSelectorContainer}>
        <AgentSelector collapsed={collapsed} />
      </div> */}
      {/* ==================== 选择智能体结束 (Kun He) - 已注释 ==================== */}
      {collapsed ? (
        <nav className={styles.collapsedNav}>
          {collapsedNavItems.map((item) => {
            const isActive = selectedKey === item.key;
            return (
              <Tooltip
                key={item.key}
                title={item.label}
                placement="right"
                overlayInnerStyle={{
                  background: "rgba(0,0,0,0.75)",
                  color: "#fff",
                }}
              >
                <button
                  className={`${styles.collapsedNavItem} ${
                    isActive ? styles.collapsedNavItemActive : ""
                  }`}
                  onClick={() => navigate(item.path)}
                >
                  {item.icon}
                </button>
              </Tooltip>
            );
          })}
        </nav>
      ) : (
        <Menu
          mode="inline"
          selectedKeys={[selectedKey]}
          openKeys={openKeys}
          onOpenChange={(keys) => setOpenKeys(keys.map(String))}
          onClick={({ key }) => {
            const path = KEY_TO_PATH[String(key)];
            if (path) navigate(path);
          }}
          items={menuItems}
          expandIcon={renderExpandIcon}
          theme={isDark ? "dark" : "light"}
          className={styles.sideMenu}
        />
      )}

      {authEnabled && !collapsed && (
        <div className={styles.authActions}>
          <Button
            type="text"
            icon={<SparkSearchUserLine size={16} />}
            onClick={() => {
              accountForm.resetFields();
              setAccountModalOpen(true);
            }}
            block
            className={`${styles.authBtn} ${
              collapsed ? styles.authBtnCollapsed : ""
            }`}
          >
            {!collapsed && t("account.title")}
          </Button>
          <Button
            type="text"
            icon={<SparkExitFullscreenLine size={16} />}
            onClick={() => {
              clearAuthToken();
              window.location.href = "/login";
            }}
            block
            className={`${styles.authBtn} ${
              collapsed ? styles.authBtnCollapsed : ""
            }`}
          >
            {!collapsed && t("login.logout")}
          </Button>
        </div>
      )}

      <div className={styles.collapseToggleContainer}>
        <Button
          type="text"
          icon={
            collapsed ? (
              <SparkMenuExpandLine size={20} />
            ) : (
              <SparkMenuFoldLine size={20} />
            )
          }
          onClick={() => setCollapsed(!collapsed)}
          className={styles.collapseToggle}
        />
      </div>

      <Modal
        open={accountModalOpen}
        onCancel={() => setAccountModalOpen(false)}
        title={t("account.title")}
        footer={null}
        destroyOnHidden
        centered
      >
        <Form
          form={accountForm}
          layout="vertical"
          onFinish={handleUpdateProfile}
        >
          <Form.Item
            name="currentPassword"
            label={t("account.currentPassword")}
            rules={[
              { required: true, message: t("account.currentPasswordRequired") },
            ]}
          >
            <Input.Password />
          </Form.Item>
          <Form.Item name="newUsername" label={t("account.newUsername")}>
            <Input placeholder={t("account.newUsernamePlaceholder")} />
          </Form.Item>
          <Form.Item name="newPassword" label={t("account.newPassword")}>
            <Input.Password placeholder={t("account.newPasswordPlaceholder")} />
          </Form.Item>
          <Form.Item
            name="confirmPassword"
            label={t("account.confirmPassword")}
            dependencies={["newPassword"]}
            rules={[
              ({ getFieldValue }) => ({
                validator(_, value) {
                  if (!value && !getFieldValue("newPassword")) {
                    return Promise.resolve();
                  }
                  if (value === getFieldValue("newPassword")) {
                    return Promise.resolve();
                  }
                  return Promise.reject(
                    new Error(t("account.passwordMismatch")),
                  );
                },
              }),
            ]}
          >
            <Input.Password
              placeholder={t("account.confirmPasswordPlaceholder")}
            />
          </Form.Item>
          <Form.Item>
            <Button
              type="primary"
              htmlType="submit"
              loading={accountLoading}
              block
            >
              {t("account.save")}
            </Button>
          </Form.Item>
        </Form>
      </Modal>
    </Sider>
  );
}
