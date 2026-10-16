/**
 * ！！！！注意：此页面需要单独评估
 * ！！！！！！！
 */
import { parseCron, serializeCron } from "@/utils/parseCron";
import { request } from "../../api/request";
import { buildAuthHeaders } from "../../api/authHeaders";
import { getWPlusCookie } from "../../utils/cookie-utils";
import { getIframeContext } from "../../stores/iframeStore";
import { DEFAULT_BBK_ID } from "../../constants/identity";
// import { Base64 } from "js-base64";
import { SCENE_CATEGORIES } from "./mock/data";
import { DEFAULT_SCHEDULE } from "./utils";
import type {
  Account,
  Customer,
  DistributeTarget,
  Draft,
  Plan,
  PlanItem,
  Scene,
  SkillStat,
} from "./types";

/** 模拟网络延迟（毫秒），让离线 mock 的异步行为贴近真实接口 */
const MOCK_LATENCY_MS = 60;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

// ---------------------------------------------------------------------------
// 后端接口契约（与 src/swe/app/wealth_plans/models.py 对齐）
// ---------------------------------------------------------------------------

interface PlanSceneView {
  scene_id: string;
  scene_name: string;
  category: string;
  category_label: string;
  item_id?: string | null;
  direction?: string | null;
  cycle?: string | null;
  start_date?: string | null;
  end_date?: string | null;
  cron_expr: string;
  cron_example?: string | null;
  mcp_relations: string[];
}

interface PlanTargetView {
  sap_id: string;
  name?: string | null;
}

interface PlanView {
  id: string;
  name: string;
  description?: string | null;
  source_label?: string | null;
  period_start?: string | null;
  period_end?: string | null;
  publish_status: string;
  board_status: string;
  editable: boolean;
  scenes: PlanSceneView[];
  targets: PlanTargetView[];
}

interface PlanListResponse {
  items: PlanView[];
}

interface PlanCreateResponse {
  id: string;
  publish_status: string;
}

interface SceneSkillItem {
  skillId: string;
  itemId?: string | null;
  senceName: string;
  category: string;
  senceDesc?: string | null;
  cronExample?: string | null;
  mcpRelationList: string[];
  skillBbkLabel?: string | null;
  bbkId?: string | null;
}

interface SceneSkillListResponse {
  items: SceneSkillItem[];
}

// ---------------------------------------------------------------------------
// 视图 → 展示模型映射
// ---------------------------------------------------------------------------

/** 大类英文 code → 场景图标（原型图标集） */
const CATEGORY_ICON: Record<string, string> = {
  insurance: "shield",
  loan: "bank",
  deposit: "safe",
  finance: "chart",
  fund: "pie",
  payroll: "money",
};

const CATEGORY_LABEL: Record<string, string> = Object.fromEntries(
  SCENE_CATEGORIES.map((c) => [c.code, c.label]),
);

function mapScene(item: SceneSkillItem): Scene {
  return {
    id: item.skillId,
    itemId: item.itemId,
    name: item.senceName,
    category: CATEGORY_LABEL[item.category] ?? item.category,
    categoryCode: item.category,
    icon: CATEGORY_ICON[item.category] ?? "layer",
    desc: item.senceDesc ?? "",
    source: item.skillBbkLabel ?? "",
    cronExample: item.cronExample,
    mcpRelations: item.mcpRelationList ?? [],
  };
}

function mapSceneItem(scene: PlanSceneView): PlanItem {
  return {
    id: scene.scene_id,
    sceneName: scene.scene_name,
    categoryLabel: scene.category_label,
    categoryCode: scene.category,
    itemId: scene.item_id,
    cronExample: scene.cron_example,
    mcpRelations: scene.mcp_relations ?? [],
    direction: scene.direction ?? "",
    cycle: scene.cycle ?? "自定义",
    start: scene.start_date ?? undefined,
    end: scene.end_date ?? undefined,
    schedule: parseCron(scene.cron_expr),
  };
}

