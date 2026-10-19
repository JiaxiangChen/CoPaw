import { useMemo, useRef, useState } from "react";
import { Alert, Space } from "antd";
import { Button, Card, Table } from "@agentscope-ai/design";
import { useTranslation } from "react-i18next";
import type { ManagedCronContext, ManagedJob } from "@/api/types/schedulerCron";
import { targetManagedContext } from "@/api/modules/schedulerCron";
import { PageHeader } from "@/components/PageHeader";
import { useExecutionModelOptions } from "@/hooks/useExecutionModelOptions";
import { useAppMessage } from "@/hooks/useAppMessage";
import { useAgentStore } from "@/stores/agentStore";
import { useIframeStore } from "@/stores/iframeStore";
import { DEFAULT_SOURCE_ID } from "@/constants/identity";
import { getUserId } from "@/utils/identity";
import { createManagedColumns, type ManagedJobPanel } from "./columns";
import { useManagedCronJobs } from "./useManagedCronJobs";
import { ManagedJobEditor } from "./ManagedJobEditor";
import { ManagedBroadcastModal } from "./ManagedBroadcastModal";
import { ManagedChildrenModal } from "./ManagedChildrenModal";
import { ManagedModeModal } from "./ManagedModeModal";
import { ManagedProgressModal } from "./ManagedProgressModal";
import { failureMessage, RequestIdentity } from "./helpers";
import styles from "../CronJobs/index.module.less";

type Panel = ManagedJobPanel | { kind: "create" } | null;

function SchedulerCronWorkspace({ context }: { context: ManagedCronContext }) {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const [includeDeleted, setIncludeDeleted] = useState(false);
  const data = useManagedCronJobs(context, includeDeleted);
  const models = useExecutionModelOptions(true);
  const [panel, setPanel] = useState<Panel>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const requests = useRef(new Map<string, RequestIdentity>());
  const refresh = () => void data.reload();
  const close = () => setPanel(null);
  const perform = async (
    kind: "toggle" | "remove" | "run",
    job: ManagedJob,
  ) => {
    const key = `${kind}:${job.tenant_id}:${job.source_id}:${job.agent_id}:${job.id}`;
    const identity = requests.current.get(key) ?? new RequestIdentity();
    requests.current.set(key, identity);
    setActionError(null);
    try {
      const result = await data[kind](
        job,
        identity.for({
          definition: job.definition_version,
          management: job.management_version,
        }),
      );
      if (result) {
        identity.reset();
        setPanel({ kind: "progress", job });
      }
    } catch (failure) {
      setActionError(failureMessage(failure));
    }
  };
  const columns = createManagedColumns({
    t,
    models,
    message,
    perform,
    setPanel,
    busy: data.busy,
  });
  const target = useMemo(
    () =>
      panel && "job" in panel
        ? targetManagedContext(context, panel.job)
        : context,
    [context, panel],
  );
  return (
    <div className={styles.cronJobsPage}>
      <PageHeader
        items={[{ title: t("nav.runCenter") }, { title: "定时任务（新）" }]}
        extra={
          <Space>
            <Button
              type={includeDeleted ? "primary" : "default"}
              aria-pressed={includeDeleted}
              onClick={() => setIncludeDeleted((current) => !current)}
            >
              包含已删除任务（查看清理进度）
            </Button>
            <Button onClick={refresh} loading={data.loading}>
              刷新
            </Button>
            <Button
              type="primary"
              disabled={data.busy}
              onClick={() => setPanel({ kind: "create" })}
            >
              + {t("cronJobs.createJob")}
            </Button>
          </Space>
        }
      />
      {(data.error || actionError) && (
        <Alert type="error" showIcon message={actionError || data.error} />
      )}
      {data.operation && (
        <Alert
          type={data.operation.status === "failed" ? "warning" : "info"}
          message={`操作 ${data.operation.operation_id} · ${
            data.operation.status
          } · ${data.operation.stage}${
            data.operation.error_code ? ` · ${data.operation.error_code}` : ""
          }`}
        />
      )}
      <Card className={styles.tableCard} bodyStyle={{ padding: 0 }}>
        <Table
          columns={columns}
          dataSource={data.jobs}
          loading={data.loading}
          rowKey={(job) =>
            JSON.stringify([job.tenant_id, job.source_id, job.agent_id, job.id])
          }
          scroll={{ x: 3390 }}
          pagination={{
            defaultPageSize: 10,
            showSizeChanger: true,
            pageSizeOptions: ["10", "20", "50", "100"],
            showTotal: (total) => `共 ${total} 条`,
          }}
        />
      </Card>
      {(panel?.kind === "create" || panel?.kind === "edit") && (
        <ManagedJobEditor
          key={panel.kind === "edit" ? panel.job.id : "create"}
          context={target}
          job={panel.kind === "edit" ? panel.job : null}
          saving={data.busy}
          onLoaded={data.reconcileJob}
          onClose={close}
          onSave={(job, spec, id) =>
            job ? data.update(job, spec, id) : data.create(spec, id)
          }
        />
      )}
      {panel?.kind === "broadcast" && (
        <ManagedBroadcastModal
          context={target}
          job={panel.job}
          onClose={close}
        />
      )}
      {panel?.kind === "children" && (
        <ManagedChildrenModal
          context={target}
          job={panel.job}
          onClose={close}
          onChanged={refresh}
          onEdit={(job) => setPanel({ kind: "edit", job })}
          onProgress={(job) => setPanel({ kind: "progress", job })}
        />
      )}
      {panel?.kind === "mode" && (
        <ManagedModeModal
          context={target}
          job={panel.job}
          onClose={close}
          onChanged={refresh}
        />
      )}
      {panel?.kind === "progress" && (
        <ManagedProgressModal
          context={target}
          job={panel.job}
          onClose={close}
          onChanged={refresh}
          initialOperation={
            data.operation?.job_id === panel.job.id ? data.operation : null
          }
          initialExecution={
            data.execution?.job_id === panel.job.id ? data.execution : null
          }
        />
      )}
    </div>
  );
}

/** 工作区和角色变化时整体卸载子页面，旧弹窗不会借用新身份提交。 */
export default function SchedulerCronJobsPage() {
  const user = useIframeStore((store) => store.userId);
  const source = useIframeStore((store) => store.source);
  const manager = useIframeStore((store) => store.manager);
  const admin = useIframeStore((store) => store.isSuperManager);
  const agent = useAgentStore((store) => store.selectedAgent);
  const actor = user || getUserId();
  const context = useMemo(
    () => ({
      actor_id: actor,
      tenant_id: actor,
      source_id: source || DEFAULT_SOURCE_ID,
      agent_id: agent,
    }),
    [actor, source, agent],
  );
  return (
    <SchedulerCronWorkspace
      key={JSON.stringify([context, manager, admin])}
      context={context}
    />
  );
}
