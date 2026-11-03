/**
 * AI平台运营概览 - 业务价值展示页面
 * 用于银行管理层查看平台使用情况和业务覆盖情况
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { UIEvent } from "react";
import { useNavigate } from "react-router-dom";
import {
  CalendarDays,
  CheckSquare,
  ChevronRight,
  Coins,
  Database,
  MessageCircleMore,
  RotateCw,
  Sparkles,
  TrendingDown,
  TrendingUp,
  UserRound,
  Users,
} from "lucide-react";
import { DatePicker, Select, Spin, Tooltip, message } from "antd";
import ReactECharts from "echarts-for-react";
import type { Dayjs } from "dayjs";
import dayjs from "dayjs";
import styles from "./index.module.less";
import { useIframeStore } from "../../../stores/iframeStore";
import { DEFAULT_SOURCE_ID } from "../../../constants/identity";
import {
  tracingApi,
  displaySkillName,
  type BranchMetricItem,
  type ErrorSummary,
  type OverviewStats,
  type SkillUsage,
  type TaskStatusSummary,
} from "../../../api/modules/tracing";
import UserDetailModal from "./components/UserDetailModal";
import SkillDetailModal from "./components/SkillDetailModal";
import ErrorDetailModal from "./components/ErrorDetailModal";
import { BBK_ID_TO_NAME_MAP, getBbkDisplayName } from "../../../constants/bbk";
import {
  ensureBranchOptions,
  getScopedBranchFilter,
} from "../../../utils/branchScope";
import {
  formatChange,
  formatNumber,
  formatPercent,
  formatTokens,
  toChangeDirection,
  truncateName,
  type BreakdownItem,
  type OverviewMetricCard,
  type SummaryLegendItem,
  type TimeRange,
  type TrendDatum,
  type UserRow,
} from "./types";
const { Option } = Select;

const METRIC_ACCENT_COLORS = [
  "#2563eb",
  "#22c55e",
  "#06b6d4",
  "#f97316",
  "#7c3aed",
];

const DONUT_COLORS = ["#18b368", "#f97316", "#ef4444", "#94a3b8"]; // 成功、运行中、失败、取消
const safeNumber = (value: unknown): number =>
  typeof value === "number" && !Number.isNaN(value) ? value : 0;

const iconMap = {
  users: UserRound,
  conversations: MessageCircleMore,
  sessions: CheckSquare,
  tokens: Coins,
  customers: Users,
};

function mapBreakdown(
  rows: BranchMetricItem[] | undefined,
  formatter?: (value: number) => string,
): BreakdownItem[] | null {
  const mapped = (rows || []).slice(0, 5).map((item) => ({
    name: item.bbk_name || item.bbk_id || "-",
    value: Math.max(item.percent || 0, 8),
    valueText: formatter
      ? formatter(safeNumber(item.value))
      : formatPercent(item.percent || 0),
  }));

  // 无真实数据时返回 null，由渲染层显示空状态
  if (mapped.length === 0) {
    return null;
  }

  return mapped;
}

function buildMetricCards(
  overviewStats: OverviewStats | null,
  taskStatusSummary: TaskStatusSummary | null,
): OverviewMetricCard[] {
  const growthStats = overviewStats?.growth_stats;
  return [
    {
      key: "users",
      title: "活跃用户数",
      valueText: (
        <span className={styles.userValueWrap}>
          <span className={styles.userTotal}>
            {formatNumber(overviewStats?.total_users ?? 0)}
          </span>
          <span className={styles.userAnnotation}>
            <span className={styles.annotationRow}>
              <span
                className={styles.annotationDot}
                style={{ background: "#6366f1" }}
              />
              IT人员 {formatNumber(overviewStats?.it_users ?? 0)}
            </span>
            <span className={styles.annotationRow}>
              <span
                className={styles.annotationDot}
                style={{ background: "#22c55e" }}
              />
              业务人员 {formatNumber(overviewStats?.business_users ?? 0)}
            </span>
          </span>
        </span>
      ),
      changeText: formatChange(growthStats?.userGrowth),
      changeDirection: toChangeDirection(growthStats?.userGrowth),
      accentColor: METRIC_ACCENT_COLORS[0],
      breakdown: mapBreakdown(overviewStats?.branch_breakdown?.users),
    },
    {
      key: "sessions",
      title: "总会话数",
      valueText: formatNumber(overviewStats?.total_sessions ?? 0),
      changeText: formatChange(growthStats?.sessionGrowth),
      changeDirection: toChangeDirection(growthStats?.sessionGrowth),
      accentColor: METRIC_ACCENT_COLORS[1],
      breakdown: mapBreakdown(overviewStats?.branch_breakdown?.sessions),
    },
    {
      key: "cron_tasks",
      title: "定时任务执行数",
      valueText: (
        <span className={styles.userValueWrap}>
          <span className={styles.userTotal}>
            {formatNumber(taskStatusSummary?.total_tasks ?? 0)}
          </span>
          <span className={styles.userAnnotation}>
            <span className={styles.annotationRow}>
              <span
                className={styles.annotationDot}
                style={{ background: "#22c55e" }}
              />
              已读 {formatNumber(taskStatusSummary?.read_count ?? 0)}
            </span>
          </span>
        </span>
      ),
      changeText: formatChange(growthStats?.cronGrowth),
      changeDirection: toChangeDirection(growthStats?.cronGrowth),
      accentColor: METRIC_ACCENT_COLORS[2],
      breakdown: mapBreakdown(overviewStats?.branch_breakdown?.cron_tasks),
    },
    {
      key: "tokens",
      title: "资源消耗",
      valueText: formatTokens(overviewStats?.total_tokens ?? 0),
      changeText: formatChange(growthStats?.tokensGrowth),
      changeDirection: toChangeDirection(growthStats?.tokensGrowth),
      accentColor: METRIC_ACCENT_COLORS[3],
      breakdown: mapBreakdown(overviewStats?.branch_breakdown?.tokens),
    },
    {
      key: "customers",
      title: "查看报告客户数",
      valueText: (
        <span className={styles.userValueWrap}>
          <span className={styles.userTotal}>
            {formatNumber(overviewStats?.plan_customers ?? 0)}
          </span>
          <span className={styles.userAnnotation}>
            <span className={styles.annotationRow}>
              <span
                className={styles.annotationDot}
                style={{ background: "#3b82f6" }}
              />
              去洞察客户数 {formatNumber(overviewStats?.insight_customers ?? 0)}
            </span>
            <span className={styles.annotationRow}>
              <span
                className={styles.annotationDot}
                style={{ background: "#f97316" }}
              />
              去电访客户数 {formatNumber(overviewStats?.phone_customers ?? 0)}
            </span>
          </span>
        </span>
      ),
      changeText: formatChange(growthStats?.planCustomersGrowth),
      changeDirection: toChangeDirection(growthStats?.planCustomersGrowth),
      accentColor: METRIC_ACCENT_COLORS[4],
      breakdown: mapBreakdown(overviewStats?.branch_breakdown?.customers),
    },
  ];
}

function buildExecutionSummary(
  summary: TaskStatusSummary | null,
): SummaryLegendItem[] {
  return [
    {
      key: "success",
      label: "成功",
      value: safeNumber(summary?.success),
      color: DONUT_COLORS[0],
    },
    {
      key: "running",
      label: "运行中",
      value: safeNumber(summary?.running),
      color: DONUT_COLORS[1],
    },
    {
      key: "failed",
      label: "失败",
      value: safeNumber(summary?.failed),
      color: DONUT_COLORS[2],
    },
    {
      key: "cancelled",
      label: "已取消/跳过",
      value: safeNumber(summary?.cancelled),
      color: DONUT_COLORS[3],
    },
  ];
}

function buildErrorSummary(summary: ErrorSummary | null): SummaryLegendItem[] {
  return [
    {
      key: "model-error",
      label: "模型报错",
      value: safeNumber(summary?.model_errors),
      color: "#f59e0b",
    },
    {
      key: "tool-error",
      label: "工具报错",
      value: safeNumber(summary?.tool_errors),
      color: "#ef4444",
    },
  ];
}

function buildDonutSegments(items: SummaryLegendItem[]) {
  const total = Math.max(
    items.reduce((sum, item) => sum + item.value, 0),
    1,
  );
  let offset = 0;

  return items.map((item) => {
    const fraction = item.value / total;
    const segment = {
      ...item,
      dasharray: `${fraction * 283} 283`,
      dashoffset: -offset,
    };
    offset += fraction * 283;
    return segment;
  });
}

function renderModelErrorCodeTooltip(summary: ErrorSummary | null) {
  const rows = summary?.model_error_codes || [];

  if (rows.length === 0) {
    return (
      <div className={styles.errorCodeTooltip}>
        <div className={styles.errorCodeTooltipEmpty}>暂无可识别错误码</div>
      </div>
    );
  }

  return (
    <div className={styles.errorCodeTooltip}>
      {rows.map((row) => (
        <div key={row.code} className={styles.errorCodeTooltipRow}>
          <span className={styles.errorCodeTooltipCode}>{row.code}</span>
          <span className={styles.errorCodeTooltipCount}>
            {formatNumber(row.count)}个
          </span>
        </div>
      ))}
    </div>
  );
}

/** 漏斗图组件：使用 echarts 展示任务执行转化率 */
function TaskFunnel({
  taskStatusSummary,
}: {
  taskStatusSummary: TaskStatusSummary | null;
}) {
  const totalTasks = safeNumber(taskStatusSummary?.total_tasks);
  const successCount = safeNumber(taskStatusSummary?.success);
  const readCount = safeNumber(taskStatusSummary?.read_count);

  if (totalTasks === 0) {
    return (
      <div className={styles.emptyBreakdown}>
        <Database className={styles.emptyBreakdownIcon} />
        <span className={styles.emptyBreakdownText}>暂无任务数据</span>
      </div>
    );
  }

  const successRate = ((successCount / totalTasks) * 100).toFixed(1);
  const readRate =
    successCount > 0 ? ((readCount / successCount) * 100).toFixed(1) : "0.0";

  // 值为 0 时保证有最小值显示
  const minBar = Math.max(totalTasks * 0.12, 1);
  const ensureVisible = (v: number) => (v <= minBar ? minBar : v);

  const funnelColors = ["#4f46e5", "#16a34a", "#0891b2"];

  const chartData = [
    {
      name: "总任务数",
      value: ensureVisible(totalTasks),
      rawValue: totalTasks,
    },
    {
      name: "执行成功数",
      value: ensureVisible(successCount),
      rawValue: successCount,
    },
    { name: "已读数", value: ensureVisible(readCount), rawValue: readCount },
  ];

  const option = {
    tooltip: {
      trigger: "item",
      formatter: (params: { name: string; data: { rawValue: number } }) =>
        `${params.name}: ${formatNumber(params.data.rawValue)}`,
      extraCssText: "max-width: 200px; white-space: normal;",
    },
    legend: {
      data: chartData.map((item) => item.name),
      orient: "horizontal",
      bottom: 0,
      itemWidth: 10,
      itemHeight: 10,
      itemGap: 12,
      textStyle: {
        color: "#475569",
        fontSize: 11,
        fontWeight: 500,
      },
    },
    grid: {
      left: "5%",
      right: "35%",
      top: "10%",
      bottom: "15%",
    },
    xAxis: { show: false, type: "value" },
    yAxis: { show: false, type: "category" },
    series: [
      {
        type: "funnel",
        left: "5%",
        right: "40%",
        top: "5%",
        bottom: "25%",
        min: 0,
        max: totalTasks,
        minSize: "35%",
        maxSize: "100%",
        sort: "descending",
        gap: 2,
        label: {
          show: true,
          position: "inside",
          formatter: (params: { name: string; data: { rawValue: number } }) =>
            `${params.name}${formatNumber(params.data.rawValue)}`,
          color: "#fff",
          fontSize: 10,
          fontWeight: 600,
        },
        itemStyle: {
          borderWidth: 0,
        },
        data: chartData.map((item, index) => ({
          name: item.name,
          value: item.value,
          rawValue: item.rawValue,
          itemStyle: { color: funnelColors[index] },
        })),
      },
    ],
    // 右侧转化率标注
    graphic: [
      // 第一层到第二层的转化率
      {
        type: "group",
        left: "68%",
        top: "18%",
        children: [
          {
            type: "circle",
            shape: { cx: 3, cy: 0, r: 3 },
            style: { fill: "#94a3b8" },
          },
          {
            type: "line",
            shape: { x1: 3, y1: 0, x2: 3, y2: 20 },
            style: { stroke: "#94a3b8", lineWidth: 1, lineDash: [3, 2] },
          },
          {
            type: "circle",
            shape: { cx: 3, cy: 20, r: 3 },
            style: { fill: "#94a3b8" },
          },
          {
            type: "text",
            style: {
              text: `→ ${successRate}%`,
              x: 12,
              y: 10,
              fill: "#64748b",
              fontSize: 10,
              fontWeight: 500,
            },
          },
        ],
      },
      // 第二层到第三层的转化率
      {
        type: "group",
        left: "68%",
        top: "38%",
        children: [
          {
            type: "circle",
            shape: { cx: 3, cy: 0, r: 3 },
            style: { fill: "#94a3b8" },
          },
          {
            type: "line",
            shape: { x1: 3, y1: 0, x2: 3, y2: 20 },
            style: { stroke: "#94a3b8", lineWidth: 1, lineDash: [3, 2] },
          },
          {
            type: "circle",
            shape: { cx: 3, cy: 20, r: 3 },
            style: { fill: "#94a3b8" },
          },
          {
            type: "text",
            style: {
              text: `→ ${readRate}%`,
              x: 12,
              y: 10,
              fill: "#64748b",
              fontSize: 10,
              fontWeight: 500,
            },
          },
        ],
      },
    ],
  };

  return (
    <div className={styles.funnelWrap}>
      <ReactECharts option={option} style={{ height: 200 }} />
    </div>
  );
}

