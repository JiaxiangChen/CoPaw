import { schedulerCronApi } from "@/api/modules/schedulerCron";
import type {
  ManagedBatchDeleteResult,
  ManagedCronContext,
  ManagedSelectedChild,
} from "@/api/types/schedulerCron";
import { RequestIdentity } from "@/api/requestIdentity";

export interface ChildrenDeletionPlan {
  items: ManagedSelectedChild[];
  id: string;
  all: boolean;
}

/** 固定确认快照；全部清单一次持久受理，不依赖浏览器分片续办。 */
export function createChildrenDeletion(
  items: ManagedSelectedChild[],
  all = false,
): ChildrenDeletionPlan {
  if (!items.length) throw new Error("没有可删除的子任务");
  if (!all && items.length > 1000) throw new Error("选中删除一次最多1000项");
  const fixed = items.map((item) => ({ ...item }));
  return {
    items: fixed,
    id: new RequestIdentity().for({ items: fixed, all }),
    all,
  };
}

export function deletionInProgress(result: ManagedBatchDeleteResult): boolean {
  return (
    result.operation_status === "pending" ||
    result.operation_status === "running"
  );
}

export async function runChildrenDeletion(
  context: ManagedCronContext,
  sourceJobId: string,
  plan: ChildrenDeletionPlan,
  onProgress: (result: ManagedBatchDeleteResult) => void,
  isActive: () => boolean,
): Promise<ManagedBatchDeleteResult> {
  const submit = plan.all
    ? schedulerCronApi.childrenDeleteAll
    : schedulerCronApi.childrenDelete;
  const result = await submit(context, sourceJobId, plan.items, plan.id);
  if (isActive()) onProgress(result);
  return result;
}
