/**
 * 智能财富工作台 —— 领域类型定义
 * 移植自单文件原型「智能财富工作台.html」。
 * 第二阶段起与后端接口契约对齐：场景来自 /wealth/scene-skills，
 * 规划来自 /wealth/plans，id 均为字符串，排程与控制台定时任务同模型。
 */
import type { CronParts } from "@/utils/parseCron";
import type { WealthRole } from "./permissions";

/** 工作台角色对应的登录账户（mock 期的角色预览身份） */
export interface Account {
  /** 与 WealthRole 一一对应：rm 客户经理 / president 支行行长 / middle 分行中台 */
  id: WealthRole;
  name: string;
  /** 角色显示名：客户经理 / 支行行长 / 分行中台 */
  role: string;
  department: string;
  /** 规划来源：我的关注 / 行长关注 / 分行关注 */
  source: string;
}

/** 经营场景（/wealth/scene-skills 返回，与外部 skill-config 接口字段对齐） */
export interface Scene {
  /** 场景标识：外部接口 skillId */
  id: string;
  /** 外部接口 itemId，发布时回传 */
  itemId?: string | null;
  name: string;
  /** 产品大类中文名：保险 / 贷款 / 存款 / 理财 / 基金 / 代发 */
  category: string;
  /** 产品大类英文 code：insurance / loan / deposit / finance / fund / payroll */
  categoryCode: string;
  icon: string;
  desc: string;
  /** 来源标签：总部预置 / 分行自建 */
  source: string;
  cronExample?: string | null;
  mcpRelations: string[];
}

/** 规划/草稿中的单个场景条目 */
export interface PlanItem {
  /** 场景 id（skillId） */
  id: string;
  /** 场景名冗余存储，不依赖场景池即可展示与回填 */
  sceneName: string;
  /** 产品大类中文名 */
  categoryLabel: string;
  /** 产品大类英文 code（发布入参用） */
  categoryCode: string;
  itemId?: string | null;
  /** 场景技能自带的 cron 示例文本（发布时作为定时任务请求内容） */
  cronExample?: string | null;
  mcpRelations: string[];
  direction: string;
  /** 任务周期（有效期）：本月 / 本季 / 今日 / T+1日 / 自定义 */
  cycle: string;
  start?: string;
  end?: string;
  /** 执行排程：与控制台定时任务同一模型（hourly/daily/weekly/custom） */
  schedule: CronParts;
}

/** 工作规划（看板展示模型，由后端 PlanView 映射而来） */
export interface Plan {
  id: string;
  name: string;
  source: string;
  desc: string;
  customers: number;
  tasks: number;
  rate: number;
  /** 状态栏文案（后端聚合）：发布中/发布失败/分发中/已自动下发/分发失败 */
  status: string;
  /** 发布生命周期：publishing / published / publish_failed */
  publishStatus?: string;
  /** 仅创建人可编辑/移除；分发给我的规划为只读 */
  editable?: boolean;
  period: string;
  start: string;
  end: string;
  items?: PlanItem[];
  /** 发布时的分发目标 sapId 列表；客户经理为本人 userId */
  targetSapIds?: string[];
  /** 已保存的分发目标信息，用于编辑回显及重新发布。 */
  targets?: DistributeTarget[];
}

/** 创建计划页的草稿（会话级，不持久化） */
export interface Draft {
  name: string;
  items: PlanItem[];
}

/** 规划统计展示模型：targetTaskCount 映射为 generatedTaskCount。 */
export interface SkillStat {
  targetCustomerCount: number;
  generatedTaskCount: number;
}

/** 分发目标用户（行长/中台发布规划时选择的下属客户经理） */ export interface DistributeTarget {
  /** 发布目标 SAP ID（机构岗位人员接口返回的 sapID） */
  sapId: string;
  name: string;
  /** 所属机构名称（可能为空） */
  orgName: string;
}

/** 客户 / 触达记录 */
export interface CustomerField {
  name: string;
  label: string;
  value: string;
}

export interface Customer {
  /** 页面内唯一标识：`${skillId}|${custUid}`（同一客户可出现在多个任务下） */
  id: string;
  /** 外部 name-list 返回的客户 UID */
  custUid: string;
  /** 所属经营场景的技能 ID（触达登记/名单接口入参） */
  skillId: string;
  name: string;
  /** 重点标签：行长指派 / 分行重点 / 我的关注，可多值 */
  label: string;
  /** 推荐理由（外部 name-list 的 recomReason） */
  reason: string;
  category: string;
  task: string;
  /** 客户名单接口 skillList 返回的去重技能名称 */
  skillNames?: string[];
  /** 接口原始接触状态：0 未接触、1 已接触，其余或缺失为未知。 */
  touched?: number | null;
  done: boolean;
  channel: string;
  time: string;
  note: string;
  opportunities?: string[];
  /** 机构编码（外部 name-list 的 bbkOrgId，用于电访/客户洞察外链签名） */
  bbkOrgId?: string;
  /** 客户详情跳转链接（外部 name-list 的 filename，可直接 iframe 渲染） */
  link?: string;
  dynamicFields?: CustomerField[];
  groupFields?: CustomerField[];
}
