import { useCallback, useRef, useState } from "react";
import { Alert, Descriptions, Space } from "antd";
import { Button, Modal } from "@agentscope-ai/design";
import { schedulerCronApi } from "@/api/modules/schedulerCron";
import type {
  ManagedCronContext,
  ManagedExecution,
  ManagedJob,
  ManagedOperation,
} from "@/api/types/schedulerCron";
import { failureMessage, executionNeedsPolling } from "./helpers";
import { useManagedPanel } from "./useManagedPanel";

const pending = (value: {
  operation: ManagedOperation | null;
  execution: ManagedExecution | null;
}) =>
  Boolean(
    (value.operation &&
      ["pending", "running"].includes(value.operation.status)) ||
      executionNeedsPolling(value.execution),
  );
interface Props {
  context: ManagedCronContext;
  job: ManagedJob;
  initialOperation?: ManagedOperation | null;
  initialExecution?: ManagedExecution | null;
  onClose: () => void;
  onChanged: () => void;
}

export function ManagedProgressModal({
  context,
  job,
  initialOperation,
  initialExecution,
  onClose,
  onChanged,
}: Props) {
  const operationId = useRef(initialOperation?.operation_id);
  const [retrying, setRetrying] = useState(false);
  const load = useCallback(async () => {
    const [operation, execution] = await Promise.all([
      operationId.current
        ? schedulerCronApi.operation(context, job.id, operationId.current)
        : schedulerCronApi
            .pending(context, job.id)
            .then((result) => result.operation),
      initialExecution
        ? schedulerCronApi.execution(
            context,
            job.id,
            initialExecution.execution_id,
          )
        : Promise.resolve(null),
    ]);
    if (operation) operationId.current = operation.operation_id;
    return { operation, execution };
  }, [context, job.id, initialExecution]);
  const panel = useManagedPanel(load, pending);
  const operation = panel.value?.operation ?? initialOperation;
  const execution = panel.value?.execution ?? initialExecution;
  const retry = async () => {
    if (!panel.alive.current || !operation || retrying) return;
    setRetrying(true);
    try {
      await schedulerCronApi.retry(context, job.id, operation.operation_id);
      if (panel.alive.current) {
        await panel.reload();
        onChanged();
      }
    } catch (failure) {
      if (panel.alive.current) panel.setError(failureMessage(failure));
    } finally {
      if (panel.alive.current) setRetrying(false);
    }
  };
  return (
    <Modal
      open
      title={`操作与执行进度 · ${job.name}`}
      onCancel={onClose}
      footer={
        <Button loading={panel.loading} onClick={() => void panel.reload()}>
          刷新进度
        </Button>
      }
    >
      <Space direction="vertical" style={{ width: "100%" }}>
        {panel.error && <Alert type="error" message={panel.error} showIcon />}
        {operation ? (
          <Descriptions
            column={1}
            size="small"
            items={[
              { key: "id", label: "操作 ID", children: operation.operation_id },
              { key: "action", label: "操作", children: operation.action },
              { key: "stage", label: "阶段", children: operation.stage },
              { key: "status", label: "状态", children: operation.status },
              {
                key: "error",
                label: "错误",
                children: operation.error_code || "—",
              },
            ]}
          />
        ) : (
          !panel.loading && (
            <Alert type="info" message="当前没有未完成的管理操作。" />
          )
        )}
        {operation?.status === "failed" &&
          !["mode_sync", "batch_delete"].includes(operation.action) && (
            <Button loading={retrying} onClick={() => void retry()}>
              重试原操作
            </Button>
          )}
        {execution && (
          <Descriptions
            column={1}
            size="small"
            items={[
              {
                key: "run",
                label: "执行 ID",
                children: execution.execution_id,
              },
              {
                key: "status",
                label: "主执行状态",
                children: execution.status,
              },
              {
                key: "settled",
                label: "整轮结果",
                children:
                  execution.settled_status ??
                  (execution.status === "success" &&
                  execution.settled_status === null
                    ? "等待异步结果确认"
                    : "—"),
              },
              {
                key: "settled-error",
                label: "整轮错误",
                children: execution.settled_error_code || "—",
              },
              {
                key: "error",
                label: "执行错误",
                children: execution.error_code || "—",
              },
            ]}
          />
        )}
        <Alert
          type="info"
          message="已受理或排队不代表正在运行；业务执行失败不会自动重跑。"
        />
      </Space>
    </Modal>
  );
}