function mapPlan(view: PlanView): Plan {
  const items = view.scenes.map(mapSceneItem);
  const cycles = [...new Set(items.map((x) => x.cycle))];
  return {
    id: view.id,
    name: view.name,
    desc: view.description ?? items[0]?.direction ?? "",
    source: view.source_label ?? "我的关注",
    customers: 0,
    tasks: 0,
    rate: 0,
    status: view.board_status,
    publishStatus: view.publish_status,
    editable: view.editable,
    period: cycles.length === 1 ? cycles[0] : "自定义",
    start: view.period_start ?? "",
    end: view.period_end ?? "",
    items,
    targetSapIds: view.targets.map((t) => t.sap_id),
    targets: view.targets.map((t) => ({
      sapId: t.sap_id,
      name: t.name || t.sap_id,
      orgName: "",
    })),
  };
}

// ---------------------------------------------------------------------------
// 场景查询：/wealth/scene-skills（外部接口代理）
// ---------------------------------------------------------------------------

/**
 * 按产品大类查询经营场景；categoryCode 传空串表示查询全部大类。
 * 接口不可达时返回空列表，由页面展示空态，不做假数据兜底。
 */
export async function fetchScenesByCategory(
  categoryCode: string,
): Promise<Scene[]> {
  try {
    const resp = await request<SceneSkillListResponse>(
      `/wealth/scene-skills?category=${encodeURIComponent(categoryCode)}`,
    );
    return resp.items.map(mapScene);
  } catch (error) {
    console.warn("[Wealth] 场景接口不可用，返回空列表", error);
    return [];
  }
}

/**
 * 看板「可用能力」统计：全部大类的场景技能总数。
 * 与 fetchScenesByCategory 不同，接口不可达时返回 null，
 * 由页面保持 "--" 占位，避免把查询失败误显为 0。
 */
export async function fetchAvailableSceneCount(): Promise<number | null> {
  try {
    const resp = await request<SceneSkillListResponse>(
      "/wealth/scene-skills?category=",
    );
    return resp.items.length;
  } catch (error) {
    console.warn("[Wealth] 可用场景总数查询失败", error);
    return null;
  }
}

// ---------------------------------------------------------------------------
// 规划统计：同源直连，由 Nginx 转发到外部服务
// ---------------------------------------------------------------------------

interface SkillStatItemView {
  planId: string;
  targetCustomerCount: number;
  targetTaskCount: number;
}

interface SkillStatsResponse {
  code: number | string;
  data?: { items?: SkillStatItemView[] };
}

/**
 * 按规划 ID 批量查询，返回以 planId 为键的结果表。
 * 不经过 CoPaw 后端；请求失败返回 null，由看板保持 "--" 占位。
 */
export async function fetchSkillStats(
  planIds: string[],
  signal?: AbortSignal,
): Promise<Record<string, SkillStat> | null> {
  if (!planIds.length) return {};
  try {
    const auth = buildAuthHeaders();
    const resp = await fetch("/api/agent/workspace/skill-stats", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: getWPlusCookie("token") ?? "",
      },
      body: JSON.stringify({ planIds: [...new Set(planIds)] }),
      signal,
    });
    if (!resp.ok) throw new Error(`规划统计请求失败：HTTP ${resp.status}`);
    const payload = (await resp.json()) as SkillStatsResponse;
    if (String(payload.code) !== "200") {
      throw new Error(`规划统计接口返回失败：${payload.code}`);
    }
    const byKey: Record<string, SkillStat> = {};
    for (const item of payload.data?.items ?? []) {
      byKey[item.planId] = {
        targetCustomerCount: item.targetCustomerCount,
        generatedTaskCount: item.targetTaskCount,
      };
    }
    return byKey;
  } catch (error) {
    console.warn("[Wealth] 规划统计接口不可用", error);
    return null;
  }
}

// ---------------------------------------------------------------------------
// 客户名单查询：同源 /api/agent/workspace/name-list，由前端 Nginx 转发
// ---------------------------------------------------------------------------

