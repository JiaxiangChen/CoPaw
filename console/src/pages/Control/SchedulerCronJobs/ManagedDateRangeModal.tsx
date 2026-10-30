import { useEffect, useRef, useState } from "react";
import { Alert, DatePicker, Space } from "antd";
import { Button, Modal } from "@agentscope-ai/design";
import dayjs, { type Dayjs } from "dayjs";
import { schedulerCronApi } from "@/api/modules/schedulerCron";
import type {
  ManagedCronContext,
  ManagedJob,
  ManagedJobDetail,
} from "@/api/types/schedulerCron";
import { isBroadcastChildJob } from "../CronJobs/components/columns";
import { detailToJob, failureMessage, RequestIdentity } from "./helpers";

interface Props {
  context: ManagedCronContext;
  job: ManagedJob;
  onClose: () => void;
  onChanged: () => void;
}

/** 独立修改日期，不走禁止编辑父任务正文的完整更新入口。 */
export function ManagedDateRangeModal({
  context,
  job,
  onClose,
  onChanged,
}: Props) {
  const [detail, setDetail] = useState<ManagedJobDetail | null>(null);
  const [dates, setDates] = useState<[Dayjs, Dayjs] | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(false),
    lock = useRef(false);
  const request = useRef(new RequestIdentity());
  const key = JSON.stringify([
    context.actor_id,
    context.tenant_id,
    context.source_id,
    context.agent_id,
    job.id,
  ]);
  const contextRef = useRef(context);
  contextRef.current = context;
  useEffect(() => {
    let current = true;
    alive.current = true;
    setLoading(true);
    setDetail(null);
    setError(null);
    schedulerCronApi
      .detail({ ...contextRef.current }, job.id)
      .then((value) => {
        if (!current) return;
        if (!value.management_version)
          throw new Error("任务状态缺失，请刷新并核对数据库");
        setDetail(value);
        const period = value.spec.run_date_range;
        setDates(
          period ? [dayjs(period.start_date), dayjs(period.end_date)] : null,
        );
      })
      .catch((failure: unknown) => {
        if (current) setError(failureMessage(failure));
      })
      .finally(() => {
        if (current) setLoading(false);
      });
    return () => {
      current = false;
      alive.current = false;
    };
  }, [key, job.id]);
  const inherited = Boolean(detail && isBroadcastChildJob(detailToJob(detail)));
  const save = async () => {
    if (!detail?.management_version || inherited || lock.current || loading)
      return;
    lock.current = true;
    setSaving(true);
    setError(null);
    const period = dates
      ? {
          start_date: dates[0].format("YYYY-MM-DD"),
          end_date: dates[1].format("YYYY-MM-DD"),
        }
      : null;
    const body = {
      run_date_range: period,
      expected_management_version: detail.management_version,
    };
    try {
      await schedulerCronApi.setDateRange(
        { ...contextRef.current },
        job.id,
        period,
        detail.management_version,
        request.current.for(body),
      );
      if (alive.current) {
        onChanged();
        onClose();
      }
    } catch (failure) {
      if (alive.current) setError(failureMessage(failure));
    } finally {
      lock.current = false;
      if (alive.current) setSaving(false);
    }
  };
  return (
    <Modal
      open
      title={`修改可执行时间区间 · ${job.name}`}
      width={560}
      maskClosable={!saving}
      onCancel={() => {
        if (!saving) onClose();
      }}
      footer={
        <Space>
          <Button disabled={saving} onClick={onClose}>
            取消
          </Button>
          <Button
            type="primary"
            loading={saving}
            disabled={loading || !detail || inherited}
            onClick={() => void save()}
          >
            保存区间
          </Button>
        </Space>
      }
    >
      <Space direction="vertical" style={{ width: "100%" }}>
        {error && <Alert type="error" showIcon message={error} />}
        <div>按北京时间，包含开始和结束当天；未设置为不限日期段。</div>
        {inherited && (
          <Alert
            type="info"
            message="继承父任务，请在父任务修改；子任务不能单独调整区间。"
          />
        )}
        <DatePicker.RangePicker
          value={dates}
          disabled={loading || saving || inherited || !detail}
          format="YYYY-MM-DD"
          placeholder={["开始日期", "结束日期"]}
          style={{ width: "100%" }}
          onChange={(value) =>
            setDates(value?.[0] && value[1] ? [value[0], value[1]] : null)
          }
        />
        <Button
          type="link"
          disabled={loading || saving || inherited || !detail}
          onClick={() => setDates(null)}
        >
          清空区间
        </Button>
        <div>
          保存会同步所有子任务，不需重新分发。本地日期限制立即生效，平台开关由外部日期维护任务同步。
        </div>
      </Space>
    </Modal>
  );
}
