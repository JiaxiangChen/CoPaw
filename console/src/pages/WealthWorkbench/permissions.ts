/**
 * 智能财富工作台 —— 角色与页面权限
 *
 * 权限模型：
 * - 角色（WealthRole）是唯一权限主体，矩阵表 ROLE_PERMISSIONS 是唯一判定点；
 *   组件与路由守卫一律经 canAccessPage 判定，不再各自读账户字段。
 * - 生产环境角色由父系统身份解析：cookie positionID 经 handleUrlOriginParam
 *   写入 iframeStore.positionId，再由 resolveRole 映射为角色。
 *
 * 矩阵（与原型一致的最小版本）：
 * - 三个角色均可访问 规划看板 / 创建计划；
 * - 仅客户经理可访问 任务三页（今日任务 / 待触达客户 / 已完成）；
 * - 未识别身份（unknown）无任何页面权限，入口整页拦截。
 * 操作级权限（发布 / 编辑 / 删除 / 触达登记）暂不按角色区分，矩阵预留扩展。
 */

/** 工作台角色；unknown 为未识别身份的伪角色（deny by default，无任何权限） */
export type WealthRole = "rm" | "president" | "middle" | "unknown";

/** 看板来源范围；与后端管理角色的来源过滤保持一致。 */
export const BOARD_SOURCES_BY_ROLE: Record<WealthRole, readonly string[]> = {
  rm: ["分行关注", "行长关注", "我的关注"],
  president: ["分行关注", "行长关注"],
  middle: ["分行关注"],
  unknown: [],
};

/** 受权限控制的页面组；tasks 代表今日任务 / 待触达客户 / 已完成三页 */
export type WealthPage = "board" | "create" | "tasks";

/** 角色 × 页面矩阵：唯一权限判定来源，新增权限点只改这里 */
export const ROLE_PERMISSIONS: Record<WealthRole, readonly WealthPage[]> = {
  rm: ["board", "create", "tasks"],
  president: ["board", "create"],
  middle: ["board", "create"],
  unknown: [],
};

/**
 * 兜底角色：positionId 缺失或未命中映射表时使用（deny by default）。
 * 未识别身份无任何页面权限：入口整页拦截为「暂无访问权限」提示，
 * 不加载业务数据，见 index.tsx 的 unknown 分支。
 */
export const FALLBACK_ROLE: WealthRole = "unknown";

/**
 * positionId → 角色映射表（父系统岗位编号清单）：
 * - RB0101 客户经理；RB1101 / RB0306 均为支行行长；RB0301 / RB0305 均为分行中台。
 * - RB0108 理财主管岗（综合），与支行行长使用同一权限角色。
 * 新增岗位类型时在此登记；未知编号由 resolveRole 回退 FALLBACK_ROLE 并留痕。
 */
export const POSITION_ROLE_MAP: Record<string, WealthRole> = {
  RB0101: "rm",
  RB1101: "president",
  RB0306: "president",
  RB0108: "president", // 理财主管岗（综合），权限等同支行行长
  RB0301: "middle",
  RB0305: "middle",
};

/**
 * 角色展示元数据（非 mock）：角色显示名 + 发布入参的规划来源标签。
 * source 与后端 SOURCE_LABEL_BY_ROLE 保持一致。
 */
export const ROLE_ACCOUNT_META: Record<
  WealthRole,
  { role: string; source: string }
> = {
  rm: { role: "客户经理", source: "我的关注" },
  president: { role: "支行行长", source: "行长关注" },
  middle: { role: "分行中台", source: "分行关注" },
  unknown: { role: "未识别", source: "" },
};

/** 由父系统岗位编号解析工作台角色；未命中时回退 FALLBACK_ROLE 并留痕 */
export function resolveRole(positionId: string | null | undefined): WealthRole {
  if (!positionId) return FALLBACK_ROLE;
  const role = POSITION_ROLE_MAP[positionId];
  if (role) return role;
  console.warn(
    `[WealthWorkbench] 未知 positionId「${positionId}」，回退为角色 ${FALLBACK_ROLE}`,
  );
  return FALLBACK_ROLE;
}

/** 角色是否可访问某页面组 */
export function canAccessPage(role: WealthRole, page: WealthPage): boolean {
  return ROLE_PERMISSIONS[role].includes(page);
}

/**
 * 角色发布规划时是否需要选择分发目标。
 * 客户经理的规划只发给自己（sapId 即本人 userId），行长/中台需分发给下属客户经理。
 */
export function needsDistributeTargets(role: WealthRole): boolean {
  return role !== "rm";
}
