import {
  ArrowLeft,
  ArrowDownUp,
  Banknote,
  CalendarDays,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  ChevronRight,
  Download,
  Eye,
  FileText,
  Landmark,
  RefreshCw,
  UserRoundCheck,
  type LucideIcon,
} from "lucide-react";
import {
  DatePicker,
  Input,
  Modal,
  Pagination,
  Select,
  Spin,
  Table,
  Tooltip,
} from "antd";
import { WarningOutlined } from "@ant-design/icons";
import dayjs, { type Dayjs } from "dayjs";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type Ref,
} from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  monitorApi,
  type ExecutionItem,
  type CronJobOverviewFailureReason,
  type CronJobOverviewDateFilters,
  type CronJobOverviewPageData,
  type BranchManagerSummaryItem,
  type ManagerSkillItem,
  type ManagerCustomerItem,
  type BranchSkillItem,
  type BranchSkillManagerItem,
  type BranchSkillManagerCustomerItem,
  type CronBranchTaskRankingItem,
} from "../../../api/modules/monitor";
import { BBK_ID_TO_NAME_MAP } from "../../../constants/bbk";
import {
  ensureBranchOptions,
  getScopedBranchFilter,
} from "../../../utils/branchScope";
import { useIframeStore } from "../../../stores/iframeStore";
import styles from "./index.module.less";

const { Option } = Select;

type TimeRange = "day" | "week" | "month" | "custom";
type SummaryMetricTone = "blue" | "green" | "orange" | "red";
type SortDirection = "asc" | "desc";
type BranchRankingSortKey = Exclude<
  keyof CronJobOverviewPageData["branchRankingRows"][number],
  "rank" | "bbkId" | "branchName"
>;
type BranchManagerSortableMetric = Exclude<
  keyof BranchManagerSummaryItem,
  "user_id" | "user_name"
>;

const failureReasonOptions = [
  "子任务执行失败",
  "渠道不存在",
  "token过期",
  "密文长度错误",
  "智能体请求校验失败",
  "模型错误",
  "其他",
] as const;

type FailureReason = (typeof failureReasonOptions)[number];

const quickTooltipProps = {
  mouseEnterDelay: 0,
  mouseLeaveDelay: 0,
} as const;

const DRILL_DOWN_TABLE_SCROLL = {
  x: "max-content",
  y: 300,
} as const;

const formatRatioPercent = (value?: number | null) =>
  `${((value ?? 0) * 100).toFixed(2)}%`;

const parseRankingValue = (value: string | number) => {
  const numericValue = Number(String(value).replace(/[%\s,]/g, ""));
  return Number.isFinite(numericValue) ? numericValue : 0;
};

const branchManagerMetricSorter =
  (key: BranchManagerSortableMetric) =>
  (left: BranchManagerSummaryItem, right: BranchManagerSummaryItem) =>
    Number(left[key] ?? 0) - Number(right[key] ?? 0);

type SummaryMetricDefinition = {
  key: string;
  title: string;
  unit?: string;
  footerLabel?: string;
  tone: SummaryMetricTone;
  icon: LucideIcon;
};

type SummaryMetricView = SummaryMetricDefinition & {
  value: string;
  footerValue?: string;
  hintValue?: string;
};

// Multi-footer metric for "查看方案任务率" card
type ReportMetricDefinition = {
  key: string;
  title: string;
  unit?: string;
  tone: SummaryMetricTone;
  icon: LucideIcon;
  subItems: Array<{
    key: string;
    label: string;
  }>;
};

type ReportMetricView = ReportMetricDefinition & {
  value: string;
  hintValue?: string;
  subValues: Record<string, string>;
};

const summaryMetricDefinitions: SummaryMetricDefinition[] = [
  {
    key: "branches",
    title: "覆盖分行数",
    unit: "家",
    footerLabel: "客户经理数",
    tone: "blue",
    icon: Landmark,
  },
  {
    key: "tasks",
    title: "定时任务数",
    unit: "个",
    footerLabel: "任务执行次数",
    tone: "blue",
    icon: CalendarDays,
  },
  {
    key: "success",
    title: "执行成功率",
    unit: "%",
    footerLabel: "成功执行数/失败执行数",
    tone: "green",
    icon: CheckCircle2,
  },
  {
    key: "read",
    title: "任务已读率",
    unit: "%",
    footerLabel: "已读任务数",
    tone: "orange",
    icon: Eye,
  },
];

const reportMetricDefinition: ReportMetricDefinition = {
  key: "report",
  title: "查看方案任务率",
  unit: "%",
  tone: "blue",
  icon: FileText,
  subItems: [
    { key: "report_count", label: "查看方案任务数" },
    { key: "insight_count", label: "去洞察任务数" },
    { key: "phone_count", label: "去电访任务数" },
  ],
};

const emptyOverviewData: CronJobOverviewPageData = {
  summaryMetrics: [],
  branchRankingRows: [],
  failureReasons: [],
  anomalySummary: {
    affectedBranches: "-",
    affectedBranchesUnit: "家",
    affectedManagers: "-",
    affectedManagersUnit: "人",
  },
  anomalyRankRows: [],
};

function renderPanelLoading() {
  return (
    <div className={styles.listFootnote} data-testid="cron-panel-loading">
      加载中...
    </div>
  );
}

function isValidDateParam(value: string | null) {
  if (!value) {
    return false;
  }
  const parsed = dayjs(value);
  return parsed.isValid() && parsed.format("YYYY-MM-DD") === value;
}

function getInitialDateRange(searchParams: URLSearchParams): [Dayjs, Dayjs] {
  const startDate = searchParams.get("start_date");
  const endDate = searchParams.get("end_date");

  if (isValidDateParam(startDate) && isValidDateParam(endDate)) {
    return [dayjs(startDate), dayjs(endDate)];
  }

  return [dayjs(), dayjs()];
}

function getTimeRangeForDateRange([start, end]: [Dayjs, Dayjs]): TimeRange {
  const today = dayjs();

  if (start.isSame(today, "day") && end.isSame(today, "day")) {
    return "day";
  }
  if (
    start.isSame(today.subtract(6, "day"), "day") &&
    end.isSame(today, "day")
  ) {
    return "week";
  }
  if (
    start.isSame(today.subtract(29, "day"), "day") &&
    end.isSame(today, "day")
  ) {
    return "month";
  }
  return "custom";
}

function getInitialBbkIds(searchParams: URLSearchParams) {
  const bbkIds = searchParams.get("bbk_ids");
  return bbkIds
    ? bbkIds
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean)
    : [];
}

const classifyFailureReason = (
  errorMessage: string,
  asyncStatus?: string | null,
  status?: string,
): FailureReason => {
  // 只有当 status='success' AND async_status='error' 时才是子任务执行失败
  // 如果 status='error'，即使 async_status='error' 也是其他类型的失败
  if (status === "success" && asyncStatus === "error") {
    return "子任务执行失败";
  }

  const message = errorMessage || "";
  const normalizedMessage = message.toLowerCase();

  if (message.includes("channel not found")) {
    return "渠道不存在";
  }
  if (message.includes("cron auth user_info is expired")) {
    return "token过期";
  }
  if (message.includes("Illegal Argument")) {
    return "密文长度错误";
  }
  if (normalizedMessage.includes("validation error for agentrequest")) {
    return "智能体请求校验失败";
  }
  if (message.includes("Agent execution did not complete")) {
    return "模型错误";
  }
  return "其他";
};

function SummaryCard({ metric }: { metric: SummaryMetricView }) {
  const Icon = metric.icon;

  return (
    <article className={`${styles.summaryCard} ${styles[metric.tone]}`}>
      <div className={styles.summaryMain}>
        <span className={styles.summaryIcon}>
          <Icon size={28} />
        </span>
        <div className={styles.summaryText}>
          <span className={styles.summaryTitle}>{metric.title}</span>
          <div className={styles.summaryValueRow}>
            <strong>
              {metric.value}
              {metric.unit ? <em>{metric.unit}</em> : null}
            </strong>
            {metric.hintValue ? (
              <span className={styles.summaryHint}>{metric.hintValue}</span>
            ) : null}
          </div>
        </div>
      </div>
      {metric.footerLabel && metric.footerValue ? (
        <div className={styles.summaryFooter}>
          <span>{metric.footerLabel}</span>
          <strong>{metric.footerValue}</strong>
        </div>
      ) : null}
    </article>
  );
}