function buildTrendChartOption(
  trendData: TrendDatum[],
  showExtendedTrendMetrics: boolean,
) {
  const dates = trendData.map((item) =>
    item.date.includes(":")
      ? dayjs(item.date).format("HH:mm")
      : dayjs(item.date).format("MM-DD"),
  );
  const extendedLegend = ["查看方案客户数", "去洞察客户数", "去电访客户数"];
  const series = [
    {
      name: "调用量",
      type: "bar" as const,
      yAxisIndex: 0,
      data: trendData.map((i) => i.calls),
      barWidth: 16,
      itemStyle: {
        color: "#4f7cff",
        borderRadius: [8, 8, 0, 0],
      },
      emphasis: {
        itemStyle: {
          color: "#2f5ff0",
        },
      },
      z: 1,
    },
    {
      name: "调用用户",
      type: "line" as const,
      yAxisIndex: 0,
      data: trendData.map((i) => i.users),
      smooth: true,
      lineStyle: {
        color: "#94a3b8",
        width: 2,
        opacity: 0.75,
      },
      symbol: "none" as const,
      z: 2,
    },
    {
      name: "已读任务数",
      type: "line" as const,
      yAxisIndex: 1,
      data: trendData.map((i) => i.read_tasks),
      smooth: true,
      symbol: "none" as const,
      lineStyle: {
        color: "#c084fc",
        width: 2,
        opacity: 0.78,
      },
      z: 2,
    },
    ...(showExtendedTrendMetrics
      ? [
          {
            name: "查看方案客户数",
            type: "line" as const,
            yAxisIndex: 1,
            data: trendData.map((i) => i.plan_customers),
            smooth: true,
            symbol: "circle" as const,
            symbolSize: 8,
            lineStyle: {
              color: "#f97316",
              width: 3,
            },
            itemStyle: {
              color: "#f97316",
              borderColor: "#ffffff",
              borderWidth: 2,
            },
            z: 4,
          },
          {
            name: "去洞察客户数",
            type: "line" as const,
            yAxisIndex: 1,
            data: trendData.map((i) => i.insight_customers),
            smooth: true,
            symbol: "none" as const,
            lineStyle: {
              color: "#fb7185",
              width: 2,
              opacity: 0.72,
            },
            z: 2,
          },
          {
            name: "去电访客户数",
            type: "line" as const,
            yAxisIndex: 1,
            data: trendData.map((i) => i.phone_customers),
            smooth: true,
            symbol: "circle" as const,
            symbolSize: 8,
            lineStyle: {
              color: "#10b981",
              width: 3,
            },
            itemStyle: {
              color: "#10b981",
              borderColor: "#ffffff",
              borderWidth: 2,
            },
            z: 4,
          },
        ]
      : []),
  ];

  return {
    tooltip: {
      trigger: "axis" as const,
      axisPointer: {
        type: "cross" as const,
        crossStyle: {
          color: "#94a3b8",
        },
      },
      backgroundColor: "rgba(15, 23, 42, 0.94)",
      borderColor: "rgba(148, 163, 184, 0.18)",
      textStyle: {
        color: "#f8fafc",
      },
    },
    legend: {
      show: true,
      type: "scroll" as const,
      data: [
        "调用量",
        "调用用户",
        "已读任务数",
        ...extendedLegend.filter(() => showExtendedTrendMetrics),
      ],
      bottom: 0,
      left: "center" as const,
      itemWidth: 12,
      itemHeight: 8,
      itemGap: 14,
      textStyle: {
        color: "#64748b",
        fontSize: 12,
        fontWeight: 600,
      },
      pageIconColor: "#2563eb",
      pageIconInactiveColor: "#cbd5e1",
      pageTextStyle: {
        color: "#94a3b8",
      },
    },
    grid: {
      left: 60,
      right: 60,
      top: 28,
      bottom: 52,
    },
    xAxis: {
      type: "category" as const,
      data: dates,
      axisTick: { show: false },
      axisLine: {
        lineStyle: {
          color: "#dbe3ef",
        },
      },
      axisLabel: {
        interval: "auto" as const,
        color: "#64748b",
      },
    },
    yAxis: [
      {
        type: "value" as const,
        name: "调用量 / 用户",
        position: "left" as const,
        nameTextStyle: {
          color: "#64748b",
        },
        splitLine: {
          lineStyle: {
            color: "#e9eff7",
          },
        },
        axisLabel: {
          color: "#94a3b8",
        },
      },
      {
        type: "value" as const,
        name: showExtendedTrendMetrics ? "客户数/任务数" : "任务数",
        position: "right" as const,
        nameTextStyle: {
          color: "#64748b",
        },
        splitLine: {
          show: false,
        },
        axisLabel: {
          color: "#94a3b8",
        },
      },
    ],
    series,
  };
}