interface NameListSkillView {
  skillId: string;
  skillName: string;
}

interface NameListField {
  filedName: string;
  filedNameCn: string;
}

interface NameListFieldValue extends NameListField {
  filedValue?: string | number | boolean | null;
}

interface NameListSkillFields {
  skillId: string;
  groupField: string[];
  fields: NameListField[];
}

interface NameListItemView {
  custUid: string;
  custNm: string;
  sapId?: string | null;
  bbkOrgId?: string | null;
  filename?: string | null;
  recomReason?: string | null;
  skillList?: NameListSkillView[];
  strongContactTime?: string | null;
  touchMethod?: string | null;
  touched?: number | null;
  fieldList?: NameListFieldValue[];
}

export interface CustomerCounts {
  targetCusCount: number | null;
  todayTouchCount: number | null;
  sevenDayTouchCount: number | null;
  unTouchedCount: number | null;
}

export interface CustomerListResult {
  customers: Customer[];
  counts: CustomerCounts;
}

function customerCounts(response?: Partial<CustomerCounts>): CustomerCounts {
  return {
    targetCusCount: response?.targetCusCount ?? null,
    todayTouchCount: response?.todayTouchCount ?? null,
    sevenDayTouchCount: response?.sevenDayTouchCount ?? null,
    unTouchedCount: response?.unTouchedCount ?? null,
  };
}

const EMPTY_CUSTOMER_COUNTS: CustomerCounts = {
  targetCusCount: 0,
  todayTouchCount: 0,
  sevenDayTouchCount: 0,
  unTouchedCount: 0,
};

interface NameListResponse extends Partial<CustomerCounts> {
  items: NameListItemView[];
  skillFieldList?: NameListSkillFields[];
  allFields?: NameListField[];
}

interface ExternalNameListResponse {
  code: string | number;
  data?: Partial<CustomerCounts> & {
    list?: NameListItemView[];
    skillFieldList?: NameListSkillFields[];
    allFields?: NameListField[];
  };
}

/**
 * 客户名单直接通过同源 Nginx 查询，使用 fetch 默认的同源凭证策略。
 * 经营视角传当前场景，其余口径传当天任务树全部技能；空范围不查询。
 */
export async function fetchNameList(
  skillIds: string[],
  sapId?: string,
  touched?: number,
): Promise<NameListResponse> {
  const ids = [...new Set(skillIds.map((id) => id.trim()).filter(Boolean))];
  if (!ids.length) return { items: [] };
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 60000);
  try {
    const context = getIframeContext();
    const resp = await fetch("/api/agent/workspace/name-list", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        skillIds: ids,
        bbkId: context.bbk || DEFAULT_BBK_ID,
        platformSource: "WP",
        pageSource: "WP_AGENT_WORKSPACE_TASK_LIST",
        ...(touched !== undefined ? { touched } : {}),
        ...(sapId ? { sapId } : {}),
        ...(context.orgCode ? { subBbkId: context.orgCode } : {}),
        ...(context.positionId ? { posId: context.positionId } : {}),
      }),
      signal: controller.signal,
    });
    if (!resp.ok) throw new Error(`客户名单请求失败：HTTP ${resp.status}`);
    const payload = (await resp.json()) as ExternalNameListResponse;
    if (String(payload.code) !== "200") {
      throw new Error(`客户名单接口返回失败：${payload.code}`);
    }
    const data = payload.data;
    return {
      ...customerCounts(data),
      items: (data?.list ?? []).filter((item) => Boolean(item?.custUid)),
      skillFieldList: data?.skillFieldList ?? [],
      allFields: data?.allFields ?? [],
    };
  } catch (error) {
    console.warn("[Wealth] 客户名单接口不可用，返回空列表", error);
    return { items: [] };
  } finally {
    clearTimeout(timeout);
  }
}