function ReportSummaryCard({ metric }: { metric: ReportMetricView }) {
  const Icon = metric.icon;

  return (
    <article className={`${styles.summaryCard} ${styles[metric.tone]}`}>
      <div className={styles.summaryMain}>
        <span className={styles.summaryIcon}>
          <Icon size={28} />
        </span>
        <div className={styles.summaryText}>
          <span className={styles.summaryTitle}>{metric.title}</span>
          <strong>
            {metric.value}
            {metric.unit ? <em>{metric.unit}</em> : null}
            {metric.hintValue ? (
              <span className={styles.summaryHint}>{metric.hintValue}</span>
            ) : null}
          </strong>
        </div>
      </div>
      <div className={styles.reportSubGrid}>
        {metric.subItems.map((item) => {
          const value = metric.subValues[item.key] || "-";
          return (
            <div key={item.key} className={styles.reportSubItem}>
              <span className={styles.reportSubLabel}>{item.label}</span>
              <strong className={styles.reportSubValue}>{value}</strong>
            </div>
          );
        })}
      </div>
    </article>
  );
}

function TaskRankingTable({
  data,
  loading,
  onRowClick,
  selectedBranchId,
}: {
  data: CronBranchTaskRankingItem[];
  loading: boolean;
  onRowClick: (bbkId: string, bbkName: string) => void;
  selectedBranchId: string | null;
}) {
  return (
    <section className={`${styles.panel} ${styles.behaviorPanel}`}>
      {loading ? (
        renderPanelLoading()
      ) : (
        <div className={styles.tableScroller}>
          <table className={styles.behaviorTable}>
            <colgroup>
              <col style={{ width: 42 }} />
              <col style={{ width: 95 }} />
              <col style={{ width: 85 }} />
              <col style={{ width: 75 }} />
              <col style={{ width: 75 }} />
              <col style={{ width: 60 }} />
              <col style={{ width: 75 }} />
              <col style={{ width: 120 }} />
              <col style={{ width: 120 }} />
              <col style={{ width: 120 }} />
              <col style={{ width: 75 }} />
            </colgroup>
            <thead>
              <tr>
                <th className={styles.indexCell} />
                <th>分行名称</th>
                <th>覆盖客户经理数</th>
                <th>定时任务数</th>
                <th>成功执行数</th>
                <th>成功率</th>
                <th>已读任务数</th>
                <th>查看方案任务数/点击数</th>
                <th>点击去洞察任务数/点击数</th>
                <th>点击去电访任务数/点击数</th>
                <th>报错执行次数</th>
              </tr>
            </thead>
            <tbody>
              {data.map((row, index) => {
                const isSelected = row.bbk_id === selectedBranchId;
                return (
                  <tr
                    key={`${row.bbk_id}-${index}`}
                    className={
                      `${isSelected ? styles.selectedRow : ""} ${
                        styles.clickableRow
                      }`.trim() || undefined
                    }
                    onClick={() => onRowClick(row.bbk_id, row.bbk_name)}
                  >
                    <td className={styles.indexCell}>{index + 1}</td>
                    <td className={styles.branchNameLink}>
                      <span>{row.bbk_name}</span>
                    </td>
                    <td>{row.manager_count}</td>
                    <td>{row.total_tasks}</td>
                    <td>{row.success_count}</td>
                    <td>{row.success_rate.toFixed(1)}%</td>
                    <td>{row.read_tasks}</td>
                    <td>
                      {row.plan_count}/{row.plan_clicks}
                    </td>
                    <td>
                      {row.insight_count}/{row.insight_clicks}
                    </td>
                    <td>
                      {row.phone_count}/{row.phone_clicks}
                    </td>
                    <td>{row.error_count}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function RankingTable({
  data,
  loading = false,
  onRowClick,
  selectedBranchId,
  tableRef,
}: {
  data: CronJobOverviewPageData["branchRankingRows"];
  loading?: boolean;
  onRowClick: (bbkId: string, bbkName: string) => void;
  selectedBranchId: string | null;
  tableRef: Ref<HTMLTableElement>;
}) {
  const [sortConfig, setSortConfig] = useState<{
    key: BranchRankingSortKey;
    direction: SortDirection;
  } | null>(null);
  const sortedData = useMemo(() => {
    if (!sortConfig) {
      return data;
    }
    return [...data].sort((left, right) => {
      const leftValue = parseRankingValue(left[sortConfig.key]);
      const rightValue = parseRankingValue(right[sortConfig.key]);
      const result = leftValue - rightValue;
      return sortConfig.direction === "asc" ? result : -result;
    });
  }, [data, sortConfig]);

  const handleSort = (key: BranchRankingSortKey) => {
    setSortConfig((current) => {
      if (!current || current.key !== key) {
        return { key, direction: "desc" };
      }
      if (current.direction === "desc") {
        return { key, direction: "asc" };
      }
      return null;
    });
  };

  const renderSortableHeader = (title: string, key: BranchRankingSortKey) => {
    const isActive = sortConfig?.key === key;
    const Icon = isActive
      ? sortConfig?.direction === "desc"
        ? ChevronDown
        : ChevronUp
      : ArrowDownUp;
    return (
      <span className={styles.sortableHeader}>
        {title}
        <button
          type="button"
          className={`${styles.sortButton} ${
            isActive ? styles.sortButtonActive : ""
          }`.trim()}
          aria-label={`${title}排序`}
          aria-pressed={isActive}
          title={`${title}排序`}
          onClick={(event) => {
            event.stopPropagation();
            handleSort(key);
          }}
        >
          <Icon size={13} aria-hidden="true" />
        </button>
      </span>
    );
  };

  const renderGroupHeader = (title: string, span: number) => (
    <th colSpan={span} className={styles.groupHeader}>
      {title}
    </th>
  );

  return (
    <section className={`${styles.panel} ${styles.behaviorPanel}`}>
      {loading ? (
        renderPanelLoading()
      ) : (
        <div className={styles.tableScroller}>
          <table
            ref={tableRef}
            className={`${styles.behaviorTable} ${styles.branchDimensionTable}`}
          >
            <colgroup>
              <col style={{ width: 30 }} />
              <col style={{ width: 80 }} />
              <col style={{ width: 60 }} />
              <col style={{ width: 65 }} />
              <col style={{ width: 72 }} />
              <col style={{ width: 72 }} />
              <col style={{ width: 72 }} />
              <col style={{ width: 80 }} />
              <col style={{ width: 80 }} />
              <col style={{ width: 80 }} />
              <col style={{ width: 80 }} />
              <col style={{ width: 75 }} />
              <col style={{ width: 80 }} />
              <col style={{ width: 75 }} />
              <col style={{ width: 80 }} />
              <col style={{ width: 85 }} />
              <col style={{ width: 75 }} />
              <col style={{ width: 70 }} />
              <col style={{ width: 65 }} />
              <col style={{ width: 65 }} />
              <col style={{ width: 65 }} />
              <col style={{ width: 70 }} />
            </colgroup>
            <thead>
              <tr>
                <th rowSpan={2} className={styles.indexCell} />
                <th rowSpan={2}>分行名称</th>
                {renderGroupHeader("任务信息", 4)}
                {renderGroupHeader("by客户经理", 9)}
                {renderGroupHeader("by客户", 7)}
              </tr>
              <tr>
                <th>{renderSortableHeader("技能数", "skillCount")}</th>
                <th>{renderSortableHeader("任务总数", "totalTasks")}</th>
                <th>
                  {renderSortableHeader("成功执行任务总数", "successCount")}
                </th>
                <th>{renderSortableHeader("已读任务数", "readTasks")}</th>
                <th>
                  {renderSortableHeader("涉及用户数", "involvedManagers")}
                </th>
                <th>
                  {renderSortableHeader(
                    "查看结果的用户数",
                    "resultViewManagers",
                  )}
                </th>
                <th>
                  {renderSortableHeader(
                    "RM查看Claw任务结果比例",
                    "resultViewManagerRate",
                  )}
                </th>
                <th>
                  {renderSortableHeader("查看客户级方案用户数", "planManagers")}
                </th>
                <th>
                  {renderSortableHeader(
                    "查看结果的RM中点击客户级方案的比例",
                    "planManagerRate",
                  )}
                </th>
                <th>
                  {renderSortableHeader("点击去洞察用户数", "insightManagers")}
                </th>
                <th>
                  {renderSortableHeader(
                    "查看结果的RM中点击去洞察的比例",
                    "insightManagerRate",
                  )}
                </th>
                <th>
                  {renderSortableHeader("点击去电访的用户数", "phoneManagers")}
                </th>
                <th>
                  {renderSortableHeader(
                    "查看结果的RM中点击去电访的比例",
                    "phoneManagerRate",
                  )}
                </th>
                <th>
                  {renderSortableHeader(
                    "Claw任务推荐的客户数",
                    "recommendedCustomers",
                  )}
                </th>
                <th>
                  {renderSortableHeader(
                    "被用户查看的客户数",
                    "viewedCustomers",
                  )}
                </th>
                <th>
                  {renderSortableHeader("客户查看率", "viewedCustomerRate")}
                </th>
                <th>
                  {renderSortableHeader("去洞察客户数", "insightCustomers")}
                </th>
                <th>
                  {renderSortableHeader("去电访客户数", "phoneCustomers")}
                </th>
                <th>
                  {renderSortableHeader("接触客户数", "contactedCustomers")}
                </th>
                <th>{renderSortableHeader("接触客户率", "contactRate")}</th>
              </tr>
            </thead>
            <tbody>
              {sortedData.map((row, index) => {
                const isClickable = row.bbkId && row.rank !== "...";
                const isSelected = row.bbkId && row.bbkId === selectedBranchId;
                const rank = sortConfig ? index + 1 : row.rank;

                return (
                  <tr
                    key={`${row.branchName}-${index}`}
                    className={
                      `${row.rank === "..." ? styles.mutedRow : ""} ${
                        isSelected ? styles.selectedRow : ""
                      } ${isClickable ? styles.clickableRow : ""}`.trim() ||
                      undefined
                    }
                    onClick={() => {
                      if (isClickable) {
                        onRowClick(row.bbkId, row.branchName);
                      }
                    }}
                  >
                    <td className={styles.indexCell}>{rank}</td>
                    <td
                      className={
                        isClickable ? styles.branchNameLink : styles.branchName
                      }
                    >
                      <span>{row.branchName}</span>
                    </td>
                    <td>{row.skillCount}</td>
                    <td>{row.totalTasks}</td>
                    <td>{row.successCount}</td>
                    <td>{row.readTasks}</td>
                    <td>{row.involvedManagers}</td>
                    <td>{row.resultViewManagers}</td>
                    <td>{row.resultViewManagerRate}</td>
                    <td>{row.planManagers}</td>
                    <td>{row.planManagerRate}</td>
                    <td>{row.insightManagers}</td>
                    <td>{row.insightManagerRate}</td>
                    <td>{row.phoneManagers}</td>
                    <td>{row.phoneManagerRate}</td>
                    <td>{row.recommendedCustomers}</td>
                    <td>{row.viewedCustomers}</td>
                    <td>{row.viewedCustomerRate}</td>
                    <td>{row.insightCustomers}</td>
                    <td>{row.phoneCustomers}</td>
                    <td>{row.contactedCustomers}</td>
                    <td>{row.contactRate}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function DonutChart({ items }: { items: CronJobOverviewFailureReason[] }) {
  const total = items.reduce((sum, item) => sum + item.count, 0);
  const radius = 44;
  const circumference = 2 * Math.PI * radius;
  let offset = 0;

  return (
    <div className={styles.donutWrap}>
      <svg
        className={styles.donutChart}
        viewBox="0 0 116 116"
        role="img"
        aria-label="报错原因分布"
      >
        <circle
          cx="58"
          cy="58"
          r={radius}
          fill="none"
          stroke="#edf3fb"
          strokeWidth="16"
        />
        {items.map((item) => {
          const dash = total > 0 ? (item.count / total) * circumference : 0;
          const segmentStyle = {
            "--dash": dash,
            "--gap": circumference - dash,
            "--offset": -offset,
            "--segment-color": item.color,
          } as CSSProperties;
          offset += dash;

          return (
            <circle
              key={item.name}
              className={styles.donutSegment}
              cx="58"
              cy="58"
              r={radius}
              fill="none"
              strokeWidth="16"
              style={segmentStyle}
            />
          );
        })}
      </svg>
      <div className={styles.donutCenter}>
        <strong>{total.toLocaleString("en-US")}</strong>
        <span>报错执行次数</span>
      </div>
    </div>
  );
}

function FailureReasonPanel({
  data,
  onOpenDetail,
  loading = false,
}: {
  data: CronJobOverviewFailureReason[];
  onOpenDetail: () => void;
  loading?: boolean;
}) {
  return (
    <article className={styles.reasonPanel}>
      <div className={styles.reasonPanelHeader}>
        <h3>报错原因分布（按报错执行次数）</h3>
        <button
          type="button"
          className={styles.linkButton}
          onClick={onOpenDetail}
        >
          查看详情
          <ChevronRight size={14} />
        </button>
      </div>
      {loading ? (
        renderPanelLoading()
      ) : (
        <div className={styles.reasonContent}>
          <DonutChart items={data} />
          <div className={styles.reasonLegend}>
            {data.map((item) => (
              <div key={item.name} className={styles.reasonRow}>
                <span>
                  <i style={{ backgroundColor: item.color }} />
                  {item.name}
                </span>
                <strong>
                  {item.percent.toFixed(2)}% ({item.count})
                </strong>
              </div>
            ))}
          </div>
        </div>
      )}
    </article>
  );
}

function MiniSummaryCard({
  icon,
  title,
  value,
  unit,
  tone = "blue",
}: {
  icon: LucideIcon;
  title: string;
  value: string;
  unit: string;
  tone?: SummaryMetricTone;
}) {
  const Icon = icon;

  return (
    <article className={`${styles.miniSummaryCard} ${styles[tone]}`}>
      <span className={styles.miniIcon}>
        <Icon size={26} />
      </span>
      <div>
        <span>{title}</span>
        <strong>
          {value}
          <em>{unit}</em>
        </strong>
      </div>
    </article>
  );
}

function RankTable({
  data,
  loading = false,
}: {
  data: CronJobOverviewPageData["anomalyRankRows"];
  loading?: boolean;
}) {
  return (
    <section className={`${styles.panel} ${styles.rankPanel}`}>
      <h2>分行异常排行</h2>
      {loading ? (
        renderPanelLoading()
      ) : (
        <div className={styles.tableScroller}>
          <table className={styles.rankTable}>
            <thead>
              <tr>
                <th className={styles.indexCell} />
                <th>分行名称</th>
                <th>报错执行次数</th>
                <th>报错率</th>
                <th>受影响客户经理数</th>
              </tr>
            </thead>
            <tbody>
              {data.map((row) => (
                <tr key={row.rank}>
                  <td className={styles.indexCell}>{row.rank}</td>
                  <td className={styles.branchName}>{row.branchName}</td>
                  <td>{row.alertExecutions}</td>
                  <td>{row.alertRate}</td>
                  <td>{row.affectedManagers}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function FailedTaskModal({
  open,
  onClose,
  tasks,
  loading,
}: {
  open: boolean;
  onClose: () => void;
  tasks: ExecutionItem[];
  loading: boolean;
}) {
  const [keyword, setKeyword] = useState("");
  const [failureReason, setFailureReason] = useState<
    FailureReason | undefined
  >();
  const [currentPage, setCurrentPage] = useState(1);
  const pageSize = 5;
  const normalizedKeyword = keyword.trim().toLowerCase();
  const filteredTasks = tasks.filter((task) => {
    const matchesKeyword = normalizedKeyword
      ? (task.tenant_id || "").toLowerCase().includes(normalizedKeyword)
      : true;
    const matchesFailureReason = failureReason
      ? classifyFailureReason(
          task.error_message,
          task.async_status,
          task.status,
        ) === failureReason
      : true;

    return matchesKeyword && matchesFailureReason;
  });
  const totalCount = filteredTasks.length;
  const paginatedTasks = filteredTasks.slice(
    (currentPage - 1) * pageSize,
    currentPage * pageSize,
  );
  const handlePageChange = (page: number) => {
    setCurrentPage(page);
  };
  const handleFilterChange = () => {
    setCurrentPage(1);
  };
  const handleClose = () => {
    setKeyword("");
    setFailureReason(undefined);
    setCurrentPage(1);
    onClose();
  };

  return (
    <Modal
      open={open}
      className={styles.failedTaskModal}
      title={
        <div className={styles.failedTaskModalTitle}>
          <span className={styles.failedTaskWarningIcon}>
            <WarningOutlined />
          </span>
          <span>执行失败任务清单</span>
        </div>
      }
      width={1080}
      footer={null}
      onCancel={handleClose}
      destroyOnHidden
    >
      <div className={styles.failedTaskToolbar}>
        <Input.Search
          value={keyword}
          onChange={(event) => setKeyword(event.target.value)}
          onSearch={(val) => {
            setKeyword(val);
            handleFilterChange();
          }}
          allowClear
          placeholder="输入用户ID筛选"
          className={styles.failedTaskSearch}
        />
        <Select
          allowClear
          value={failureReason}
          onChange={(value) => {
            setFailureReason(value);
            handleFilterChange();
          }}
          placeholder="失败原因"
          className={styles.failedReasonSelect}
          options={failureReasonOptions.map((reason) => ({
            label: reason,
            value: reason,
          }))}
        />
      </div>
      <Spin spinning={loading} tip="加载失败任务...">
        <div className={styles.failedTaskTable}>
          <div className={styles.failedTaskTableHeader}>
            <span>任务名称</span>
            <span>用户姓名</span>
            <span>用户id</span>
            <span>执行时间</span>
            <span>耗时</span>
            <span>报错信息</span>
          </div>
          <div className={styles.failedTaskTableBody}>
            {paginatedTasks.map((task) => (
              <div key={task.id} className={styles.failedTaskTableRow}>
                <span className={styles.failedTaskName}>{task.job_name}</span>
                <span>{task.tenant_name}</span>
                <span>{task.tenant_id}</span>
                <span>
                  {task.actual_time
                    ? dayjs(task.actual_time).format("YYYY-MM-DD HH:mm:ss")
                    : "-"}
                </span>
                <span>
                  {task.duration_ms === undefined || task.duration_ms === null
                    ? "-"
                    : task.duration_ms < 1000
                    ? `${task.duration_ms}ms`
                    : `${(task.duration_ms / 1000).toFixed(2)}s`}
                </span>
                <Tooltip
                  {...quickTooltipProps}
                  title={
                    task.async_status === "error"
                      ? "子任务执行失败"
                      : task.error_message || "-"
                  }
                  placement="topLeft"
                >
                  <span className={styles.errorMessageCell}>
                    {task.async_status === "error"
                      ? "子任务执行失败"
                      : task.error_message || "-"}
                  </span>
                </Tooltip>
              </div>
            ))}
          </div>
        </div>
        <div className={styles.failedTaskPagination}>
          <Pagination
            current={currentPage}
            pageSize={pageSize}
            total={totalCount}
            onChange={handlePageChange}
            showSizeChanger={false}
            showTotal={(total) => `共 ${total} 条`}
          />
        </div>
      </Spin>
    </Modal>
  );
}

export default function CronJobOverviewPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const currentBbkId = useIframeStore((state) => state.bbk);
  const branchScope = useMemo(
    () => getScopedBranchFilter(currentBbkId),
    [currentBbkId],
  );
  const branchOptions = useMemo(
    () => ensureBranchOptions(branchScope.lockedBbkId),
    [branchScope.lockedBbkId],
  );
  const initialDateRange = getInitialDateRange(searchParams);
  const [overviewData, setOverviewData] =
    useState<CronJobOverviewPageData>(emptyOverviewData);
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [branchExporting, setBranchExporting] = useState(false);
  const branchTableRef = useRef<HTMLTableElement>(null);
  const [timeRange, setTimeRange] = useState<TimeRange>(
    getTimeRangeForDateRange(initialDateRange),
  );
  const [dateRange, setDateRange] = useState<[Dayjs, Dayjs]>(initialDateRange);
  const [bbkIds, setBbkIds] = useState<string[]>(() =>
    branchScope.lockedBbkId
      ? [branchScope.lockedBbkId]
      : getInitialBbkIds(searchParams),
  );
  const [failedTaskModalOpen, setFailedTaskModalOpen] = useState(false);
  const [failedTasks, setFailedTasks] = useState<ExecutionItem[]>([]);
  const [failedTasksLoading, setFailedTasksLoading] = useState(false);

  // Task view state (original ranking table)
  const [taskBranchRankingRows, setTaskBranchRankingRows] = useState<
    CronBranchTaskRankingItem[]
  >([]);
  const [taskBranchRankingLoading, setTaskBranchRankingLoading] =
    useState(false);
  const [selectedTaskBranch, setSelectedTaskBranch] = useState<{
    bbk_id: string;
    bbk_name: string;
  } | null>(null);
  const [selectedTaskSkill, setSelectedTaskSkill] = useState<string | null>(
    null,
  );
  const [selectedTaskManager, setSelectedTaskManager] = useState<string | null>(
    null,
  );
  const [taskSkills, setTaskSkills] = useState<BranchSkillItem[]>([]);
  const [taskSkillsLoading, setTaskSkillsLoading] = useState(false);
  const [taskManagers, setTaskManagers] = useState<BranchSkillManagerItem[]>(
    [],
  );
  const [taskManagersLoading, setTaskManagersLoading] = useState(false);
  const [taskCustomers, setTaskCustomers] = useState<
    BranchSkillManagerCustomerItem[]
  >([]);
  const [taskCustomersLoading, setTaskCustomersLoading] = useState(false);

  // Skill view state (current ranking table with manager drill-down)
  // Inline drill-down state for branch ranking expansion
  const [selectedBranch, setSelectedBranch] = useState<{
    bbk_id: string;
    bbk_name: string;
  } | null>(null);
  const [managerSummary, setManagerSummary] = useState<
    BranchManagerSummaryItem[]
  >([]);
  const [managerSummaryLoading, setManagerSummaryLoading] = useState(false);

  // Manager detail modal state
  const [managerDetailModalOpen, setManagerDetailModalOpen] = useState(false);
  const [selectedManagerForModal, setSelectedManagerForModal] =
    useState<BranchManagerSummaryItem | null>(null);
  const [modalSkills, setModalSkills] = useState<ManagerSkillItem[]>([]);
  const [modalSkillsLoading, setModalSkillsLoading] = useState(false);
  const [modalCustomers, setModalCustomers] = useState<ManagerCustomerItem[]>(
    [],
  );
  const [modalCustomersLoading, setModalCustomersLoading] = useState(false);
  const [selectedModalSkill, setSelectedModalSkill] = useState<string | null>(
    null,
  );

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

  const getOverviewFilters = (): CronJobOverviewDateFilters => ({
    start_date: dateRange[0].format("YYYY-MM-DD"),
    end_date: dateRange[1].format("YYYY-MM-DD"),
    bbk_ids: bbkIds.length > 0 ? bbkIds.join(",") : undefined,
  });

  const getExecutionDateRangeParams = () => ({
    start_time: dateRange[0].startOf("day").format("YYYY-MM-DDTHH:mm:ss"),
    end_time: dateRange[1].endOf("day").format("YYYY-MM-DDTHH:mm:ss"),
  });

  const getDrawerDateParams = () => ({
    start_date: dateRange[0].format("YYYY-MM-DD"),
    end_date: dateRange[1].format("YYYY-MM-DD"),
  });

  // ===== Task view functions =====

  const handleSelectTaskBranch = async (bbkId: string, bbkName: string) => {
    if (selectedTaskBranch?.bbk_id === bbkId) {
      setSelectedTaskBranch(null);
      setTaskSkills([]);
      setTaskManagers([]);
      setTaskCustomers([]);
      setSelectedTaskSkill(null);
      setSelectedTaskManager(null);
      return;
    }
    setSelectedTaskBranch({ bbk_id: bbkId, bbk_name: bbkName });
    setSelectedTaskSkill(null);
    setSelectedTaskManager(null);
    setTaskSkills([]);
    setTaskManagers([]);
    setTaskCustomers([]);

    // Fetch skills for this branch
    setTaskSkillsLoading(true);
    try {
      const dateParams = getDrawerDateParams();
      const response = await monitorApi.getBranchSkills({
        bbk_id: bbkId,
        ...dateParams,
      });
      setTaskSkills(response.items);
    } catch (error) {
      console.warn("Failed to fetch task skills.", error);
    } finally {
      setTaskSkillsLoading(false);
    }
  };

  const handleSelectTaskSkill = async (skillName: string) => {
    if (selectedTaskSkill === skillName) {
      setSelectedTaskSkill(null);
      setTaskManagers([]);
      setTaskCustomers([]);
      setSelectedTaskManager(null);
      return;
    }
    setSelectedTaskSkill(skillName);
    setSelectedTaskManager(null);
    setTaskManagers([]);
    setTaskCustomers([]);

    // Fetch managers for this skill
    setTaskManagersLoading(true);
    try {
      const dateParams = getDrawerDateParams();
      const response = await monitorApi.getBranchSkillManagers({
        bbk_id: selectedTaskBranch!.bbk_id,
        skill_name: skillName,
        ...dateParams,
      });
      setTaskManagers(response.items);
    } catch (error) {
      console.warn("Failed to fetch task managers.", error);
    } finally {
      setTaskManagersLoading(false);
    }
  };

  const handleSelectTaskManager = async (userId: string) => {
    if (selectedTaskManager === userId) {
      setSelectedTaskManager(null);
      setTaskCustomers([]);
      return;
    }
    setSelectedTaskManager(userId);
    setTaskCustomers([]);

    // Fetch customers for this manager
    setTaskCustomersLoading(true);
    try {
      const dateParams = getDrawerDateParams();
      const response = await monitorApi.getBranchSkillManagerCustomers({
        bbk_id: selectedTaskBranch!.bbk_id,
        skill_name: selectedTaskSkill!,
        user_id: userId,
        ...dateParams,
      });
      setTaskCustomers(response.items);
    } catch (error) {
      console.warn("Failed to fetch task customers.", error);
    } finally {
      setTaskCustomersLoading(false);
    }
  };

  // ===== Skill view functions =====

  const handleSelectBranch = async (bbkId: string, bbkName: string) => {
    if (selectedBranch?.bbk_id === bbkId) {
      setSelectedBranch(null);
      setManagerSummary([]);
      return;
    }
    setSelectedBranch({ bbk_id: bbkId, bbk_name: bbkName });
    setManagerSummary([]);

    setManagerSummaryLoading(true);
    try {
      const dateParams = getDrawerDateParams();
      const response = await monitorApi.getBranchManagerSummary({
        bbk_id: bbkId,
        ...dateParams,
      });
      setManagerSummary(response.items);
    } catch (error) {
      console.warn("Failed to fetch branch manager summary.", error);
    } finally {
      setManagerSummaryLoading(false);
    }
  };

  // 打开客户经理详情弹窗
  const handleOpenManagerDetail = async (manager: BranchManagerSummaryItem) => {
    setSelectedManagerForModal(manager);
    setManagerDetailModalOpen(true);
    setModalSkills([]);
    setModalCustomers([]);
    setSelectedModalSkill(null);

    // 获取技能明细
    setModalSkillsLoading(true);
    try {
      const dateParams = getDrawerDateParams();
      const response = await monitorApi.getManagerSkills({
        bbk_id: selectedBranch!.bbk_id,
        user_id: manager.user_id,
        ...dateParams,
      });
      setModalSkills(response.items);
    } catch (error) {
      console.warn("Failed to fetch modal skills.", error);
    } finally {
      setModalSkillsLoading(false);
    }
  };

  // 选择技能，获取该技能下的点击客户明细
  const handleSelectModalSkill = async (skillName: string) => {
    setSelectedModalSkill(skillName);
    setModalCustomers([]);
    setModalCustomersLoading(true);
    try {
      const dateParams = getDrawerDateParams();
      const response = await monitorApi.getManagerCustomers({
        bbk_id: selectedBranch!.bbk_id,
        user_id: selectedManagerForModal!.user_id,
        skill_name: skillName,
        ...dateParams,
      });
      setModalCustomers(response.items);
    } catch (error) {
      console.warn("Failed to fetch modal customers.", error);
    } finally {
      setModalCustomersLoading(false);
    }
  };

  // 关闭客户经理详情弹窗
  const handleCloseManagerDetail = () => {
    setManagerDetailModalOpen(false);
    setSelectedManagerForModal(null);
    setModalSkills([]);
    setModalCustomers([]);
    setSelectedModalSkill(null);
  };

  // Collapse drill-down and refresh all data when filters change
  useEffect(() => {
    // Reset drill-down states
    setSelectedBranch(null);
    setManagerSummary([]);
    setManagerDetailModalOpen(false);
    setSelectedManagerForModal(null);
    setSelectedTaskBranch(null);
    setTaskSkills([]);
    setTaskManagers([]);
    setTaskCustomers([]);
    setSelectedTaskSkill(null);
    setSelectedTaskManager(null);

    // Fetch all main data in parallel
    const fetchAllData = async () => {
      setLoading(true);
      setTaskBranchRankingLoading(true);
      try {
        const [overviewResponse, taskRankingResponse] = await Promise.all([
          monitorApi.getCronJobOverviewPageData(getOverviewFilters()),
          monitorApi.getCronBranchTaskBehavior(getOverviewFilters()),
        ]);
        setOverviewData(overviewResponse);
        setTaskBranchRankingRows(taskRankingResponse.items);
      } catch (error) {
        console.warn("Failed to fetch cron job overview data.", error);
      } finally {
        setLoading(false);
        setTaskBranchRankingLoading(false);
      }
    };

    fetchAllData();
  }, [dateRange, bbkIds]);

  useEffect(() => {
    const nextParams = new URLSearchParams();
    nextParams.set("start_date", dateRange[0].format("YYYY-MM-DD"));
    nextParams.set("end_date", dateRange[1].format("YYYY-MM-DD"));
    if (bbkIds.length > 0) {
      nextParams.set("bbk_ids", bbkIds.join(","));
    }
    setSearchParams(nextParams, { replace: true });
  }, [dateRange, bbkIds, setSearchParams]);

  const handleRefresh = async () => {
    // Reset drill-down states
    setSelectedBranch(null);
    setManagerSummary([]);
    setManagerDetailModalOpen(false);
    setSelectedManagerForModal(null);
    setSelectedTaskBranch(null);
    setTaskSkills([]);
    setTaskManagers([]);
    setTaskCustomers([]);
    setSelectedTaskSkill(null);
    setSelectedTaskManager(null);

    // Fetch all main data in parallel
    setLoading(true);
    setTaskBranchRankingLoading(true);
    try {
      const [overviewResponse, taskRankingResponse] = await Promise.all([
        monitorApi.getCronJobOverviewPageData(getOverviewFilters()),
        monitorApi.getCronBranchTaskBehavior(getOverviewFilters()),
      ]);
      setOverviewData(overviewResponse);
      setTaskBranchRankingRows(taskRankingResponse.items);
    } catch (error) {
      console.warn("Failed to refresh cron job overview page data.", error);
    } finally {
      setLoading(false);
      setTaskBranchRankingLoading(false);
    }
  };

  const handleBranchExport = async () => {
    if (!branchTableRef.current || loading || branchExporting) return;
    const snapshot = branchTableRef.current.cloneNode(true) as HTMLTableElement;
    setBranchExporting(true);
    try {
      const { exportBranchTable } = await import("./exportBranchTable");
      await exportBranchTable(snapshot);
    } catch (error) {
      Modal.error({
        title: "导出失败",
        content:
          error instanceof Error ? error.message : "导出失败，请稍后重试",
      });
    } finally {
      setBranchExporting(false);
    }
  };

  const handleExport = async () => {
    setExporting(true);
    try {
      const blob = await monitorApi.exportSkillUsageDetails({
        start_date: dateRange[0].format("YYYY-MM-DD"),
        end_date: dateRange[1].format("YYYY-MM-DD"),
        bbk_ids: bbkIds.length > 0 ? bbkIds.join(",") : undefined,
      });
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `定时任务客户经理技能明细_${dayjs().format(
        "YYYYMMDD_HHmmss",
      )}.xlsx`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      window.URL.revokeObjectURL(url);
    } catch (error) {
      const errorMessage =
        error instanceof Error ? error.message : "导出失败，请稍后重试";
      Modal.error({ title: "导出失败", content: errorMessage });
    } finally {
      setExporting(false);
    }
  };

  const fetchFailedTasks = async () => {
    setFailedTasksLoading(true);
    setFailedTasks([]);
    try {
      const pageSize = 100;
      const activeBbkIds = bbkIds.filter(Boolean);
      const selectedBbkIds =
        activeBbkIds.length > 0 ? activeBbkIds : [undefined];
      const selectedBbkIdSet = new Set(activeBbkIds);
      const allTasks: ExecutionItem[] = [];
      console.info("[cron failed tasks debug] start fetch", {
        dateRange: getExecutionDateRangeParams(),
        activeBbkIds,
        selectedBbkIds,
      });

      for (const bbkId of selectedBbkIds) {
        let page = 1;
        let total = 0;

        do {
          const response = await monitorApi.getExecutions(page, pageSize, {
            ...getExecutionDateRangeParams(),
            status: "failed",
            bbk_id: bbkId,
          });
          console.info("[cron failed tasks debug] response page", {
            requestedBbkId: bbkId,
            page,
            total: response.total,
            itemCount: response.items.length,
            sample: response.items.slice(0, 5).map((task) => ({
              id: task.id,
              jobId: task.job_id,
              tenantId: task.tenant_id,
              bbkId: task.bbk_id,
              status: task.status,
            })),
          });
          if (response.items.length === 0) {
            break;
          }
          allTasks.push(...response.items);
          total = response.total;
          page += 1;
        } while ((page - 1) * pageSize < total);
      }

      const tasksById = new Map<number, ExecutionItem>();
      allTasks
        .filter((task) =>
          selectedBbkIdSet.size === 0
            ? true
            : selectedBbkIdSet.has(task.bbk_id || ""),
        )
        .forEach((task) => {
          tasksById.set(task.id, task);
        });
      console.info("[cron failed tasks debug] final tasks", {
        activeBbkIds,
        rawCount: allTasks.length,
        filteredCount: tasksById.size,
        filteredSample: Array.from(tasksById.values())
          .slice(0, 5)
          .map((task) => ({
            id: task.id,
            jobId: task.job_id,
            tenantId: task.tenant_id,
            bbkId: task.bbk_id,
            status: task.status,
          })),
      });
      setFailedTasks(
        Array.from(tasksById.values()).sort((a, b) => {
          const left = a.actual_time ? dayjs(a.actual_time).valueOf() : 0;
          const right = b.actual_time ? dayjs(b.actual_time).valueOf() : 0;
          return right - left;
        }),
      );
    } catch (error) {
      console.warn("Failed to fetch failed cron executions.", error);
    } finally {
      setFailedTasksLoading(false);
    }
  };

  useEffect(() => {
    if (failedTaskModalOpen) {
      fetchFailedTasks();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [failedTaskModalOpen, dateRange, bbkIds]);

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
    dates: null | [Dayjs | null, Dayjs | null],
  ) => {
    if (!dates?.[0] || !dates?.[1]) {
      return;
    }

    const [start, end] = dates;
    const today = dayjs();

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

  const disabledDate = (current: Dayjs | null): boolean =>
    !!current && current.isAfter(dayjs().startOf("day"), "day");

  const summaryMetricValues = new Map(
    overviewData.summaryMetrics.map((metric) => [metric.key, metric]),
  );
  const summaryMetrics = summaryMetricDefinitions.map((definition) => {
    const metricValue = summaryMetricValues.get(definition.key);
    const footerValue =
      definition.key === "branches"
        ? summaryMetricValues.get("managers")?.value
        : metricValue?.footerValue;
    return {
      ...definition,
      value: metricValue?.value ?? "-",
      hintValue: metricValue?.hintValue,
      footerValue,
    };
  });

  // Build report metric view
  const reportMetricValue = summaryMetricValues.get(reportMetricDefinition.key);
  const reportMetric: ReportMetricView = {
    ...reportMetricDefinition,
    value: reportMetricValue?.value ?? "-",
    hintValue: reportMetricValue?.hintValue,
    subValues: {
      report_count: summaryMetricValues.get("report_count")?.value ?? "-",
      insight_count: summaryMetricValues.get("insight_count")?.value ?? "-",
      phone_count: summaryMetricValues.get("phone_count")?.value ?? "-",
    },
  };

  return (
    <main className={styles.cronOverviewPage}>
      {loading ? <div className={styles.loadingBar}>加载中...</div> : null}
      <header className={styles.header}>
        <div className={styles.titleRow}>
          <button
            type="button"
            className={styles.backButton}
            onClick={() => navigate("/analytics/business-overview")}
          >
            <ArrowLeft size={20} />
          </button>
          <h1>定时任务详情</h1>
        </div>
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
              onClick={handleRefresh}
            >
              <RefreshCw size={14} />
              刷新
            </button>
          </div>
        </div>
      </header>

      <section className={styles.summaryGrid} aria-label="概览指标">
        {loading ? (
          Array.from({ length: summaryMetricDefinitions.length + 1 }).map(
            (_, index) => (
              <article key={index} className={styles.summaryCard}>
                {renderPanelLoading()}
              </article>
            ),
          )
        ) : (
          <>
            {summaryMetrics.map((metric) => (
              <SummaryCard key={metric.key} metric={metric} />
            ))}
            <ReportSummaryCard metric={reportMetric} />
          </>
        )}
      </section>

      <p className={styles.formulaNote}>
        说明： 执行成功率 = 成功执行次数 / 任务执行次数； 任务已读率 =
        已读执行次数 / 任务执行次数； 查看方案任务率 = 查看方案次数 /
        任务执行次数
      </p>

      {/* 分行维度报表 */}
      <h2
        className={`${styles.sectionHeading} ${styles.sectionHeadingSpacious}`}
      >
        分行维度
        <span className={styles.sectionHeadingHint}>（点击分行查看明细）</span>
        <button
          type="button"
          className={styles.exportButton}
          onClick={handleBranchExport}
          disabled={
            loading || branchExporting || !overviewData.branchRankingRows.length
          }
          aria-label="分行维度导出 Excel"
          aria-busy={branchExporting}
        >
          <Download size={14} aria-hidden="true" />
          {branchExporting ? "导出中..." : "导出 Excel"}
        </button>
      </h2>
      <RankingTable
        tableRef={branchTableRef}
        data={overviewData.branchRankingRows}
        loading={loading}
        onRowClick={handleSelectBranch}
        selectedBranchId={selectedBranch?.bbk_id ?? null}
      />

      {/* 技能视角下钻 */}
      {selectedBranch && (
        <div className={styles.drillDownContainer}>
          <div className={styles.drillDownFullWidth}>
            <h3 className={styles.drillDownTitle}>
              当前分行下的客户经理明细
              <span className={styles.drillDownSubTitle}>
                （{selectedBranch.bbk_name}）
              </span>
            </h3>
            <Table
              className={styles.drillDownTable}
              dataSource={managerSummary}
              rowKey="user_id"
              loading={managerSummaryLoading}
              size="small"
              pagination={false}
              sticky
              scroll={DRILL_DOWN_TABLE_SCROLL}
              rowClassName={styles.drillHoverRow}
              columns={[
                {
                  title: "客户经理名称",
                  dataIndex: "user_name",
                  key: "user_name",
                  width: 100,
                  align: "center",
                  render: (v: string, record: BranchManagerSummaryItem) => (
                    <span
                      className={styles.clickableLink}
                      onClick={() => handleOpenManagerDetail(record)}
                    >
                      {v || record.user_id}
                    </span>
                  ),
                },
                {
                  title: "技能数量",
                  dataIndex: "skill_count",
                  key: "skill_count",
                  width: 70,
                  align: "center",
                  sorter: branchManagerMetricSorter("skill_count"),
                },
                {
                  title: "任务总数",
                  dataIndex: "total_tasks",
                  key: "total_tasks",
                  width: 70,
                  align: "center",
                  sorter: branchManagerMetricSorter("total_tasks"),
                },
                {
                  title: "成功执行数",
                  dataIndex: "success_count",
                  key: "success_count",
                  width: 70,
                  align: "center",
                  sorter: branchManagerMetricSorter("success_count"),
                },
                {
                  title: "已读任务数",
                  dataIndex: "read_tasks",
                  key: "read_tasks",
                  width: 70,
                  align: "center",
                  sorter: branchManagerMetricSorter("read_tasks"),
                },
                {
                  title: "推荐客户数",
                  dataIndex: "recommended_customers",
                  key: "recommended_customers",
                  width: 80,
                  align: "center",
                  sorter: branchManagerMetricSorter("recommended_customers"),
                },
                {
                  title: "查看方案客户数",
                  dataIndex: "viewed_customers",
                  key: "viewed_customers",
                  width: 90,
                  align: "center",
                  sorter: branchManagerMetricSorter("viewed_customers"),
                },
                {
                  title: "去洞察客户数",
                  dataIndex: "insight_customers",
                  key: "insight_customers",
                  width: 80,
                  align: "center",
                  sorter: branchManagerMetricSorter("insight_customers"),
                },
                {
                  title: "去电访客户数",
                  dataIndex: "phone_customers",
                  key: "phone_customers",
                  width: 80,
                  align: "center",
                  sorter: branchManagerMetricSorter("phone_customers"),
                },
                {
                  title: "接触客户数",
                  dataIndex: "contacted_customers",
                  key: "contacted_customers",
                  width: 80,
                  align: "center",
                  sorter: branchManagerMetricSorter("contacted_customers"),
                },
                {
                  title: "接触客户率",
                  dataIndex: "contact_rate",
                  key: "contact_rate",
                  width: 90,
                  align: "center",
                  sorter: branchManagerMetricSorter("contact_rate"),
                  render: (v: number | null | undefined) =>
                    formatRatioPercent(v),
                },
              ]}
            />
          </div>
        </div>
      )}

      {/* 任务视角分行排行 */}
      <h2 className={styles.sectionHeading}>
        分行综合排行
        <span className={styles.sectionHeadingHint}>（点击分行查看明细）</span>
        <button
          type="button"
          className={styles.exportButton}
          onClick={handleExport}
          disabled={exporting}
          aria-busy={exporting}
        >
          <Download size={14} aria-hidden="true" />
          {exporting ? "导出中..." : "导出 Excel"}
        </button>
      </h2>
      <TaskRankingTable
        data={taskBranchRankingRows}
        loading={taskBranchRankingLoading}
        onRowClick={handleSelectTaskBranch}
        selectedBranchId={selectedTaskBranch?.bbk_id ?? null}
      />

      {selectedTaskBranch && (
        <div className={styles.drillDownContainer}>
          {/* 技能列 */}
          <div className={styles.drillDownColumn}>
            <h3 className={styles.drillDownTitle}>
              当前分行下的技能明细
              <span className={styles.drillDownSubTitle}>
                （{selectedTaskBranch.bbk_name}）
              </span>
            </h3>
            <Table
              className={`${styles.drillDownTable} ${styles.branchSkillDrillDownTable}`}
              dataSource={taskSkills}
              rowKey="skill_name"
              loading={taskSkillsLoading}
              size="small"
              pagination={false}
              sticky
              scroll={DRILL_DOWN_TABLE_SCROLL}
              onRow={(record) => ({
                onClick: () => handleSelectTaskSkill(record.skill_name),
                style: {
                  cursor: "pointer",
                  background:
                    record.skill_name === selectedTaskSkill
                      ? "#e6f4ff"
                      : undefined,
                },
              })}
              columns={[
                {
                  title: "技能名称",
                  dataIndex: "skill_name",
                  key: "skill_name",
                  width: 130,
                  align: "center",
                },
                {
                  title: "定时任务数",
                  dataIndex: "cron_task_count",
                  key: "cron_task_count",
                  width: 60,
                  align: "center",
                },
                {
                  title: "成功执行数",
                  dataIndex: "success_count",
                  key: "success_count",
                  width: 60,
                  align: "center",
                },
                {
                  title: "成功率",
                  dataIndex: "success_rate",
                  key: "success_rate",
                  width: 48,
                  align: "center",
                  render: (v: number) => (v != null ? `${v.toFixed(1)}%` : "-"),
                },
                {
                  title: "已读任务数",
                  dataIndex: "read_count",
                  key: "read_count",
                  width: 60,
                  align: "center",
                },
                {
                  title: "报错次数",
                  dataIndex: "error_count",
                  key: "error_count",
                  width: 55,
                  align: "center",
                },
              ]}
            />
          </div>

          {/* 客户经理列 */}
          <div className={styles.drillDownColumn}>
            <h3 className={styles.drillDownTitle}>
              该技能下的客户经理明细
              {selectedTaskSkill && (
                <span className={styles.drillDownSubTitle}>
                  （{selectedTaskSkill}）
                </span>
              )}
            </h3>
            <Table
              className={styles.drillDownTable}
              dataSource={taskManagers}
              rowKey="user_id"
              loading={taskManagersLoading}
              size="small"
              pagination={false}
              sticky
              scroll={DRILL_DOWN_TABLE_SCROLL}
              onRow={(record) => ({
                onClick: () => handleSelectTaskManager(record.user_id),
                style: {
                  cursor: "pointer",
                  background:
                    record.user_id === selectedTaskManager
                      ? "#e6f4ff"
                      : undefined,
                },
              })}
              columns={[
                {
                  title: "客户经理",
                  dataIndex: "user_name",
                  key: "user_name",
                  width: 80,
                  align: "center",
                },
                {
                  title: "已读次数",
                  dataIndex: "read_count",
                  key: "read_count",
                  width: 50,
                  align: "center",
                },
                {
                  title: "方案次数",
                  dataIndex: "plan_count",
                  key: "plan_count",
                  width: 50,
                  align: "center",
                },
                {
                  title: "洞察次数",
                  dataIndex: "insight_count",
                  key: "insight_count",
                  width: 50,
                  align: "center",
                },
                {
                  title: "电访次数",
                  dataIndex: "phone_count",
                  key: "phone_count",
                  width: 50,
                  align: "center",
                },
                {
                  title: "最后点击时间",
                  dataIndex: "last_click_time",
                  key: "last_click_time",
                  width: 100,
                  align: "center",
                  render: (v: string) =>
                    v ? dayjs(v).format("YYYY-MM-DD HH:mm") : "-",
                },
              ]}
            />
          </div>

          {/* 客户列 */}
          <div className={styles.drillDownColumn}>
            <h3 className={styles.drillDownTitle}>
              该客户经理下的客户明细
              {selectedTaskManager && taskManagers.length > 0 && (
                <span className={styles.drillDownSubTitle}>
                  （
                  {taskManagers.find((m) => m.user_id === selectedTaskManager)
                    ?.user_name || selectedTaskManager}
                  ）
                </span>
              )}
            </h3>
            <Table
              className={styles.drillDownTable}
              dataSource={taskCustomers}
              rowKey="customer_id"
              loading={taskCustomersLoading}
              size="small"
              pagination={false}
              sticky
              scroll={DRILL_DOWN_TABLE_SCROLL}
              columns={[
                {
                  title: "客户名称",
                  dataIndex: "customer_name",
                  key: "customer_name",
                  width: 90,
                  align: "center",
                },
                {
                  title: "客户ID",
                  dataIndex: "customer_id",
                  key: "customer_id",
                  width: 80,
                  align: "center",
                },
                {
                  title: "点击方案",
                  dataIndex: "clicked_plan",
                  key: "clicked_plan",
                  width: 55,
                  align: "center",
                  render: (v: boolean) => (v ? "是" : "否"),
                },
                {
                  title: "点击洞察",
                  dataIndex: "clicked_insight",
                  key: "clicked_insight",
                  width: 55,
                  align: "center",
                  render: (v: boolean) => (v ? "是" : "否"),
                },
                {
                  title: "点击电访",
                  dataIndex: "clicked_phone",
                  key: "clicked_phone",
                  width: 55,
                  align: "center",
                  render: (v: boolean) => (v ? "是" : "否"),
                },
              ]}
            />
          </div>
        </div>
      )}

      <section className={styles.anomalySection}>
        <div className={styles.anomalyLeft}>
          <h2>分行层异常诊断</h2>
          <div className={styles.miniSummaryGrid}>
            {loading ? (
              Array.from({ length: 2 }).map((_, index) => (
                <article key={index} className={styles.miniSummaryCard}>
                  {renderPanelLoading()}
                </article>
              ))
            ) : (
              <>
                <MiniSummaryCard
                  icon={Banknote}
                  title="受影响分行数"
                  value={overviewData.anomalySummary.affectedBranches}
                  unit={overviewData.anomalySummary.affectedBranchesUnit}
                />
                <MiniSummaryCard
                  icon={UserRoundCheck}
                  title="受影响客户经理数"
                  value={overviewData.anomalySummary.affectedManagers}
                  unit={overviewData.anomalySummary.affectedManagersUnit}
                  tone="orange"
                />
              </>
            )}
          </div>
          <FailureReasonPanel
            data={overviewData.failureReasons}
            loading={loading}
            onOpenDetail={() => setFailedTaskModalOpen(true)}
          />
        </div>
        <RankTable data={overviewData.anomalyRankRows} loading={loading} />
      </section>
      <FailedTaskModal
        open={failedTaskModalOpen}
        onClose={() => setFailedTaskModalOpen(false)}
        tasks={failedTasks}
        loading={failedTasksLoading}
      />

      {/* 客户经理详情弹窗 */}
      <Modal
        open={managerDetailModalOpen}
        onCancel={handleCloseManagerDetail}
        footer={null}
        width={900}
        title={
          <span>
            客户经理详情
            {selectedManagerForModal && (
              <span
                style={{
                  marginLeft: 8,
                  fontWeight: "normal",
                  color: "#64748b",
                }}
              >
                （
                {selectedManagerForModal.user_name ||
                  selectedManagerForModal.user_id}
                ）
              </span>
            )}
          </span>
        }
      >
        {selectedManagerForModal && (
          <div className={styles.drillDownContainer}>
            {/* 技能明细列 */}
            <div className={styles.drillDownColumn}>
              <h3 className={styles.drillDownTitle}>
                技能明细
                <span className={styles.drillDownHint}>（点击查看客户）</span>
              </h3>
              <Table
                className={styles.drillDownTable}
                dataSource={modalSkills}
                rowKey="skill_name"
                loading={modalSkillsLoading}
                size="small"
                pagination={false}
                sticky
                scroll={DRILL_DOWN_TABLE_SCROLL}
                onRow={(record) => ({
                  onClick: () => handleSelectModalSkill(record.skill_name),
                  style: {
                    cursor: "pointer",
                    background:
                      record.skill_name === selectedModalSkill
                        ? "#e6f4ff"
                        : undefined,
                  },
                })}
                columns={[
                  {
                    title: "技能名称",
                    dataIndex: "skill_name",
                    key: "skill_name",
                    width: 130,
                    align: "center",
                  },
                  {
                    title: "定时任务数",
                    dataIndex: "cron_task_count",
                    key: "cron_task_count",
                    width: 70,
                    align: "center",
                  },
                  {
                    title: "成功执行数",
                    dataIndex: "success_count",
                    key: "success_count",
                    width: 70,
                    align: "center",
                  },
                  {
                    title: "成功率",
                    dataIndex: "success_rate",
                    key: "success_rate",
                    width: 60,
                    align: "center",
                    render: (v: number) =>
                      v != null ? `${v.toFixed(1)}%` : "-",
                  },
                  {
                    title: "已读任务数",
                    dataIndex: "read_count",
                    key: "read_count",
                    width: 70,
                    align: "center",
                  },
                  {
                    title: "报错次数",
                    dataIndex: "error_count",
                    key: "error_count",
                    width: 60,
                    align: "center",
                  },
                ]}
              />
            </div>

            {/* 点击客户明细列 */}
            <div className={styles.drillDownColumn}>
              <h3 className={styles.drillDownTitle}>
                点击客户明细
                {selectedModalSkill && (
                  <span className={styles.drillDownSubTitle}>
                    （{selectedModalSkill}）
                  </span>
                )}
              </h3>
              <Table
                className={styles.drillDownTable}
                dataSource={modalCustomers}
                rowKey="customer_id"
                loading={modalCustomersLoading}
                size="small"
                pagination={{ pageSize: 5 }}
                sticky
                scroll={DRILL_DOWN_TABLE_SCROLL}
                columns={[
                  {
                    title: "客户名称",
                    dataIndex: "customer_name",
                    key: "customer_name",
                    width: 90,
                    align: "center",
                  },
                  {
                    title: "客户ID",
                    dataIndex: "customer_id",
                    key: "customer_id",
                    width: 80,
                    align: "center",
                  },
                  {
                    title: "点击方案",
                    dataIndex: "clicked_plan",
                    key: "clicked_plan",
                    width: 55,
                    align: "center",
                    render: (v: boolean) => (v ? "是" : "否"),
                  },
                  {
                    title: "点击洞察",
                    dataIndex: "clicked_insight",
                    key: "clicked_insight",
                    width: 55,
                    align: "center",
                    render: (v: boolean) => (v ? "是" : "否"),
                  },
                  {
                    title: "点击电访",
                    dataIndex: "clicked_phone",
                    key: "clicked_phone",
                    width: 55,
                    align: "center",
                    render: (v: boolean) => (v ? "是" : "否"),
                  },
                  {
                    title: "点击时间",
                    dataIndex: "click_time",
                    key: "click_time",
                    width: 100,
                    align: "center",
                    render: (v: string) =>
                      v ? dayjs(v).format("YYYY-MM-DD HH:mm") : "-",
                  },
                ]}
              />
            </div>
          </div>
        )}
      </Modal>
    </main>
  );
}
