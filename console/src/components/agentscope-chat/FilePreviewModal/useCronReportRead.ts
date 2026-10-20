import { useEffect, useRef } from "react";
import type { CronTaskOrigin } from "@/api/types/cronTaskSource";
import { cronAccessKey, currentCronContext } from "@/api/cronTaskContext";
import { resolveTaskOrigin } from "@/api/modules/cronTaskSources";
import { markTaskReferenceRead } from "@/api/modules/cronTaskActions";
import { useCronAccessKey } from "@/hooks/useCronAccessKey";
import { RequestIdentity } from "@/api/requestIdentity";

interface Props {
  open: boolean;
  taskId?: string | null;
  contentKey: string;
  knownTaskId?: string | null;
  knownOrigin?: CronTaskOrigin | null;
  onFailure: (message: string) => void;
}

/** 内容已确认后才标读；关闭/换报告/换身份时，迟到的归属查询不会继续写入。 */
export function useCronReportRead(props: Props): void {
  const accessKey = useCronAccessKey();
  const current = useRef(props);
  current.current = props;
  const requests = useRef(new Map<string, RequestIdentity>());
  const id = String(props.taskId || "").trim();
  const stamp =
    props.open && id
      ? JSON.stringify([
          accessKey,
          id,
          props.contentKey,
          props.knownTaskId,
          props.knownOrigin?.owner,
          props.knownOrigin?.accessKey,
        ])
      : "";
  useEffect(() => {
    if (!stamp) return;
    const snapshot = current.current;
    const context = currentCronContext();
    const request = requests.current.get(stamp) ?? new RequestIdentity();
    requests.current.set(stamp, request);
    let active = true;
    const mark = async () => {
      const hint =
        snapshot.knownTaskId === id &&
        snapshot.knownOrigin?.accessKey === accessKey
          ? snapshot.knownOrigin
          : null;
      const origin = hint ?? (await resolveTaskOrigin(context, id));
      if (!active || cronAccessKey() !== accessKey) return;
      await markTaskReferenceRead(id, origin, true, request.for(stamp));
      request.reset();
    };
    void mark().catch(() => {
      if (active && cronAccessKey() === accessKey)
        snapshot.onFailure(
          "报告已展示，但任务已读处理失败；未读状态保留，可重新打开后重试",
        );
    });
    return () => {
      active = false;
    };
  }, [stamp, accessKey, id]);
}
