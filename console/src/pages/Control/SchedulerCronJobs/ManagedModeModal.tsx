import { useCallback, useRef, useState } from "react";
import { Alert, Select, Space, Typography } from "antd";
import { Button, Modal } from "@agentscope-ai/design";
import { schedulerCronApi } from "@/api/modules/schedulerCron";
import type {
  ManagedCronContext,
  ManagedDispatchMode,
  ManagedDispatchState,
  ManagedJob,
} from "@/api/types/schedulerCron";
import BatchPriorityEditor from "../CronJobs/components/BatchPriorityEditor";
import { failureMessage, MODE_LABELS, RequestIdentity } from "./helpers";
import { useManagedPanel } from "./useManagedPanel";
import styles from "../CronJobs/index.module.less";

const pending = (value: { state: ManagedDispatchState }) =>
  ["pending", "running"].includes(value.state.sync_status);
interface Props {
  context: ManagedCronContext;
  job: ManagedJob;
  onClose: () => void;
  onChanged: () => void;
}

export function ManagedModeModal({ context, job, onClose, onChanged }: Props) {
  const [target, setTarget] = useState<ManagedDispatchMode | null>(null);
  const [sending, setSending] = useState(false);
  const request = useRef(new RequestIdentity());
  const priorityRequest = useRef(new RequestIdentity());
  const load = useCallback(async () => {
    const [state, priority] = await Promise.all([
      schedulerCronApi.dispatchState(context, job.id),
      schedulerCronApi.priority(context, job.id),
    ]);
    return { state, priority };
  }, [context, job.id]);
  const panel = useManagedPanel(load, pending);
  const state = panel.value?.state;
  const locked =
    !state || state.sync_status !== "completed" || sending || panel.loading;
  const change = async (retry = false) => {
    if (!panel.alive.current || !state || sending) return;
    setSending(true);
    try {
      if (retry && state.operation_id)
        await schedulerCronApi.retry(context, job.id, state.operation_id);
      else {
        const desired = target ?? state.state;
        const id = request.current.for({
          state: desired,
          expected_version: state.version,
        });
        await schedulerCronApi.setDispatchState(
          context,
          job.id,
          desired,
          state.version,
          id,
        );
      }
      if (!panel.alive.current) return;
      request.current.reset();
      await panel.reload();
      if (panel.alive.current) onChanged();
    } catch (failure) {
      if (panel.alive.current) panel.setError(failureMessage(failure));
    } finally {
      if (panel.alive.current) setSending(false);
    }
  };
  return (
    <Modal
      open
      width={720}
      title="调度方式与批调度配置"
      onCancel={onClose}
      footer={null}
      className={styles.batchConfigModal}
    >
      <div className={styles.batchConfigContent}>
        <Typography.Text>任务：{job.name}</Typography.Text>
        <Alert
          type="info"
          message="切换逐对象生效，已入队和正在执行的任务继续执行；源任务自身启停不影响整组入口。"
        />
        {panel.error && <Alert type="error" showIcon message={panel.error} />}
        {state && (
          <Space direction="vertical" style={{ width: "100%" }}>
            <Typography.Text>
              目标：{MODE_LABELS[state.state]}；实际生效：
              {state.effective_state === "mixed"
                ? "混合"
                : MODE_LABELS[state.effective_state]}
              ；同步：{state.sync_status}
            </Typography.Text>
            <Typography.Text>
              已完成 {state.completed_count} / 待处理 {state.pending_count} /
              失败 {state.failed_count}
            </Typography.Text>
            {state.error_code && (
              <Alert type="warning" message={state.error_code} />
            )}
            <Select
              aria-label="调度方式"
              style={{ width: "100%" }}
              value={target ?? state.state}
              disabled={locked}
              onChange={setTarget}
              options={Object.entries(MODE_LABELS).map(([value, label]) => ({
                value,
                label,
              }))}
            />
            <Space>
              <Button
                onClick={() => void panel.reload()}
                loading={panel.loading}
              >
                刷新进度
              </Button>
              <Button
                type="primary"
                disabled={locked || (target ?? state.state) === state.state}
                loading={sending}
                onClick={() => void change()}
              >
                提交切换
              </Button>
              {state.sync_status === "failed" && state.operation_id && (
                <Button disabled={sending} onClick={() => void change(true)}>
                  重试原切换
                </Button>
              )}
            </Space>
          </Space>
        )}
        {panel.value && (
          <BatchPriorityEditor
            key={panel.value.priority.management_version}
            jobId={job.id}
            initial={panel.value.priority.priority}
            disabled={sending}
            savePriority={async (priority) => {
              if (!panel.alive.current)
                throw new Error("工作区已切换，请重新打开配置");
              const version = panel.value!.priority.management_version;
              const body = { ...priority, version };
              const result = await schedulerCronApi.setPriority(
                context,
                job.id,
                priority.user_ids,
                priority.branch_ids,
                version,
                priorityRequest.current.for(body),
              );
              if (result.operation.status !== "completed")
                throw new Error(
                  `优先策略尚未完成：${result.operation.operation_id}，请查询操作进度`,
                );
              priorityRequest.current.reset();
            }}
            onSaved={() => {
              void panel.reload();
              onChanged();
            }}
          />
        )}
      </div>
    </Modal>
  );
}