/** 配置负责顺序和名称，客户明细决定实际展示项；不生成无关字段占位。 */
function customerFields(
  item: NameListItemView,
  response: NameListResponse,
  skillId?: string,
): Pick<Customer, "dynamicFields" | "groupFields"> {
  const values = new Map(
    (item.fieldList ?? []).map((field) => [field.filedName, field]),
  );
  const labels = new Map(
    (response.allFields ?? []).map((field) => [
      field.filedName,
      field.filedNameCn,
    ]),
  );
  const skill = response.skillFieldList?.find((s) => s.skillId === skillId);
  const fields = skill?.fields ?? response.allFields ?? [];
  const order = [...new Set(fields.map((field) => field.filedName))];
  // 旧接口没有字段配置时仍可展示客户自己的指标。
  if (!fields.length) order.push(...values.keys());
  const toField = (name: string) => {
    const field = values.get(name);
    return {
      name,
      label:
        labels.get(name) ||
        field?.filedNameCn ||
        fields.find((f) => f.filedName === name)?.filedNameCn ||
        name,
      value:
        field?.filedValue == null || field.filedValue === ""
          ? "未提供"
          : String(field.filedValue),
    };
  };
  return {
    dynamicFields: order.filter((name) => values.has(name)).map(toField),
    groupFields: (skill?.groupField ?? []).map(toField),
  };
}

/** 今日有排程的经营场景（客户名单查询的入参上下文） */
export interface TodayTaskRef {
  skillId: string;
  sceneName: string;
  category: string;
  /** 所属规划来源：我的关注 / 行长关注 / 分行关注 */
  source: string;
}

const CUSTOMER_LABEL_BY_PLAN_SOURCE: Record<string, string> = {
  行长关注: "行长指派",
  分行关注: "分行重点",
  我的关注: "我的关注",
};

/**
 * 拉取今日任务对应的客户经营清单。
 * 经营视角由调用方传入当前场景；客户视角传入当天任务树全部场景。
 * 两个口径都传技能 ID 数组和 sapId，客户视角直接使用外部聚合名单。
 */
export async function fetchTodayCustomers(
  tasks: TodayTaskRef[],
  sapId: string | undefined,
  view: "business" | "customer",
): Promise<CustomerListResult> {
  const uniqueTasks = [...new Map(tasks.map((t) => [t.skillId, t])).values()];
  if (view === "customer") {
    return fetchCustomerViewCustomers(uniqueTasks, sapId, {
      touched: TOUCHED_ALL,
    });
  }
  const groups = await Promise.all(
    uniqueTasks.map(async (t) => ({
      task: t,
      response: await fetchNameList([t.skillId], sapId, TOUCHED_ALL),
    })),
  );
  const seen = new Set<string>();
  const customers: Customer[] = [];
  for (const { task, response } of groups) {
    for (const item of response.items) {
      const id = `${task.skillId}|${item.custUid}`;
      if (seen.has(id)) {
        continue;
      }
      seen.add(id);
      const reason = item.recomReason ?? "";
      customers.push({
        id,
        custUid: item.custUid,
        skillId: task.skillId,
        name: item.custNm,
        label: "",
        reason,
        category: task.category,
        task: task.sceneName,
        touched: item.touched,
        done: item.touched === 1,
        channel: item.touchMethod ?? "",
        time: item.strongContactTime ?? "",
        note: "",
        opportunities: reason ? [reason] : [],
        bbkOrgId: item.bbkOrgId ?? undefined,
        link: item.filename ?? undefined,
        ...customerFields(item, response, task.skillId),
      });
    }
  }
  return {
    customers,
    counts: groups.length
      ? customerCounts(groups[0].response)
      : { ...EMPTY_CUSTOMER_COUNTS },
  };
}

/** 触达状态过滤（外部接口 touched 字段）：0 未触达 / 1 已触达 / 2 全部 */
const TOUCHED_PENDING = 0;
const TOUCHED_DONE = 1;
const TOUCHED_ALL = 2;

