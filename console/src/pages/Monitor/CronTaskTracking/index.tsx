import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  DatePicker,
  Empty,
  Input,
  Radio,
  Select,
  Space,
  Spin,
  Tag,
} from "antd";
import dayjs from "dayjs";
import { cronTaskTracking } from "../../../api/modules/cronTaskTracking";
import type {
  TaskIdentity,
  TrackingCandidate,
  TrackingCategory,
  TrackingEntry,
  TrackingFilters,
  TrackingMetadata,
} from "../../../api/types/cronTaskTracking";
import { useIframeStore } from "../../../stores/iframeStore";
import { DEFAULT_SOURCE_ID } from "../../../constants/identity";
import TrackingOverview, { Coverage } from "./TrackingOverview";
import TrackingTimeline from "./TrackingTimeline";
import { categories, entries } from "./format";
import styles from "./index.module.less";

function today() {
  return dayjs().utcOffset(480).startOf("day");
}
function dateFilters(start = today(), end = start): TrackingFilters {
  return {
    start_time: `${start.format("YYYY-MM-DD")}T00:00:00+08:00`,
    end_time: `${end.add(1, "day").format("YYYY-MM-DD")}T00:00:00+08:00`,
    category: "all",
    entry: "all",
    order: "asc",
  };
}

function TaskResults({
  source,
  identity,
}: {
  source: string;
  identity: TaskIdentity;
}) {
  const [revision, setRevision] = useState(0);
  const [filters, setFilters] = useState<TrackingFilters>(dateFilters);
  const [range, setRange] = useState<[dayjs.Dayjs, dayjs.Dayjs]>([
    today(),
    today(),
  ]);
  const [rangeError, setRangeError] = useState("");
  const reset = () => {
    setRange([today(), today()]);
    setFilters(dateFilters());
    setRangeError("");
  };
  return (
    <>
      <div className={styles.identity}>
        <code>{identity.job_id}</code>
        <Space>
          <Tag>只读追踪</Tag>
          <Button aria-label="刷新" onClick={() => setRevision(revision + 1)}>
            刷新
          </Button>
        </Space>
      </div>
      <TrackingOverview
        source={source}
        identity={identity}
        revision={revision}
      />
      <section aria-label="时间轴筛选" className={styles.filters}>
        <div className={styles.filterRow}>
          <div className={`${styles.field} ${styles.rangeField}`}>
            <span>时间范围（北京时间，最多 31 天）</span>
            <label className={styles.srOnly} htmlFor="tracking-start-date">
              开始日期（北京时间）
            </label>
            <label className={styles.srOnly} htmlFor="tracking-end-date">
              结束日期（北京时间）
            </label>
            <DatePicker.RangePicker
              id={{ start: "tracking-start-date", end: "tracking-end-date" }}
              value={range}
              allowClear={false}
              onChange={(values) => {
                if (!values?.[0] || !values[1]) return;
                if (
                  values[1]
                    .startOf("day")
                    .diff(values[0].startOf("day"), "day") >= 31
                ) {
                  setRangeError("一次最多查询 31 天，请缩小时间范围");
                  return;
                }
                setRangeError("");
                setRange([values[0], values[1]]);
                setFilters({
                  ...filters,
                  ...dateFilters(values[0], values[1]),
                  category: filters.category,
                  entry: filters.entry,
                  order: filters.order,
                });
              }}
            />
          </div>
          <label className={styles.field}>
            <span>业务入口</span>
            <Select
              aria-label="业务入口"
              value={filters.entry}
              options={Object.entries(entries).map(([value, label]) => ({
                value,
                label,
              }))}
              onChange={(entry: TrackingEntry) =>
                setFilters({ ...filters, entry })
              }
            />
          </label>
          <label className={styles.field}>
            <span>时间顺序</span>
            <Select
              aria-label="时间顺序"
              value={filters.order}
              options={[
                { value: "asc", label: "从早到晚" },
                { value: "desc", label: "从晚到早" },
              ]}
              onChange={(order: "asc" | "desc") =>
                setFilters({ ...filters, order })
              }
            />
          </label>
          <Button onClick={reset}>重置筛选</Button>
        </div>
        {rangeError ? <Alert type="warning" message={rangeError} /> : null}
        <Radio.Group
          aria-label="节点分类"
          className={styles.categories}
          value={filters.category}
          options={categories}
          optionType="button"
          onChange={(event) =>
            setFilters({
              ...filters,
              category: event.target.value as TrackingCategory,
            })
          }
        />
      </section>
      <TrackingTimeline
        key={JSON.stringify([identity, filters, revision])}
        source={source}
        identity={identity}
        filters={filters}
      />
    </>
  );
}