export default function BusinessOverviewPage() {
  const navigate = useNavigate();
  const sourceId = useIframeStore((state) => state.source) || DEFAULT_SOURCE_ID;
  const currentBbkId = useIframeStore((state) => state.bbk);
  const branchScope = useMemo(
    () => getScopedBranchFilter(currentBbkId),
    [currentBbkId],
  );
  const branchOptions = useMemo(
    () => ensureBranchOptions(branchScope.lockedBbkId),
    [branchScope.lockedBbkId],
  );

  const [timeRange, setTimeRange] = useState<TimeRange>("day");
  const [dateRange, setDateRange] = useState<[Dayjs, Dayjs]>([
    dayjs(),
    dayjs(),
  ]);
  // 管理员多选分行；非管理员使用用户所属分行
  const [bbkIds, setBbkIds] = useState<string[]>(() =>
    branchScope.lockedBbkId ? [branchScope.lockedBbkId] : [],
  );

  const [overviewStats, setOverviewStats] = useState<OverviewStats | null>(
    null,
  );
  const [overviewLoading, setOverviewLoading] = useState(false);
  const [trendLoading, setTrendLoading] = useState(false);
  const dashboardRequestIdRef = useRef(0);
  const [trendData, setTrendData] = useState<TrendDatum[]>([]);
  const [activeUsers, setActiveUsers] = useState<UserRow[]>([]);
  const [activePage, setActivePage] = useState(1);
  const [activeHasMore, setActiveHasMore] = useState(true);
  const [activeLoading, setActiveLoading] = useState(false);
  const activeLoadingRef = useRef(false);
  const activeListRef = useRef<HTMLDivElement | null>(null);
  // 用户过滤类型：filtered(过滤IT人员) / all(全部用户)
  const [activeFilterType, setActiveFilterType] = useState<"filtered" | "all">(
    "all",
  );
  const [skills, setSkills] = useState<SkillUsage[]>([]);
  const [skillsPage, setSkillsPage] = useState(1);
  const [skillsHasMore, setSkillsHasMore] = useState(true);
  const [skillsLoading, setSkillsLoading] = useState(false);
  const skillsLoadingRef = useRef(false);
  const skillsListRef = useRef<HTMLDivElement | null>(null);
  const [errorSummaryData, setErrorSummaryData] = useState<ErrorSummary | null>(
    null,
  );
  const [taskStatusSummary, setTaskStatusSummary] =
    useState<TaskStatusSummary | null>(null);
  const [taskStatusLoading, setTaskStatusLoading] = useState(false);
  const [htmlPreviewRefreshKey, setHtmlPreviewRefreshKey] = useState(0);
  const [errorLoading, setErrorLoading] = useState(false);
  const errorLoadingRef = useRef(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [selectedUserId, setSelectedUserId] = useState<string | null>(null);
  const [selectedUserName, setSelectedUserName] = useState<string | null>(null);
  const [skillModalOpen, setSkillModalOpen] = useState(false);
  const [selectedSkillName, setSelectedSkillName] = useState("");
  const [selectedSkillDisplayName, setSelectedSkillDisplayName] = useState<
    string | null
  >(null);
  const [errorModalOpen, setErrorModalOpen] = useState(false);

  const startDateText = useMemo(
    () => dateRange[0].format("YYYY-MM-DD"),
    [dateRange],
  );
  const endDateText = useMemo(
    () => dateRange[1].format("YYYY-MM-DD"),
    [dateRange],
  );
  // 分行筛选参数：直接使用 UI 选择的 bbkIds，空数组表示全部分行
  const effectiveBbkIds = useMemo(() => {
    return bbkIds.length === 0 ? undefined : bbkIds;
  }, [bbkIds]);

  useEffect(() => {
    if (!branchScope.lockedBbkId) {
      return;
    }
    setBbkIds((previous) =>
      previous.length === 1 && previous[0] === branchScope.lockedBbkId
        ? previous
        : [branchScope.lockedBbkId],
    );
  }, [branchScope.lockedBbkId]);
  const cronJobOverviewPath = useMemo(() => {
    const params = new URLSearchParams();
    params.set("start_date", startDateText);
    params.set("end_date", endDateText);
    if (effectiveBbkIds?.length) {
      params.set("bbk_ids", effectiveBbkIds.join(","));
    }
    return `/analytics/cron-job-overview?${params.toString()}`;
  }, [effectiveBbkIds, endDateText, startDateText]);
  const showExtendedTrendMetrics = sourceId === "RMASSIST";

  const transformUserData = useCallback(
    (items: Record<string, unknown>[]): UserRow[] =>
      items.map((item) => ({
        userId: String(item.user_id || ""),
        userName: item.user_name ? String(item.user_name) : undefined,
        bbkId: item.bbk_id ? String(item.bbk_id) : undefined,
        name: String(item.user_name || item.user_id || "-"),
        calls: safeNumber(item.total_conversations),
        tokens: safeNumber(item.total_tokens),
        lastActive: item.last_active
          ? dayjs(String(item.last_active)).format("YYYY-MM-DD HH:mm")
          : "-",
        // 四种口径统计字段
        manualCalls: safeNumber(item.manual_calls),
        cronExecutions: safeNumber(item.cron_executions),
        cronSuccess: safeNumber(item.cron_success),
        cronReads: safeNumber(item.cron_reads),
      })),
    [],
  );

  const fetchDashboard = useCallback(async () => {
    const isSingleDay = dateRange[0].isSame(dateRange[1], "day");
    const requestId = dashboardRequestIdRef.current + 1;
    dashboardRequestIdRef.current = requestId;

    setOverviewLoading(true);
    setTrendLoading(true);

    const overviewRequest = tracingApi
      .getOverview(startDateText, endDateText, effectiveBbkIds?.join(","), {
        detail: "summary",
        timeRange,
      })
      .then((result) => {
        if (dashboardRequestIdRef.current === requestId) {
          setOverviewStats(result);
        }
      })
      .catch((error) => {
        console.error("Failed to fetch overview stats:", error);
        if (dashboardRequestIdRef.current === requestId) {
          message.error("获取总览数据失败");
        }
      })
      .finally(() => {
        if (dashboardRequestIdRef.current === requestId) {
          setOverviewLoading(false);
        }
      });

    const trendRequest = (
      isSingleDay
        ? tracingApi.getHourlyTrend(
            startDateText,
            endDateText,
            effectiveBbkIds?.join(","),
          )
        : tracingApi.getDailyTrend(
            startDateText,
            endDateText,
            effectiveBbkIds?.join(","),
          )
    )
      .then((result) => {
        if (dashboardRequestIdRef.current === requestId) {
          setTrendData(result.trendData || []);
        }
      })
      .catch((error) => {
        console.error("Failed to fetch trend data:", error);
      })
      .finally(() => {
        if (dashboardRequestIdRef.current === requestId) {
          setTrendLoading(false);
        }
      });

    await Promise.allSettled([overviewRequest, trendRequest]);
  }, [dateRange, effectiveBbkIds, endDateText, startDateText, timeRange]);

  const fetchActiveUsers = useCallback(
    async (page: number, append = false) => {
      if (activeLoadingRef.current) {
        return;
      }
      activeLoadingRef.current = true;
      setActiveLoading(true);

      try {
        // 默认按主动使用次数排序，后端返回三个口径数据
        const result = await tracingApi.getUsers(page, 10, {
          start_date: startDateText,
          end_date: endDateText,
          bbk_ids: effectiveBbkIds?.join(","),
          sort_by: "manual_calls",
          filter_user_type: activeFilterType,
        });
        const mappedUsers = transformUserData(
          result.items as unknown as Record<string, unknown>[],
        );
        setActiveUsers((previous) => {
          if (!append) {
            return mappedUsers;
          }
          // 按 userId 去重，避免分页数据漂移导致的重复
          const existingIds = new Set(previous.map((u) => u.userId));
          const dedupedUsers = mappedUsers.filter(
            (u) => !existingIds.has(u.userId),
          );
          return [...previous, ...dedupedUsers];
        });
        const loadedCount = append ? page * 10 : mappedUsers.length;
        setActiveHasMore(loadedCount < (result.total || 0));
      } catch (error) {
        console.error("Failed to fetch active users:", error);
      } finally {
        activeLoadingRef.current = false;
        setActiveLoading(false);
      }
    },
    [
      effectiveBbkIds,
      endDateText,
      startDateText,
      transformUserData,
      activeFilterType,
    ],
  );

  const fetchSkills = useCallback(
    async (page: number = 1, append: boolean = false) => {
      if (skillsLoadingRef.current) {
        return;
      }
      skillsLoadingRef.current = true;
      setSkillsLoading(true);

      try {
        const pageSize = 10;
        const result = await tracingApi.getSkills(page, pageSize, {
          start_date: startDateText,
          end_date: endDateText,
          bbk_ids: effectiveBbkIds?.join(","),
        });
        const rows = result.items || [];

        if (append) {
          setSkills((prev) => {
            // 按 skill_name 去重，避免分页数据漂移导致的重复
            const existingNames = new Set(prev.map((s) => s.skill_name));
            const dedupedSkills = rows.filter(
              (s) => !existingNames.has(s.skill_name),
            );
            return [...prev, ...dedupedSkills];
          });
        } else {
          setSkills(rows);
        }

        const loadedCount = append ? page * pageSize : rows.length;
        setSkillsHasMore(loadedCount < (result.total || 0));
      } catch (error) {
        console.error("Failed to fetch skills:", error);
      } finally {
        skillsLoadingRef.current = false;
        setSkillsLoading(false);
      }
    },
    [effectiveBbkIds, endDateText, startDateText],
  );

  const fetchErrorSummary = useCallback(async () => {
    if (errorLoadingRef.current) {
      return;
    }
    errorLoadingRef.current = true;
    setErrorLoading(true);

    try {
      const result = await tracingApi.getErrorSummary({
        start_date: startDateText,
        end_date: endDateText,
        bbk_ids: effectiveBbkIds?.join(","),
      });
      setErrorSummaryData(result);
    } catch (error) {
      console.error("Failed to fetch error summary:", error);
    } finally {
      errorLoadingRef.current = false;
      setErrorLoading(false);
    }
  }, [effectiveBbkIds, endDateText, startDateText]);

  const fetchTaskStatusSummary = useCallback(async () => {
    setTaskStatusLoading(true);
    try {
      const result = await tracingApi.getTaskStatusSummary({
        start_date: startDateText,
        end_date: endDateText,
        bbk_ids: effectiveBbkIds?.join(","),
      });
      setTaskStatusSummary(result);
    } catch (error) {
      console.error("Failed to fetch task status summary:", error);
    } finally {
      setTaskStatusLoading(false);
    }
  }, [effectiveBbkIds, endDateText, startDateText]);

  useEffect(() => {
    fetchDashboard();
    setSkills([]);
    setSkillsPage(1);
    setSkillsHasMore(true);
    fetchSkills(1, false);
    fetchErrorSummary();
    fetchTaskStatusSummary();
    // 活跃用户请求由独立的 useEffect 处理
  }, [fetchDashboard, fetchErrorSummary, fetchSkills, fetchTaskStatusSummary]);

  // 活跃用户请求独立处理，避免 activeFilterType 变化触发其他请求
  useEffect(() => {
    setActivePage(1);
    setActiveHasMore(true);
    setActiveUsers([]);
    fetchActiveUsers(1, false);
  }, [fetchActiveUsers]);

  const handleModeChange = (nextRange: TimeRange) => {
    setTimeRange(nextRange);
    const today = dayjs();

    if (nextRange === "day") {
      setDateRange([today, today]);
    } else if (nextRange === "week") {
      setDateRange([today.subtract(6, "day"), today]);
    } else if (nextRange === "month") {
      setDateRange([today.subtract(29, "day"), today]);
    }
  };

  const handleDateRangeChange = (
    dates: [Dayjs | null, Dayjs | null] | null,
  ) => {
    if (!dates || !dates[0] || !dates[1]) {
      return;
    }

    const [start, end] = dates;
    const today = dayjs().startOf("day");

    // 检测是否匹配快捷按钮范围
    if (start.isSame(today, "day") && end.isSame(today, "day")) {
      setTimeRange("day");
    } else if (
      start.isSame(today.subtract(6, "day"), "day") &&
      end.isSame(today, "day")
    ) {
      setTimeRange("week");
    } else if (
      start.isSame(today.subtract(29, "day"), "day") &&
      end.isSame(today, "day")
    ) {
      setTimeRange("month");
    } else {
      setTimeRange("custom");
    }

    setDateRange([start, end]);
  };

  const handleActiveScroll = useCallback(
    (event: UIEvent<HTMLDivElement>) => {
      const target = event.currentTarget;
      if (
        target.scrollHeight - target.scrollTop <= target.clientHeight + 40 &&
        activeHasMore &&
        !activeLoadingRef.current
      ) {
        const nextPage = activePage + 1;
        setActivePage(nextPage);
        fetchActiveUsers(nextPage, true);
      }
    },
    [activeHasMore, activePage, fetchActiveUsers],
  );

  const handleSkillsScroll = useCallback(
    (event: UIEvent<HTMLDivElement>) => {
      const target = event.currentTarget;
      if (
        target.scrollHeight - target.scrollTop <= target.clientHeight + 40 &&
        skillsHasMore &&
        !skillsLoadingRef.current
      ) {
        const nextPage = skillsPage + 1;
        setSkillsPage(nextPage);
        fetchSkills(nextPage, true);
      }
    },
    [skillsHasMore, skillsPage, fetchSkills],
  );

  useEffect(() => {
    if (!skillsHasMore || skillsLoading || skills.length === 0) {
      return;
    }

    const list = skillsListRef.current;
    if (!list) {
      return;
    }

    if (list.scrollHeight <= list.clientHeight + 4) {
      const nextPage = skillsPage + 1;
      setSkillsPage(nextPage);
      fetchSkills(nextPage, true);
    }
  }, [fetchSkills, skills, skillsHasMore, skillsLoading, skillsPage]);

  useEffect(() => {
    if (!activeHasMore || activeLoading || activeUsers.length === 0) {
      return;
    }

    const list = activeListRef.current;
    if (!list) {
      return;
    }

    if (list.scrollHeight <= list.clientHeight + 4) {
      const nextPage = activePage + 1;
      setActivePage(nextPage);
      fetchActiveUsers(nextPage, true);
    }
  }, [activeHasMore, activeLoading, activePage, activeUsers, fetchActiveUsers]);

  const disabledDate = (current: Dayjs | null): boolean =>
    !!current && current.isAfter(dayjs().startOf("day"), "day");

  const metricCards = useMemo(
    () => buildMetricCards(overviewStats, taskStatusSummary),
    [overviewStats, taskStatusSummary],
  );
  const executionSummary = useMemo(
    () => buildExecutionSummary(taskStatusSummary),
    [taskStatusSummary],
  );
  const errorSummaryItems = useMemo(
    () => buildErrorSummary(errorSummaryData),
    [errorSummaryData],
  );
  const renderCardLoading = () => (
    <div className={styles.listFootnote} data-testid="overview-panel-loading">
      加载中...
    </div>
  );
  return (
    <div className={styles.businessOverviewPage}>
      <header className={styles.pageHeader}>
        <div className={styles.toolbar}>
          <div className={styles.toolbarLeft}>
            <div className={styles.segmentedControl}>
              <button
                type="button"
                className={
                  timeRange === "day"
                    ? styles.segmentActive
                    : styles.segmentButton
                }
                onClick={() => handleModeChange("day")}
              >
                今天
              </button>
              <button
                type="button"
                className={
                  timeRange === "week"
                    ? styles.segmentActive
                    : styles.segmentButton
                }
                onClick={() => handleModeChange("week")}
              >
                近7天
              </button>
              <button
                type="button"
                className={
                  timeRange === "month"
                    ? styles.segmentActive
                    : styles.segmentButton
                }
                onClick={() => handleModeChange("month")}
              >
                近30天
              </button>
            </div>

            <div className={styles.dateRangePanel}>
              <DatePicker.RangePicker
                className={styles.rangePicker}
                value={dateRange}
                onChange={handleDateRangeChange}
                format="YYYY-MM-DD"
                suffixIcon={<CalendarDays size={16} />}
                disabledDate={disabledDate}
                allowClear={false}
              />
            </div>
          </div>

          <div className={styles.toolbarRight}>
            <Select
              className={styles.scopeSelect}
              mode="multiple"
              value={bbkIds}
              onChange={(value) => {
                if (!branchScope.lockedBbkId) {
                  setBbkIds(value);
                }
              }}
              placeholder="全部分行"
              disabled={!branchScope.isHeadOffice}
              maxTagCount={branchScope.isHeadOffice ? "responsive" : 1}
              maxTagPlaceholder={
                branchScope.isHeadOffice
                  ? (omittedValues) => (
                      <Tooltip
                        title={omittedValues
                          .map((item) => {
                            const value = String(item.value ?? "");
                            return BBK_ID_TO_NAME_MAP[value] || value;
                          })
                          .join("、")}
                      >
                        <span>+{omittedValues.length} 个分行</span>
                      </Tooltip>
                    )
                  : undefined
              }
              allowClear={branchScope.isHeadOffice}
              showSearch
              filterOption={(input, option) => {
                const searchValue = input.toLowerCase();
                const optionValue = String(option?.value ?? "");
                const optionLabel = BBK_ID_TO_NAME_MAP[optionValue] || "";
                // 支持按分行号或分行名搜索
                return (
                  optionValue.toLowerCase().includes(searchValue) ||
                  optionLabel.toLowerCase().includes(searchValue)
                );
              }}
            >
              {branchOptions.map((item) => (
                <Option key={item.value} value={item.value}>
                  {item.label}
                </Option>
              ))}
            </Select>
            <button
              type="button"
              className={styles.refreshButton}
              onClick={() => {
                fetchDashboard();
                fetchActiveUsers(1, false);
                fetchSkills();
                fetchErrorSummary();
                fetchTaskStatusSummary();
                setHtmlPreviewRefreshKey((value) => value + 1);
              }}
            >
              <RotateCw size={14} />
              刷新
            </button>
          </div>
        </div>
      </header>

      <section className={styles.metricGrid} data-testid="overview-metric-grid">
        {metricCards.map((card) => {
          const MetricIcon =
            iconMap[card.key as keyof typeof iconMap] || Sparkles;

          return (
            <article
              key={card.key}
              className={styles.metricPanel}
              data-testid="overview-metric-card"
            >
              {overviewLoading ? (
                renderCardLoading()
              ) : (
                <>
                  <div className={styles.metricHeader}>
                    <span
                      className={styles.metricIcon}
                      style={{
                        background: `linear-gradient(180deg, ${card.accentColor} 0%, ${card.accentColor}dd 100%)`,
                      }}
                    >
                      <MetricIcon size={20} strokeWidth={2.2} />
                    </span>
                    <div className={styles.metricText}>
                      <div className={styles.metricTitle}>{card.title}</div>
                      <div className={styles.metricValue}>{card.valueText}</div>
                      <div
                        className={
                          card.changeDirection === "up"
                            ? styles.metricChangeUp
                            : card.changeDirection === "down"
                            ? styles.metricChangeDown
                            : styles.metricChangeFlat
                        }
                      >
                        环比
                        {card.changeDirection === "up" && (
                          <TrendingUp size={14} />
                        )}
                        {card.changeDirection === "down" && (
                          <TrendingDown size={14} />
                        )}
                        {card.changeText}
                      </div>
                    </div>
                  </div>
                  <div className={styles.breakdownTitle}>Top5分行</div>
                  {card.breakdown && card.breakdown.length > 0 ? (
                    <div className={styles.breakdownList}>
                      {card.breakdown.map((item) => (
                        <div
                          key={`${card.key}-${item.name}`}
                          className={styles.breakdownRow}
                        >
                          <span className={styles.breakdownName}>
                            {truncateName(item.name, 6)}
                          </span>
                          <div className={styles.breakdownTrack}>
                            <div
                              className={styles.breakdownBar}
                              style={{
                                width: `${Math.max(item.value, 10)}%`,
                                background: card.accentColor,
                              }}
                            />
                          </div>
                          <span className={styles.breakdownValue}>
                            {item.valueText}
                          </span>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className={styles.emptyBreakdown}>
                      <Database className={styles.emptyBreakdownIcon} />
                      <span className={styles.emptyBreakdownText}>
                        暂无分行数据
                      </span>
                    </div>
                  )}
                </>
              )}
            </article>
          );
        })}
      </section>

      <section
        className={styles.analysisGrid}
        data-testid="overview-analysis-grid"
      >
        <article className={styles.panelLarge}>
          <div className={styles.panelHeader}>
            <h3 className={styles.panelTitle}>调用量趋势</h3>
          </div>
          {trendLoading ? (
            renderCardLoading()
          ) : (
            <div className={styles.trendChart}>
              <ReactECharts
                className={styles.trendChartCanvas}
                option={buildTrendChartOption(
                  trendData,
                  showExtendedTrendMetrics,
                )}
                style={{ height: 280, width: "100%", gridColumn: "1 / -1" }}
              />
            </div>
          )}
        </article>

        <article className={styles.panelMedium}>
          <div className={styles.panelHeader}>
            <h3 className={styles.panelTitle}>活跃用户排行榜</h3>
            <div className={styles.filterTab}>
              <span
                className={
                  activeFilterType === "all"
                    ? styles.filterTabActive
                    : styles.filterTabItem
                }
                onClick={() => {
                  if (activeFilterType !== "all") {
                    setActiveFilterType("all");
                    setActiveUsers([]);
                    setActivePage(1);
                    setActiveHasMore(true);
                  }
                }}
              >
                全部
              </span>
              <span
                className={
                  activeFilterType === "filtered"
                    ? styles.filterTabActive
                    : styles.filterTabItem
                }
                onClick={() => {
                  if (activeFilterType !== "filtered") {
                    setActiveFilterType("filtered");
                    setActiveUsers([]);
                    setActivePage(1);
                    setActiveHasMore(true);
                  }
                }}
              >
                过滤IT人员
              </span>
            </div>
          </div>
          <div className={styles.rankHeader}>
            <span>排名</span>
            <span>用户</span>
            <span>任务执行</span>
            <span>任务成功</span>
            <span>结果查看</span>
            <span>主动调用</span>
          </div>
          <div
            ref={activeListRef}
            className={styles.rankList}
            onScroll={handleActiveScroll}
          >
            {activeLoading && activeUsers.length === 0 ? (
              <div className={styles.listFootnote}>加载中...</div>
            ) : activeUsers.length === 0 ? (
              <div className={styles.emptyState}>暂无用户数据</div>
            ) : (
              activeUsers.map((item, index) => {
                const rank = index + 1;
                const rankClass =
                  rank === 1
                    ? styles.rankBadgeGold
                    : rank === 2
                    ? styles.rankBadgeSilver
                    : rank === 3
                    ? styles.rankBadgeBronze
                    : styles.rankBadge;

                // 格式化显示：分行名称/用户姓名(用户ID)
                const displayParts: string[] = [];
                if (item.bbkId && getBbkDisplayName(item.bbkId) !== "-") {
                  displayParts.push(getBbkDisplayName(item.bbkId));
                }
                if (item.userName) {
                  displayParts.push(item.userName);
                }
                const displayName =
                  displayParts.length > 0
                    ? `${displayParts.join("/")}(${item.userId})`
                    : item.userId;

                return (
                  <button
                    key={`${item.userId}-${rank}`}
                    type="button"
                    className={styles.rankRow}
                    onClick={() => {
                      setSelectedUserId(item.userId);
                      setSelectedUserName(item.userName);
                      setModalOpen(true);
                    }}
                  >
                    <span className={rankClass}>{rank}</span>
                    <Tooltip title={displayName} placement="top">
                      <span className={styles.rankUser}>{displayName}</span>
                    </Tooltip>
                    <span className={styles.rankCalls}>
                      {formatNumber(item.cronExecutions)}
                    </span>
                    <span className={styles.rankCalls}>
                      {formatNumber(item.cronSuccess)}
                    </span>
                    <span className={styles.rankCalls}>
                      {formatNumber(item.cronReads)}
                    </span>
                    <span className={styles.rankCalls}>
                      {formatNumber(item.manualCalls)}
                    </span>
                  </button>
                );
              })
            )}
            {activeLoading && activeUsers.length > 0 && (
              <div className={styles.listFootnote}>加载中...</div>
            )}
          </div>
        </article>
      </section>

      <section
        className={styles.summaryGrid}
        data-testid="overview-summary-grid"
      >
        <article className={styles.panelLarge}>
          <div className={styles.panelHeader}>
            <h3 className={styles.panelTitle}>任务执行概览</h3>
            <button
              type="button"
              className={styles.detailLink}
              onClick={() => navigate(cronJobOverviewPath)}
            >
              查看详情
              <ChevronRight size={14} />
            </button>
          </div>
          {taskStatusLoading ? (
            renderCardLoading()
          ) : (
            <div className={styles.donutLayout}>
              <div className={styles.donutColumn}>
                <div className={styles.donutWrap}>
                  <svg viewBox="0 0 120 120" className={styles.donutSvg}>
                    <circle
                      cx="60"
                      cy="60"
                      r="45"
                      className={styles.donutTrack}
                    />
                    {buildDonutSegments(executionSummary).map((item) => (
                      <circle
                        key={item.key}
                        cx="60"
                        cy="60"
                        r="45"
                        className={styles.donutSegment}
                        style={{
                          stroke: item.color,
                          strokeDasharray: item.dasharray,
                          strokeDashoffset: item.dashoffset,
                        }}
                      />
                    ))}
                  </svg>
                  <div className={styles.donutCenter}>
                    <strong>
                      {formatNumber(taskStatusSummary?.total_tasks ?? 0)}
                    </strong>
                    <span>总任务数</span>
                  </div>
                </div>
                <div className={styles.donutLegend}>
                  {executionSummary.map((item) => {
                    const total = Math.max(
                      executionSummary.reduce((sum, row) => sum + row.value, 0),
                      1,
                    );

                    return (
                      <div key={item.key} className={styles.donutLegendItem}>
                        <span
                          className={styles.donutLegendDot}
                          style={{ background: item.color }}
                        />
                        <span>{item.label}</span>
                        <span className={styles.donutLegendValue}>
                          {formatNumber(item.value)}&nbsp;(
                          {formatPercent((item.value / total) * 100)})
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
              <TaskFunnel taskStatusSummary={taskStatusSummary} />
            </div>
          )}
        </article>

        <article className={styles.panelMedium}>
          <div className={styles.panelHeader}>
            <h3 className={styles.panelTitle}>技能使用排行榜</h3>
          </div>
          <div className={styles.skillRankHeader}>
            <span>排名</span>
            <span>技能</span>
            <span>调用次数</span>
          </div>
          <div
            ref={skillsListRef}
            className={styles.rankList}
            onScroll={handleSkillsScroll}
          >
            {skillsLoading && skills.length === 0 ? (
              <div className={styles.listFootnote}>加载中...</div>
            ) : skills.length === 0 ? (
              <div className={styles.emptyState}>暂无技能数据</div>
            ) : (
              skills.map((skill, index) => {
                const rank = index + 1;
                const rankClass =
                  rank === 1
                    ? styles.rankBadgeGold
                    : rank === 2
                    ? styles.rankBadgeSilver
                    : rank === 3
                    ? styles.rankBadgeBronze
                    : styles.rankBadge;
                const descLen = skill.skill_description?.length || 0;
                const tooltipWidth =
                  descLen <= 30
                    ? 240
                    : descLen <= 60
                    ? 320
                    : descLen <= 100
                    ? 400
                    : 520;
                const skillLabel = displaySkillName(skill);
                return (
                  <button
                    key={`${skill.skill_name}-${rank}`}
                    type="button"
                    className={styles.skillRankRow}
                    onClick={() => {
                      setSelectedSkillName(skill.skill_name);
                      setSelectedSkillDisplayName(skill.cn_name ?? null);
                      setSkillModalOpen(true);
                    }}
                  >
                    <span className={rankClass}>{rank}</span>
                    <Tooltip
                      placement="top"
                      overlayInnerStyle={{
                        width: tooltipWidth,
                        maxWidth: tooltipWidth,
                      }}
                      title={
                        skill.skill_description ? (
                          <div className={styles.skillTooltip}>
                            <div className={styles.skillTooltipName}>
                              {skillLabel}
                            </div>
                            <div className={styles.skillTooltipDesc}>
                              {skill.skill_description}
                            </div>
                          </div>
                        ) : (
                          skillLabel
                        )
                      }
                    >
                      <span className={styles.rankUser}>
                        {truncateName(skillLabel, 20)}
                      </span>
                    </Tooltip>
                    <span className={styles.skillRankCalls}>
                      {formatNumber(skill.count)}
                    </span>
                  </button>
                );
              })
            )}
            {skillsLoading && skills.length > 0 && (
              <div className={styles.listFootnote}>加载中...</div>
            )}
          </div>
        </article>

        <article className={styles.panelLarge}>
          <div className={styles.panelHeader}>
            <h3 className={styles.panelTitle}>报错分析</h3>
            <button
              type="button"
              className={styles.detailLink}
              onClick={() => setErrorModalOpen(true)}
            >
              查看详情
              <ChevronRight size={14} />
            </button>
          </div>
          {errorLoading ? (
            renderCardLoading()
          ) : (
            <div className={styles.donutLayoutCompact}>
              <div className={styles.donutCompact}>
                <svg viewBox="0 0 120 120" className={styles.donutCompactSvg}>
                  <circle
                    cx="60"
                    cy="60"
                    r="45"
                    className={styles.donutTrack}
                  />
                  {buildDonutSegments(errorSummaryItems).map((item) => (
                    <circle
                      key={item.key}
                      cx="60"
                      cy="60"
                      r="45"
                      className={styles.donutSegment}
                      style={{
                        stroke: item.color,
                        strokeDasharray: item.dasharray,
                        strokeDashoffset: item.dashoffset,
                      }}
                    />
                  ))}
                </svg>
                <div className={styles.donutCenter}>
                  <strong>
                    {formatNumber(safeNumber(errorSummaryData?.total_errors))}
                  </strong>
                  <span>报错总数</span>
                </div>
              </div>
              <div className={styles.legendHorizontal}>
                <div className={styles.legendGroup}>
                  {errorSummaryItems.map((item) => {
                    const total = Math.max(
                      errorSummaryItems.reduce(
                        (sum, row) => sum + row.value,
                        0,
                      ),
                      1,
                    );
                    const label = (
                      <span
                        className={`${styles.legendLabel} ${
                          item.key === "model-error" && item.value > 0
                            ? styles.legendLabelHoverable
                            : ""
                        }`}
                      >
                        <i style={{ background: item.color }} />
                        {item.label}
                      </span>
                    );

                    return (
                      <div key={item.key} className={styles.legendRow}>
                        {item.key === "model-error" && item.value > 0 ? (
                          <Tooltip
                            placement="top"
                            title={renderModelErrorCodeTooltip(
                              errorSummaryData,
                            )}
                          >
                            {label}
                          </Tooltip>
                        ) : (
                          label
                        )}
                        <span className={styles.legendValue}>
                          {formatNumber(item.value)} (
                          {formatPercent((item.value / total) * 100)})
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>
          )}
        </article>
      </section>

      <UserDetailModal
        open={modalOpen}
        userId={selectedUserId}
        userName={selectedUserName}
        startDate={startDateText}
        endDate={endDateText}
        bbkIds={effectiveBbkIds?.join(",")}
        onClose={() => {
          setModalOpen(false);
          setSelectedUserId(null);
        }}
      />

      <SkillDetailModal
        open={skillModalOpen}
        skillName={selectedSkillName}
        skillDisplayName={selectedSkillDisplayName ?? undefined}
        startDate={startDateText}
        endDate={endDateText}
        bbkIds={effectiveBbkIds?.join(",")}
        onClose={() => {
          setSkillModalOpen(false);
          setSelectedSkillName("");
          setSelectedSkillDisplayName(null);
        }}
      />

      <ErrorDetailModal
        open={errorModalOpen}
        startDate={startDateText}
        endDate={endDateText}
        bbkIds={effectiveBbkIds?.join(",")}
        onClose={() => setErrorModalOpen(false)}
      />
    </div>
  );
}