/** 待触达客户名单：当天技能数组 + sapId + touched=0。 */
export async function fetchPendingCustomers(
  tasks: TodayTaskRef[],
  sapId?: string,
): Promise<CustomerListResult> {
  return fetchCustomerViewCustomers(tasks, sapId, {
    touched: TOUCHED_PENDING,
  });
}

/** 已完成客户名单：当天技能数组 + sapId + touched=1。 */
export async function fetchDoneCustomers(
  tasks: TodayTaskRef[],
  sapId?: string,
): Promise<CustomerListResult> {
  return fetchCustomerViewCustomers(tasks, sapId, {
    touched: TOUCHED_DONE,
    done: true,
  });
}

/**
 * 客户视角名单：按当天技能数组一次查询，映射外部聚合的 data.list。
 * 重点标签列按命中技能所属规划的创建角色展示；不在今日任务树中的
 * 技能无法关联本地规划来源，因此不产生标签。
 */
async function fetchCustomerViewCustomers(
  tasks: TodayTaskRef[],
  sapId?: string,
  opts: { touched?: number; done?: boolean } = {},
): Promise<CustomerListResult> {
  const sceneBySkill = new Map(tasks.map((t) => [t.skillId, t]));
  const response = await fetchNameList(
    tasks.map((task) => task.skillId),
    sapId,
    opts.touched,
  );
  const customers = response.items.map((item) => {
    const skillList = item.skillList ?? [];
    const skillIds = [...new Set(skillList.map((s) => s.skillId))];
    const skillNames = [
      ...new Set(skillList.map((s) => s.skillName).filter(Boolean)),
    ];
    const scenes = skillIds
      .map((sid) => sceneBySkill.get(sid))
      .filter((t): t is TodayTaskRef => Boolean(t));
    const labels = [
      ...new Set(
        scenes
          .map((scene) => CUSTOMER_LABEL_BY_PLAN_SOURCE[scene.source])
          .filter(Boolean),
      ),
    ];
    const reason = item.recomReason ?? "";
    return {
      id: item.custUid,
      custUid: item.custUid,
      skillId: scenes[0]?.skillId ?? skillIds[0] ?? "",
      name: item.custNm,
      label: labels.join("、"),
      skillNames,
      reason,
      category: scenes[0]?.category ?? "",
      task: opts.done
        ? skillNames.join("、")
        : scenes.map((t) => t.sceneName).join("、"),
      touched: item.touched,
      done: item.touched == null ? opts.done ?? false : item.touched === 1,
      channel: item.touchMethod ?? "",
      time: item.strongContactTime ?? "",
      note: "",
      bbkOrgId: item.bbkOrgId ?? undefined,
      opportunities: reason ? [reason] : [],
      link: item.filename ?? undefined,
      ...customerFields(item, response),
    };
  });
  return {
    customers,
    counts: tasks.length
      ? customerCounts(response)
      : { ...EMPTY_CUSTOMER_COUNTS },
  };
}

// ---------------------------------------------------------------------------
// 电访 / 客户洞察外链（get-sign 签名 + base64 拼接）
// ---------------------------------------------------------------------------

interface GetSignResponse {
  code?: string;
  message?: string;
  data?: string;
  success?: boolean;
}

function isDevEnv(): boolean {
  const href = typeof window !== "undefined" ? window.location.href : "";
  return (
    href.includes(".xxx.") ||
    href.includes("localhost") ||
    href.includes("127.0.0.1")
  );
}

function signBaseUrl(): string {
  return isDevEnv() ? "https://xxx.st.xxxx.cn" : "https://xxx.as.xxxx.cn";
}

function insightBaseUrl(): string {
  return isDevEnv() ? "https://xxx.st.xxxx.cn/#" : "https://xxx.oa.xxxx.cn/#";
}

function telBaseUrl(): string {
  return isDevEnv() ? "https://xxx.st.xxxx.cn" : "https://xxx.oa.xxxx.cn";
}

