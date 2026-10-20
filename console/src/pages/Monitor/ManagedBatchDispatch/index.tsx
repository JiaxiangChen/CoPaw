import { useCallback, useState } from "react";
import {
  Alert,
  Button,
  DatePicker,
  Empty,
  Input,
  Pagination,
  Progress,
  Segmented,
  Select,
  Space,
  Spin,
  Tag,
} from "antd";
import dayjs, { type Dayjs } from "dayjs";
import { RefreshCw } from "lucide-react";
import {
  managedBatchBoard,
  type ManagedRetryResult,
} from "../../../api/modules/managedBatchBoard";
import { useIframeStore } from "../../../stores/iframeStore";
import { DEFAULT_SOURCE_ID } from "../../../constants/identity";
import { getUserId } from "../../../utils/identity";
import { loadWorkerHistory } from "../CronBatchDispatch/workerHistory";
import styles from "../CronBatchDispatch/index.module.less";
import { statusTag, timeText } from "./format";
import { useBoardResource } from "./useBoardResource";
import BatchDetail from "./BatchDetail";
import SharedWorkers from "./SharedWorkers";

function buildRange(key: string): [Dayjs, Dayjs] {
  const now = dayjs();
  if (key === "today") return [now.startOf("day"), now.endOf("day")];
  return [
    key === "last24h"
      ? now.subtract(24, "hour")
      : now.subtract(6, "day").startOf("day"),
    now,
  ];
}
function Board({ source, actor }: { source: string; actor: string }) {
  const [shortcut, setShortcut] = useState("today");
  const [range, setRange] = useState<[Dayjs, Dayjs]>(() => buildRange("today"));
  const [status, setStatus] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [retryResult, setRetryResult] = useState<ManagedRetryResult | null>(
    null,
  );
  const reload = useCallback(() => setRefresh((value) => value + 1), []);
  const onQueued = useCallback((result: ManagedRetryResult) => {
    setRetryResult(result);
    setRefresh((value) => value + 1);
  }, []);
  const start = range[0].toISOString();
  const end = range[1].toISOString();
  const loadBatches = useCallback(
    (signal: AbortSignal) => {
      void refresh;
      return managedBatchBoard.batches(
        source,
        { start_time: start, end_time: end, status, query, page, page_size: 4 },
        signal,
      );
    },
    [source, start, end, status, query, page, refresh],
  );
  const batches = useBoardResource(loadBatches);
  const loadWorkers = useCallback(
    (signal: AbortSignal) => {
      void refresh;
      return loadWorkerHistory(
        (cursor) =>
          managedBatchBoard.workers(
            source,
            { start_time: start, end_time: end, capacity_cursor: cursor },
            signal,
          ),
        () => !signal.aborted,
      );
    },
    [source, start, end, refresh],
  );
  const workers = useBoardResource(loadWorkers);
  const items = batches.value?.items || [];
  const batch = items.find((item) => item.batch_id === selected) || items[0];
  const stats = batches.value?.stats;
  const metrics = [
    ["Batch 总数", stats?.total_batches, "当前全局筛选范围"],
    ["任务总数", stats?.total_members, "含父任务，重试不增加任务数"],
    ["运行中 Batch", stats?.running_batches, "含等待子任务完成"],
    [
      "任务完成率",
      stats?.total_members
        ? `${((stats.done_members / stats.total_members) * 100).toFixed(1)}%`
        : "-",
      "明确终态 / 任务总数",
    ],
    ["失败任务", stats?.failed_members, "当前业务尝试"],
    [
      "有效 Worker",
      workers.value?.current_capacity.reduce(
        (sum, item) => sum + item.effective_workers,
        0,
      ),
      "新旧共享模型池",
    ],
  ];
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div>
          <h1>批调度看板（新）</h1>
          <p>新链路执行批次、逻辑成员、执行事件与共享模型池。</p>
        </div>
        <Space>
          <Tag color="blue" className={styles.sourceTag}>
            当前渠道 {source}
          </Tag>
          <Button icon={<RefreshCw size={16} />} onClick={reload}>
            刷新
          </Button>
        </Space>
      </header>
      <section className={styles.toolbar}>
        <Segmented
          value={shortcut}
          options={[
            { value: "today", label: "今天" },
            { value: "last24h", label: "近24h" },
            { value: "last7", label: "近7天" },
          ]}
          onChange={(value) => {
            setShortcut(String(value));
            setRange(buildRange(String(value)));
            setPage(1);
          }}
        />
        <DatePicker.RangePicker
          showTime
          allowClear={false}
          value={range}
          className={styles.rangePicker}
          onChange={(value) => {
            if (value?.[0] && value[1]) {
              setRange([value[0], value[1]]);
              setShortcut("custom");
              setPage(1);
            }
          }}
        />
        <Select
          aria-label="新版批次状态"
          value={status}
          className={styles.statusSelect}
          options={[
            { value: "", label: "全部状态" },
            { value: "received", label: "已接收" },
            { value: "pending", label: "等待处理" },
            { value: "running", label: "运行中" },
            { value: "completed", label: "已完成" },
            { value: "failed", label: "失败" },
          ]}
          onChange={(value) => {
            setStatus(value);
            setPage(1);
          }}
        />
      </section>
      <section className={styles.summaryStrip} aria-label="新版批调度概览">
        {metrics.map(([title, value, hint]) => (
          <div className={styles.summaryMetric} key={title}>
            <span>{title}</span>
            <strong>{value ?? "-"}</strong>
            <em>{hint}</em>
          </div>
        ))}
      </section>
      {batches.error && <Alert type="error" showIcon message={batches.error} />}
      {retryResult && (
        <Alert
          type="success"
          showIcon
          closable
          onClose={() => setRetryResult(null)}
          message={`人工重试已受理 ${retryResult.queued} 个，跳过 ${
            retryResult.skipped
          } 个${retryResult.reused ? "（复用原请求）" : ""}`}
          description={
            <details>
              <summary>逐项受理结果</summary>
              <ul>
                {retryResult.results.map((item) => (
                  <li key={item.member_id}>
                    {item.member_id}：
                    {item.status === "queued"
                      ? item.transport_only
                        ? "运输重投"
                        : "新增业务尝试"
                      : item.reason}
                  </li>
                ))}
              </ul>
            </details>
          }
        />
      )}
      <section className={styles.workspace}>
        <article className={styles.batchPane}>
          <div className={styles.paneHeader}>
            <div>
              <h2>所有 Batch</h2>
              <span>按逻辑计划时间倒序展示</span>
            </div>
            <strong>共 {batches.value?.total ?? "-"} 个全局结果</strong>
          </div>
          <Input
            allowClear
            aria-label="全局筛选新版 Batch"
            placeholder="全局筛选 Batch、任务、租户、Agent 或模型"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              setPage(1);
            }}
          />
          <div className={styles.batchListHeader}>
            <span>任务 / Batch</span>
            <span>计划 / 回调</span>
            <span>状态 / 进度</span>
          </div>
          <Spin
            spinning={batches.loading}
            wrapperClassName={styles.batchListSpin}
          >
            <div className={styles.batchList}>
              {items.map((item) => (
                <button
                  type="button"
                  key={item.batch_id}
                  onClick={() => setSelected(item.batch_id)}
                  className={`${styles.batchRow} ${
                    batch?.batch_id === item.batch_id
                      ? styles.batchRowSelected
                      : ""
                  }`}
                >
                  <span className={styles.batchIdentity}>
                    <strong>{item.job_name || "未命名任务"}</strong>
                    <em title={item.batch_id}>{item.batch_id}</em>
                    <small>{item.job_id}</small>
                  </span>
                  <span className={styles.batchTiming}>
                    <strong>{timeText(item.logical_fire_at)}</strong>
                    <em>回调 {timeText(item.created_at)}</em>
                    <small title="父任务本次执行的冻结模型；各成员模型见成员列表">
                      {item.provider_id || "-"} / {item.model_id || "-"}
                    </small>
                  </span>
                  <span className={styles.batchProgress}>
                    {statusTag(item.status)}
                    {item.dispatch_paused &&
                      item.done_count < item.member_count && (
                        <Tag color="warning">批调度入口已暂停</Tag>
                      )}
                    {(item.skipped_count ?? 0) > 0 && (
                      <strong>跳过／取消 {item.skipped_count} 个</strong>
                    )}
                    {item.status === "failed" ? (
                      <strong className={styles.batchOutcome}>
                        <span>成功 {item.success_count ?? "-"} 个</span>
                        <span>失败 {item.failed_count} 个</span>
                      </strong>
                    ) : item.completion_rate === null ? (
                      <small>无可执行成员</small>
                    ) : (
                      <>
                        <strong>
                          {item.done_count}/{item.member_count}
                        </strong>
                        <Progress
                          percent={Math.round(item.completion_rate * 100)}
                          size="small"
                          showInfo={false}
                          status={item.failed_count ? "exception" : "normal"}
                        />
                      </>
                    )}
                  </span>
                </button>
              ))}
              {!batches.loading && !items.length && (
                <Empty description="当前范围内暂无 Batch" />
              )}
            </div>
          </Spin>
          <nav className={styles.batchPagination} aria-label="新版 Batch 分页">
            <Pagination
              current={page}
              pageSize={4}
              total={batches.value?.total || 0}
              showSizeChanger={false}
              size="small"
              onChange={setPage}
            />
          </nav>
        </article>
        <article className={styles.detailPane}>
          {batch ? (
            <BatchDetail
              key={batch.batch_id}
              source={source}
              actor={actor}
              batch={batch}
              refresh={refresh}
              onQueued={onQueued}
            />
          ) : (
            <div className={styles.detailEmpty}>
              <Empty description="请选择一个 Batch" />
            </div>
          )}
        </article>
      </section>
      <SharedWorkers
        value={workers.value}
        loading={workers.loading}
        error={workers.error}
      />
    </div>
  );
}

export default function ManagedBatchDispatchPage() {
  const source = useIframeStore((state) => state.source) || DEFAULT_SOURCE_ID;
  const user = useIframeStore((state) => state.userId);
  const admin = useIframeStore((state) => state.isSuperManager);
  const manager = useIframeStore((state) => state.manager);
  if (!admin && !manager)
    return (
      <div className={styles.page}>
        <Alert
          type="warning"
          showIcon
          message="仅管理员可访问批调度看板（新）"
        />
      </div>
    );
  const actor = user || getUserId();
  return (
    <Board
      key={JSON.stringify([source, actor, admin, manager])}
      source={source}
      actor={actor}
    />
  );
}
