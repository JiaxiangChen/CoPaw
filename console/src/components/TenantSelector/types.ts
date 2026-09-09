import type { ReactNode } from "react";
import type { TenantSourceInfo } from "@/api/modules/userInfo";
import type { UserSkillStatus } from "@/api/modules/market";

export type TargetMode = "bbk_id" | "user_id";

export interface TenantSelectorProps {
  /** 已选中的租户 ID 列表 */
  selectedTenantIds: string[];

  /** 选择变更回调 */
  onChange: (tenantIds: string[]) => void;

  /** 选中租户详情变更回调 */
  onSelectionInfoChange?: (tenants: TenantSourceInfo[]) => void;

  /** 提示文本 */
  hint?: ReactNode;

  /** 当前租户 ID（用于过滤自身） */
  excludeTenantId?: string;

  /** 加载失败回调 */
  onLoadError?: (error: Error) => void;

  /** 用户技能状态映射（用于显示分发状态标记） */
  userSkillStatusMap?: Map<string, UserSkillStatus>;

  /** 当前技能版本（用于显示更新后的目标版本） */
  skillVersion?: string;

  /** 模式变更回调，返回当前选择模式 */
  onTargetModeChange?: (mode: TargetMode) => void;

  /** 已分发用户列表（用于根据模式选择机构或用户） */
  distributedUserIds?: string[];

  /** 触发计数器：每次勾选 checkbox 时增加，强制触发选择 */
  distributedTriggerKey?: number;

  /** 限制可选租户范围（不传则使用当前 source 下全部租户） */
  allowedTenantIds?: string[];

  /** 是否允许输入列表外的租户 ID */
  allowManualIds?: boolean;

  /** 追加可选租户（用于租户目录中不存在但仍有本地资源的租户） */
  additionalTenantOptions?: TenantSourceInfo[];
}