function Tracker({ source }: { source: string }) {
  const [job, setJob] = useState("");
  const [tenant, setTenant] = useState("");
  const [agent, setAgent] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [searched, setSearched] = useState(false);
  const [candidates, setCandidates] = useState<TrackingCandidate[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [identity, setIdentity] = useState<TaskIdentity | null>(null);
  const [lookupMetadata, setLookupMetadata] = useState<TrackingMetadata | null>(
    null,
  );
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  const search = async (event: React.FormEvent) => {
    event.preventDefault();
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setIdentity(null);
    setLookupMetadata(null);
    setCandidates([]);
    setError("");
    setHasMore(false);
    setSearched(true);
    if (!job.trim()) {
      setError("请输入任务 ID");
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const value = await cronTaskTracking.lookup(
        source,
        job.trim(),
        {
          tenant_id: tenant.trim() || undefined,
          agent_id: agent.trim() || undefined,
        },
        controller.signal,
      );
      if (controller.signal.aborted) return;
      setCandidates(value.candidates);
      setLookupMetadata(value);
      setHasMore(value.has_more);
      if (
        value.candidates.length === 1 &&
        !value.requires_selection &&
        !value.has_more
      )
        setIdentity(value.candidates[0]);
    } catch (failure) {
      if (!controller.signal.aborted)
        setError(
          failure instanceof Error ? failure.message : "查询失败，请重试",
        );
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  };
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1>定时任务追踪</h1>
        <span className={styles.muted}>新版任务 · 单任务时间轴</span>
      </header>
      <form
        onSubmit={search}
        className={styles.query}
        aria-label="查询定时任务"
      >
        <label className={styles.jobField} htmlFor="tracking-job">
          <span>任务 ID</span>
          <Input
            id="tracking-job"
            value={job}
            onChange={(event) => setJob(event.target.value)}
            placeholder="输入要追踪的任务 ID"
            maxLength={128}
          />
        </label>
        <label className={styles.field} htmlFor="tracking-tenant">
          <span>用户 ID（可选）</span>
          <Input
            id="tracking-tenant"
            value={tenant}
            onChange={(event) => setTenant(event.target.value)}
            placeholder="多归属时缩小范围"
            maxLength={128}
          />
        </label>
        <label className={styles.field} htmlFor="tracking-agent">
          <span>Agent（可选）</span>
          <Input
            id="tracking-agent"
            value={agent}
            onChange={(event) => setAgent(event.target.value)}
            placeholder="例如 default"
            maxLength={128}
          />
        </label>
        <Button type="primary" htmlType="submit">
          查询任务
        </Button>
        {loading ? <Spin size="small" aria-label="正在定位任务" /> : null}
      </form>
      {error ? (
        <Alert
          showIcon
          type="error"
          message="任务查询失败"
          description={error}
        />
      ) : null}
      <Coverage value={lookupMetadata || undefined} />
      {!identity && candidates.length ? (
        <section className={styles.candidates} aria-label="选择任务归属">
          <h2>请选择任务归属</h2>
          {hasMore ? (
            <Alert
              type="warning"
              message="候选超过 20 项，请填写用户 ID 或 Agent 后重新查询"
            />
          ) : null}
          <Space wrap>
            {candidates.map((candidate) => (
              <Button
                disabled={hasMore}
                key={JSON.stringify(candidate)}
                onClick={() => setIdentity(candidate)}
              >
                {candidate.tenant_id} / {candidate.agent_id}
                {candidate.name ? ` · ${candidate.name}` : ""}
              </Button>
            ))}
          </Space>
        </section>
      ) : null}
      {identity ? (
        <TaskResults
          key={JSON.stringify(identity)}
          source={source}
          identity={identity}
        />
      ) : !loading && !error && !candidates.length ? (
        <Empty
          description={
            searched
              ? "当前来源内未找到新版任务"
              : "输入任务 ID，查看属性及调度、执行和通知记录"
          }
        />
      ) : null}
    </div>
  );
}

export default function CronTaskTracking() {
  const source = useIframeStore((state) => state.source) || DEFAULT_SOURCE_ID;
  const actor = useIframeStore((state) => state.userId);
  const manager = useIframeStore((state) => state.manager);
  const admin = useIframeStore((state) => state.isSuperManager);
  if (!manager && !admin)
    return (
      <div className={styles.page}>
        <Alert showIcon type="warning" message="仅管理员可访问定时任务追踪" />
      </div>
    );
  return (
    <Tracker
      key={JSON.stringify([source, actor, manager, admin])}
      source={source}
    />
  );
}
