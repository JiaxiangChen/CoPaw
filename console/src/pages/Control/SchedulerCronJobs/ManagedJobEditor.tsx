import { useEffect, useRef, useState } from "react";
import { Alert } from "antd";
import { Button, Drawer, Form } from "@agentscope-ai/design";
import api from "@/api";
import {
  schedulerCronApi,
  targetManagedContext,
} from "@/api/modules/schedulerCron";
import type {
  ManagedCronContext,
  ManagedCronSpec,
  ManagedJob,
  ManagedOperation,
} from "@/api/types/schedulerCron";
import { useExecutionModelOptions } from "@/hooks/useExecutionModelOptions";
import { CronJobFormBody } from "../CronJobs/components/CronJobFormBody";
import { DEFAULT_FORM_VALUES } from "../CronJobs/components/constants";
import {
  buildSkillSelectOptions,
  type CronJobFormValues,
  type SkillSelectOption,
} from "../CronJobs/helpers";
import {
  buildManagedCronFormValues,
  buildManagedCronSubmitPayload,
} from "./scheduleForm";
import { detailToJob, failureMessage, RequestIdentity } from "./helpers";
import styles from "../CronJobs/index.module.less";

interface Props {
  context: ManagedCronContext;
  job: ManagedJob | null;
  saving: boolean;
  onSave: (
    job: ManagedJob | null,
    spec: ManagedCronSpec,
    requestId: string,
  ) => Promise<ManagedOperation | null>;
  onClose: () => void;
  onLoaded?: (job: ManagedJob) => void;
}

/** 原表单只负责编辑展示；服务端 ID、版本和启停走新版协议。 */
export function ManagedJobEditor({
  context,
  job,
  saving,
  onSave,
  onClose,
  onLoaded,
}: Props) {
  const [form] = Form.useForm<CronJobFormValues>();
  const models = useExecutionModelOptions(true);
  const [skills, setSkills] = useState<SkillSelectOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState(job);
  const request = useRef(new RequestIdentity());
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    let alive = true;
    const load = async () => {
      setLoading(true);
      setReady(false);
      setError(null);
      try {
        const [detail, options, timezone] = await Promise.all([
          job
            ? schedulerCronApi.detail(
                targetManagedContext(context, job),
                job.id,
              )
            : Promise.resolve(null),
          api.listSweSkills(context.source_id),
          api.getUserTimezone(),
        ]);
        if (!alive) return;
        const current = detail ? detailToJob(detail) : null;
        if (
          current &&
          job &&
          current.definition_version < job.definition_version
        )
          throw new Error("任务详情版本落后于列表，请刷新后重新编辑。");
        if (current) onLoaded?.(current);
        setSnapshot(current);
        setSkills(buildSkillSelectOptions(options.skills ?? []));
        form.setFieldsValue(
          current
            ? buildManagedCronFormValues(current)
            : {
                ...DEFAULT_FORM_VALUES,
                id: "由系统自动分配",
                schedule: {
                  ...DEFAULT_FORM_VALUES.schedule,
                  timezone: timezone.timezone || "UTC",
                },
                dispatch: {
                  ...DEFAULT_FORM_VALUES.dispatch,
                  target: { user_id: context.tenant_id, session_id: "" },
                },
              },
        );
        setReady(true);
      } catch (failure) {
        if (alive) setError(failureMessage(failure));
      } finally {
        if (alive) setLoading(false);
      }
    };
    void load();
    return () => {
      alive = false;
      mounted.current = false;
    };
  }, [context, job, form, onLoaded]);
  const submit = async (values: CronJobFormValues) => {
    if (!ready || loading || saving) return;
    try {
      const spec = buildManagedCronSubmitPayload(values, snapshot);
      const operation = await onSave(snapshot, spec, request.current.for(spec));
      if (mounted.current && operation) onClose();
      else if (mounted.current)
        setError(
          "保存结果尚未确认，请保持原内容重试，并核对列表和操作进度；不要重复新建任务。",
        );
    } catch (failure) {
      if (mounted.current) setError(failureMessage(failure));
    }
  };
  return (
    <Drawer
      open
      width={600}
      placement="right"
      title={job ? "编辑定时任务" : "创建定时任务"}
      onClose={onClose}
      destroyOnClose
      footer={
        <div className={styles.formActions}>
          <Button onClick={onClose}>取消</Button>
          <Button
            type="primary"
            disabled={loading || !ready}
            loading={saving}
            onClick={() => form.submit()}
          >
            保存
          </Button>
        </div>
      }
    >
      {error && <Alert type="error" showIcon message={error} />}
      {job && (
        <Alert
          type="info"
          message="启停请使用列表中的独立操作；存在分发子任务时不可修改源任务内容。"
        />
      )}
      <Form
        form={form}
        layout="vertical"
        disabled={loading || saving}
        onFinish={submit}
      >
        <CronJobFormBody
          form={form}
          executionModelOptions={models.options}
          executionModelLoading={models.loading}
          tenantDefaultModelLabel={models.tenantDefaultLabel}
          skillOptions={skills}
          skillOptionsLoading={loading}
          idDisabled
          enabledDisabled={Boolean(job)}
        />
      </Form>
    </Drawer>
  );
}
