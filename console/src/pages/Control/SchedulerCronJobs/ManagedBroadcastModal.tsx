import { useCallback, useRef, useState } from "react";
import { Alert, Space, Typography } from "antd";
import {
  Button,
  InputNumber,
  Modal,
  Switch,
  Table,
} from "@agentscope-ai/design";
import { TenantSelector } from "@/components/TenantSelector";
import { schedulerCronApi } from "@/api/modules/schedulerCron";
import type {
  ManagedBroadcastRequest,
  ManagedBroadcastTask,
  ManagedCronContext,
  ManagedJob,
} from "@/api/types/schedulerCron";
import type { TenantSourceInfo } from "@/api/modules/userInfo";
import { failureMessage, RequestIdentity } from "./helpers";
import { useManagedPanel } from "./useManagedPanel";
import styles from "../CronJobs/index.module.less";

const unfinished = (task: { task: ManagedBroadcastTask | null }) =>
  Boolean(task.task && ["queued", "running"].includes(task.task.status));
interface Props {
  context: ManagedCronContext;
  job: ManagedJob;
  onClose: () => void;
}

/** 分发只决定接收人与散列，调度三态在另一个弹窗管理。 */
export function ManagedBroadcastModal({ context, job, onClose }: Props) {
  const [tenants, setTenants] = useState<string[]>([]);
  const [profiles, setProfiles] = useState<TenantSourceInfo[]>([]);
  const [offset, setOffset] = useState(job.enable_offset ?? true);
  const [hours, setHours] = useState(job.offset_window_hours ?? 4);
  const [sending, setSending] = useState(false);
  const [attempted, setAttempted] = useState(false);
  const request = useRef(new RequestIdentity());
  const taskId = useRef<string | null>(null);
  const load = useCallback(async (): Promise<{
    task: ManagedBroadcastTask | null;
  }> => {
    const current = taskId.current
      ? { task: { task_id: taskId.current } }
      : await schedulerCronApi.currentBroadcast(context, job.id);
    if (!current.task) return { task: null };
    const first = await schedulerCronApi.broadcastTask(
      context,
      job.id,
      current.task.task_id,
    );
    const results = [...(first.results ?? [])];
    let cursor = first.next_cursor;
    const seen = new Set<string>();
    while (cursor) {
      if (seen.has(cursor)) throw new Error("分发结果游标重复，请刷新后重试");
      seen.add(cursor);
      const page = await schedulerCronApi.broadcastTask(
        context,
        job.id,
        first.task_id,
        cursor,
      );
      results.push(...(page.results ?? []));
      cursor = page.next_cursor;
    }
    return { task: { ...first, results } };
  }, [context, job.id]);
  const panel = useManagedPanel(load, unfinished);
  const task = panel.value?.task;
  const locked = Boolean(task) || attempted;
  const submit = async () => {
    if (!panel.alive.current || sending || tenants.length > 1000) return;
    setSending(true);
    try {
      const body = {
        expected_definition_version: job.definition_version,
        targets: tenants.map((id) => {
          const profile = profiles.find((item) => item.tenant_id === id);
          return {
            tenant_id: id,
            tenant_name: profile?.tenant_name,
            bbk_id: profile?.bbk_id,
            agent_id: context.agent_id,
          };
        }),
        enable_offset: offset,
        offset_window_hours: hours,
      };
      const requestBody: ManagedBroadcastRequest = {
        ...body,
        request_id: request.current.for(body),
      };
      setAttempted(true);
      const result = await schedulerCronApi.broadcast(
        context,
        job.id,
        requestBody,
      );
      if (!panel.alive.current) return;
      taskId.current = result.task_id;
      panel.setValue({ task: result });
      panel.setError(null);
      void panel.reload();
    } catch (failure) {
      if (panel.alive.current) panel.setError(failureMessage(failure));
    } finally {
      if (panel.alive.current) setSending(false);
    }
  };
  const newRound = () => {
    request.current.reset();
    taskId.current = null;
    panel.setValue({ task: null });
    setAttempted(false);
    panel.setError(null);
  };
  return (
    <Modal
      open
      title="广播到租户"
      width={800}
      onCancel={onClose}
      footer={
        <Space>
          <Button onClick={() => void panel.reload()} loading={panel.loading}>
            刷新进度
          </Button>
          {task && !unfinished({ task }) && (
            <Button disabled={panel.loading || sending} onClick={newRound}>
              新一轮分发
            </Button>
          )}
          <Button
            type="primary"
            loading={sending}
            disabled={!panel.value || Boolean(task) || tenants.length > 1000}
            onClick={() => void submit()}
          >
            开始分发
          </Button>
        </Space>
      }
    >
      <Space direction="vertical" style={{ width: "100%" }}>
        <Typography.Text>任务：{job.name}</Typography.Text>
        <Alert
          type="info"
          message="0～1000 人；0 人仅调整已有子任务的散列时间。分发失败请在新一轮处理，不改变调度方式。"
        />
        {panel.error && <Alert type="error" showIcon message={panel.error} />}
        <fieldset
          disabled={locked || sending}
          style={{ border: 0, padding: 0, margin: 0 }}
        >
          <TenantSelector
            selectedTenantIds={tenants}
            onChange={(ids) => {
              if (!locked) setTenants(ids);
            }}
            onSelectionInfoChange={(items) => {
              if (!locked) setProfiles(items);
            }}
            excludeTenantId={context.tenant_id}
          />
          <div className={styles.broadcastOffsetControls}>
            <span>散列时间</span>
            <Switch
              checked={offset}
              disabled={locked || sending}
              onChange={setOffset}
            />
            <InputNumber
              min={0}
              max={24}
              value={hours}
              disabled={!offset || locked || sending}
              onChange={(value) => setHours(Number(value ?? 0))}
            />
            <span>小时</span>
          </div>
        </fieldset>
        {task && (
          <>
            <Typography.Text>
              分发 {task.task_id} · {task.status} · 成功 {task.success_count} /
              失败 {task.failed_count} / 待处理 {task.pending_count} · 平台同步{" "}
              {task.sync_status}
            </Typography.Text>
            {task.error_code && (
              <Alert type="warning" message={task.error_code} />
            )}
            <Table
              size="small"
              rowKey={(row) => `${row.tenant_id}:${row.agent_id}:${row.job_id}`}
              dataSource={task.results ?? []}
              pagination={{ pageSize: 10, showSizeChanger: true }}
              scroll={{ x: 750 }}
              columns={[
                { title: "用户", dataIndex: "tenant_id" },
                { title: "Agent", dataIndex: "agent_id" },
                { title: "子任务", dataIndex: "job_id" },
                { title: "状态", dataIndex: "status" },
                { title: "处理", dataIndex: "operation" },
                { title: "提前分钟", dataIndex: "offset_minutes" },
                {
                  title: "告警 / 错误",
                  render: (_, row) => row.error_code || row.warning || "—",
                },
              ]}
            />
          </>
        )}
      </Space>
    </Modal>
  );
}
