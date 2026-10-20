import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Select,
  Space,
  Spin,
  Table,
  Tooltip,
} from "antd";
import {
  managedBatchBoard,
  retryCandidates,
  type ManagedRetryBody,
  type ManagedRetryResult,
} from "../../../api/modules/managedBatchBoard";
import { useBoardResource } from "./useBoardResource";

function readPending(key: string): ManagedRetryBody | null {
  try {
    return JSON.parse(sessionStorage.getItem(key) || "null");
  } catch {
    return null;
  }
}
function savePending(key: string, value: ManagedRetryBody | null) {
  try {
    if (value) sessionStorage.setItem(key, JSON.stringify(value));
    else sessionStorage.removeItem(key);
  } catch {
    /* 内存中的同一请求仍可重提。 */
  }
}

export default function FailurePanel({
  source,
  batchId,
  actor,
  onQueued,
}: {
  source: string;
  batchId: string;
  actor: string;
  onQueued: (result: ManagedRetryResult) => void;
}) {
  const storageKey = `managed-batch-retry:${JSON.stringify([
    source,
    actor,
    batchId,
  ])}`;
  const [types, setTypes] = useState<string[]>([]);
  const [refresh, setRefresh] = useState(0);
  const [stopped, setStopped] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [pending, setPending] = useState(() => readPending(storageKey));
  const [notice, setNotice] = useState<{
    type: "success" | "error";
    text: string;
  } | null>(null);
  const alive = useRef(false);
  const locked = useRef(false);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const load = useCallback(
    (signal: AbortSignal) => {
      void refresh;
      return managedBatchBoard.failures(source, batchId, types, signal);
    },
    [source, batchId, types, refresh],
  );
  const preview = useBoardResource(load);
  const danger = preview.value?.items.some((item) =>
    [
      "other",
      "outcome_unknown",
      "execution_timeout",
      "subtask_timeout",
    ].includes(item.failure_type),
  );
  const blocked =
    submitting ||
    (!pending &&
      (preview.loading ||
        !types.length ||
        !preview.value?.items.length ||
        (danger && !stopped)));
  const retry = async () => {
    if (blocked || locked.current) return;
    const command = pending || {
      request_id: crypto.randomUUID(),
      candidates: retryCandidates(preview.value!.items),
      confirm_stopped: stopped,
    };
    locked.current = true;
    setSubmitting(true);
    setPending(command);
    savePending(storageKey, command);
    try {
      const result = await managedBatchBoard.retry(source, batchId, command);
      savePending(storageKey, null);
      if (!alive.current) return;
      setPending(null);
      setNotice({
        type: "success",
        text: `已受理 ${result.queued} 个，跳过 ${result.skipped} 个${
          result.reused ? "（复用原请求结果）" : ""
        }`,
      });
      setRefresh((value) => value + 1);
      onQueued(result);
    } catch (error) {
      if (alive.current)
        setNotice({
          type: "error",
          text: `${
            error instanceof Error ? error.message : "请求未确认"
          }；保留原请求，请点击“核对并重提原请求”。`,
        });
    } finally {
      locked.current = false;
      if (alive.current) setSubmitting(false);
    }
  };
  return (
    <section aria-label="新版失败类型与重试">
      <Space direction="vertical" style={{ width: "100%" }}>
        <strong>失败类型统计（整个批次当前失败的逻辑成员）</strong>
        <Space wrap>
          {preview.value?.counts.map((item) => (
            <span key={item.failure_type}>
              {item.label}：{item.count}
            </span>
          ))}
        </Space>
        {pending && (
          <Alert
            type="warning"
            message="有待核对的人工重试请求，将使用原 request_id 和原候选重提，不根据新预览再次生成执行。"
          />
        )}
        <Select
          aria-label="选择重试失败类型"
          mode="multiple"
          style={{ width: "100%" }}
          placeholder="选择需要重试的失败类型"
          value={types}
          disabled={submitting || !!pending}
          options={preview.value?.counts.map((item) => ({
            value: item.failure_type,
            label: `${item.label} (${item.count})`,
          }))}
          onChange={(value) => {
            setTypes(value);
            setStopped(false);
          }}
        />
        {preview.error && <Alert type="error" message={preview.error} />}
        {preview.value?.has_more && (
          <Alert
            type="warning"
            message="本次预览前 200 个匹配成员，提交后可继续处理剩余成员。"
          />
        )}
        <Spin spinning={preview.loading}>
          <Table
            size="small"
            rowKey="member_id"
            dataSource={preview.value?.items || []}
            pagination={{ pageSize: 5 }}
            scroll={{ x: 700 }}
            columns={[
              {
                title: "失败分类 / 公开错误码",
                render: (_, item) => (
                  <span>
                    {preview.value?.counts.find(
                      (value) => value.failure_type === item.failure_type,
                    )?.label || "其他错误"}
                    <br />
                    {item.settled_error_code || item.error_code || "-"}
                  </span>
                ),
              },
              { title: "执行 ID", dataIndex: "execution_id", ellipsis: true },
              { title: "用户", dataIndex: "tenant_id" },
              { title: "业务尝试", dataIndex: "business_attempt" },
              {
                title: "原始失败原因",
                width: 200,
                render: (_, item) => {
                  const message =
                    item.error_message ||
                    item.settled_error_code ||
                    item.error_code ||
                    "-";
                  return (
                    <Tooltip title={message}>
                      <span>{message}</span>
                    </Tooltip>
                  );
                },
              },
              {
                title: "原 / 当前版本",
                render: (_, item) =>
                  `${item.execution_definition_version} / ${item.definition_version}`,
              },
              {
                title: "类型",
                render: (_, item) =>
                  item.transport_only
                    ? "运输重投（保留快照）"
                    : "业务重试（新快照）",
              },
            ]}
          />
        </Spin>
        {danger && (
          <Checkbox
            checked={stopped}
            disabled={submitting || !!pending}
            onChange={(event) => setStopped(event.target.checked)}
          >
            已核对原执行及其子任务已停止，可重新执行
          </Checkbox>
        )}
        <Space>
          <Button
            type="primary"
            aria-label={
              pending
                ? "核对并重提原请求"
                : `重试预览的 ${preview.value?.items.length || 0} 个失败成员`
            }
            disabled={Boolean(blocked)}
            loading={submitting}
            onClick={() => void retry()}
          >
            {pending
              ? "核对并重提原请求"
              : `重试预览的 ${preview.value?.items.length || 0} 个失败成员`}
          </Button>
          <Button
            disabled={submitting || !!pending}
            onClick={() => setRefresh((value) => value + 1)}
          >
            刷新预览
          </Button>
        </Space>
        <small>人工重试不改变调度模式；批入口暂停不阻止已受理队列执行。</small>
        {notice && <Alert type={notice.type} message={notice.text} showIcon />}
      </Space>
    </section>
  );
}