/**
 * 调用 /expert/get-sign 获取签名校验串。
 * header 通过自定义头 x-header-cookie 透传登录态 cookie（取 buildAuthHeaders 的
 * x-header-cookie），入参为 bbkOrgId 与 custUid；出参 data 字段即为 signature。
 *
 * 注意：不能直接设置标准 Cookie 头——Cookie 属于浏览器禁止 JS 修改的
 * forbidden header name，跨域请求中会被剥离。项目后端惯例是从
 * `Cookie` 或 `x-header-cookie` 中读取，故这里用自定义头透传。
 */
export async function fetchSchemeSignature(
  custUid: string,
  bbkOrgId: string,
): Promise<string> {
  const auth = buildAuthHeaders();
  console.log("auth", auth);
  const resp = await fetch(`/expert/get-sign`, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      Authorization: getWPlusCookie("token") ?? "",
      "x-header-cookie": auth["x-header-cookie"] ?? "",
      Cookie: auth["x-header-cookie"] ?? "",
    },
    body: JSON.stringify({ bbkOrgId, custUid }),
  });
  if (!resp.ok) {
    throw new Error(`get-sign 接口请求失败：HTTP ${resp.status}`);
  }
  const json = (await resp.json()) as GetSignResponse;
  if (!json.success) {
    throw new Error(json.message || "get-sign 接口返回失败");
  }
  return json.data || "";
}

/**
 * 构建电访外链：`{tel域名}?custUid=..&bbkOrgId=..&signature=../wpcontactpanelv2/`
 * query 值经 URL 编码，结构便于对端按 custUid/bbkOrgId 解析。
 */
export function buildTelUrl(
  custUid: string,
  bbkOrgId: string,
  signature: string,
): string {
  const params = new URLSearchParams({ custUid, bbkOrgId, signature });
  return `${telBaseUrl()}?${params.toString()}#/wpcontactpanelv2`;
}

/**
 * 构建客户洞察外链：query 串（key/value 均 URL 编码）整体 base64 后拼接到 `/homepage/`。
 * base64 采用标准 Base64，与对端 atob 解码/示例格式一致。
 */
export function buildInsightUrl(
  custUid: string,
  bbkOrgId: string,
  signature: string,
): string {
  const parts = [
    encodeURIComponent("custUid") + "=" + encodeURIComponent(custUid),
    encodeURIComponent("bbkOrgId") + "=" + encodeURIComponent(bbkOrgId),
    encodeURIComponent("signature") + "=" + encodeURIComponent(signature),
  ];
  const queryString = parts.join("&");
  // const base64Str = Base64.encode(queryString);
  return `${insightBaseUrl()}/homepage/${queryString}`;
}

// ---------------------------------------------------------------------------
// 规划接口：/wealth/plans
// ---------------------------------------------------------------------------

interface PlanScenePayload {
  scene_id: string;
  scene_name: string;
  category: string;
  item_id?: string | null;
  direction?: string;
  cycle?: string;
  start_date?: string;
  end_date?: string;
  cron_expr: string;
  cron_example?: string | null;
  mcp_relations: string[];
}

interface PlanUpsertPayload {
  name: string;
  description?: string;
  source_label?: string;
  scenes: PlanScenePayload[];
  targets: { sap_id: string; name?: string }[];
}

function buildScenePayload(item: PlanItem): PlanScenePayload {
  return {
    scene_id: item.id,
    scene_name: item.sceneName,
    category: item.categoryCode,
    item_id: item.itemId ?? null,
    cron_example: item.cronExample ?? null,
    direction: item.direction,
    cycle: item.cycle,
    start_date: item.start,
    end_date: item.end,
    cron_expr: serializeCron(item.schedule ?? DEFAULT_SCHEDULE),
    mcp_relations: item.mcpRelations,
  };
}

