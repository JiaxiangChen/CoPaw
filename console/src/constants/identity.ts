/**
 * ============================================================
 * 身份相关常量配置
 * Author: Kun He
 * Date: 2026-04-07
 * ============================================================
 *
 * 统一的身份标识默认值配置
 * 修改默认值只需改动此文件一处
 *
 * 相关文件：
 * - utils/identity.ts: getUserId/getChannel helper
 * - api/authHeaders.ts: headers 构建
 * - pages/Chat/sessionApi/index.ts: session 管理
 * ============================================================
 */

/** 默认用户 ID（非 iframe 模式时使用） */
export const DEFAULT_USER_ID = "80280195";

/** 默认租户 ID（与 X-Tenant-Id header 对应） */
export const DEFAULT_TENANT_ID = "80280195";

/** 默认渠道名称 */
export const DEFAULT_CHANNEL = "console";

/** 默认 Source ID（非 iframe 模式时作为 X-Source-Id 头发送），默认值用来区分各个渠道，不能修改 */
export const DEFAULT_SOURCE_ID = "DEFAULT-SOURCE";

/** 默认 BBK ID （非iframe模式时，X-Bbk-Id header使用，空表示不传） */
export const DEFAULT_BBK_ID = "";

/** 默认用户名（非 iframe模式时，X-Username header使用，空表示不传） */
export const DEFAULT_USER_NAME = "";