async function fetchPlans(signal?: AbortSignal): Promise<Plan[]> {
  const resp = await request<PlanListResponse>(
    "/wealth/plans",
    signal ? { signal } : undefined,
  );
  return resp.items.map(mapPlan);
}

/** 轮询场景的接口告警只打一次，恢复后重置 */
let planListErrorWarned = false;

/** 默认容错读取返回空；严格刷新上抛错误，由调用方保留已有规划并提示。 */
export async function fetchPlanList(
  throwOnError = false,
  signal?: AbortSignal,
): Promise<Plan[]> {
  try {
    const plans = await fetchPlans(signal);
    planListErrorWarned = false;
    return plans;
  } catch (error) {
    if (!planListErrorWarned) {
      console.warn("[Wealth] 规划接口不可用", error);
      planListErrorWarned = true;
    }
    if (throwOnError) throw error;
    return [];
  }
}

// ---------------------------------------------------------------------------
// 内存实现（会话级草稿，刷新即复原；规划、场景与客户名单已全程走真实接口）
// ---------------------------------------------------------------------------

interface DraftEntry {
  draft: Draft;
  at: string;
}

interface WealthDb {
  /** 按账户隔离的会话级草稿 */
  drafts: Record<string, DraftEntry>;
}

let db: WealthDb = createInitialDb();

function createInitialDb(): WealthDb {
  return {
    drafts: {},
  };
}

/** 测试辅助：重置内存后端到初始状态（模拟刷新） */
export function resetMockDb(): void {
  db = createInitialDb();
}

export function newAccountDraft(): Draft {
  return { name: "", items: [] };
}

export interface BootstrapData {
  plans: Plan[];
  draft: Draft;
  savedAt: string;
}

/** 拉取账户视角下的工作台基础数据（规划走真实接口；客户名单由各页面单独加载） */
export async function fetchBootstrap(
  accountId: string,
): Promise<BootstrapData> {
  const cached = db.drafts[accountId];
  let plans: Plan[];
  try {
    plans = await fetchPlans();
  } catch (error) {
    console.warn("[Wealth] 规划接口不可用，返回空列表", error);
    plans = [];
  }
  await sleep(MOCK_LATENCY_MS);
  return {
    plans,
    draft: cached ? clone(cached.draft) : newAccountDraft(),
    savedAt: cached?.at ?? "",
  };
}

/** 保存草稿：仅记录保存时间并写内存表（草稿不进后端，见 CONTEXT.md） */
export async function saveDraft(
  accountId: string,
  draft: Draft,
): Promise<{ savedAt: string }> {
  await sleep(MOCK_LATENCY_MS);
  const savedAt = new Date().toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
  db.drafts[accountId] = { draft: clone(draft), at: savedAt };
  return { savedAt };
}

export interface PublishResult {
  plans: Plan[];
  created: boolean;
}

/** 发布新规划或保存对既有规划的修改，返回最新规划列表；任何失败均上抛，由调用方提示 */
export async function publishPlan(
  account: Account,
  draft: Draft,
  editingId: string | null,
  targets: DistributeTarget[],
): Promise<PublishResult> {
  const payload: PlanUpsertPayload = {
    name: draft.name.trim(),
    source_label: account.source,
    scenes: draft.items.map(buildScenePayload),
    targets: targets.map((t) => ({ sap_id: t.sapId, name: t.name })),
  };
  if (editingId != null) {
    await request<PlanCreateResponse>(
      `/wealth/plans/${encodeURIComponent(editingId)}`,
      { method: "PUT", body: JSON.stringify(payload) },
    );
  } else {
    await request<PlanCreateResponse>("/wealth/plans", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }
  return { plans: await fetchPlans(), created: editingId == null };
}

/** 移除规划，返回最新规划列表；任何失败均上抛 */
export async function removePlan(id: string): Promise<{ plans: Plan[] }> {
  await request<{ deleted: boolean }>(
    `/wealth/plans/${encodeURIComponent(id)}`,
    { method: "DELETE" },
  );
  return { plans: await fetchPlans() };
}
