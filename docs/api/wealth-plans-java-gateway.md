# 财富工作台 Java 接口迁移评估与接入

日期：2026-10-19。依据用户提供的 `wealth_plans_api.md` V1.0；忽略 Controller 类名和组织方式，以实际路径与 JSON 契约接入。

## 评估结论

方案可行，前端改造可限定在工作台数据适配层。当前场景、名单、统计、机构人员和签名请求已通过同源网关，本次将剩余规划读写切换为同源 Java 接口。页面布局、权限映射、草稿、统计缓存和刷新策略继续沿用。

按用户确认，规划创建后的定时任务、分发、资源处理和状态回写由新服务完整负责；前端不接入 market 分发腿，也不继续调用 Python 发布编排。原方案中 market 等 TODO 不作为本次前端切换的前置项。

Java 实现及实际网关部署不在本仓库，当前验证为前端契约测试和构建；不能将测试通过等同于新服务已部署或后台执行链路已在线验收。

## 规划接口适配

浏览器路径固定为同源 `/zt/wealth/plans`，不经过全局 `request()`，不拼接 `/api`，也不受 `window.__env__.baseUrl` 或 `VITE_API_BASE_URL` 影响。网关上游 URI 如有差异，由网关重写，不依赖 Java Controller 命名。

| 方法/浏览器路径 | 身份位置 | 业务数据/返回 |
|---|---|---|
| GET `/zt/wealth/plans` | Query：sapId、role、bbkId | `data.items` → 页面规划列表 |
| POST `/zt/wealth/plans` | JSON：sapId、role、bbkId、creatorName、sourceId、agentId | JSON 携带规划、场景、目标；返回 data.id/publishStatus |
| PUT `/zt/wealth/plans/{id}` | 同 POST | 整体替换；返回 data.id/publishStatus |
| DELETE `/zt/wealth/plans/{id}` | JSON：sapId | `data=true` 才视为删除成功 |
| GET `/zt/wealth/plans/{id}` | 文档约定同列表 Query | Java 提供的详情接口；当前页面复用列表数据，没有新增无调用方的详情请求 |

所有请求显式发送 `Content-Type: application/json` 和 Cookie `token` 的原始值作为 Authorization，与当前名单/统计接入方式一致；浏览器使用默认同源凭证策略，不手工设置 Cookie 头。

身份取值：

- sapId：现有 `getUserId()` 身份优先级；保留前导零。
- role：每次请求使用 iframeStore.positionId 经现有 resolveRole 映射。
- bbkId、creatorName、sourceId：当前 iframe 上下文；source 缺省沿用 DEFAULT_SOURCE_ID。
- agentId：当前 useAgentStore.selectedAgent，缺省 default。
- sourceLabel：当前账户角色对应的“我的关注 / 行长关注 / 分行关注”。Java 按新契约接收此字段。

Java 必须将这些身份字段与可信登录态校验一致；前端传入 role、sapId、sourceLabel 不构成后端授权。方案中 sourceId 虽为可选，涉及隔离和后台任务时仍应保留。

## 字段及错误处理

Java 入参/返回使用驼峰：

| 原 Python 字段 | Java 字段 |
|---|---|
| source_label、period_start、period_end | sourceLabel、periodStart、periodEnd |
| publish_status、board_status | publishStatus、boardStatus |
| scene_id、scene_name、category_label | sceneId、sceneName、categoryLabel |
| item_id、cron_expr、cron_example | itemId、cronExpr、cronExample |
| start_date、end_date、mcp_relations | startDate、endDate、mcpRelations |
| targets[].sap_id | targets[].sapId |

页面内部继续使用现有 Plan/PlanItem：场景 ID、排程、目标姓名、发布状态、分发状态和 editable 在适配层映射，组件不感知 Java 包装。

所有规划接口检查 HTTP 状态以及 `String(code)==="200"`，然后读取 data。HTTP 200 下的 400/403/404/409 业务码也抛错，错误保留 message/status/data；写入失败不继续查询列表、不提示成功。网关非 JSON 返回、成功缺 data、删除 data=false 都不会当作成功。

规划列表继续区分默认容错读取和严格刷新；严格失败保留已有数据。AbortSignal 传给 fetch，读取响应后再次检查取消，避免旧响应覆盖当前状态。不自动重试创建/更新/删除，避免重复发布。

## 工作台其他请求路由

这些接口已经直连同源网关，本次保持现有调用契约：

| 方法 | 浏览器路径 | 目标能力 |
|---|---|---|
| POST | `/api/agent/workspace/skill-config/list` | 场景池，bbkId/category |
| POST | `/api/agent/workspace/skill-stats` | 规划统计，planIds |
| POST | `/api/agent/workspace/name-list` | 名单/汇总，skillIds/sapId/touched 等 |
| POST | `/xcapi-auth/wmadm/services/v1/wmadm/combox/BranchTreeInfoService` | 机构树 |
| POST | `/xcapi-auth/wmadm/services/v1/wmutadm/getUserPostionByBrn` | 分发人员 |
| POST | `/expert/get-sign` | 客户操作签名 |

不向 Python `/api/wealth/*` 或 `/api/user-info/*` 回退。旧 Python handler 保留供已有调用方兼容，本次没有删除共享后端代码或切换全局 API base。

## 网关部署要求

以下为 Nginx 配置示意，`wealth_plan_java` 是需由部署方定义的上游，不是当前已部署地址：

```nginx
location = /zt/wealth/plans {
    proxy_pass http://wealth_plan_java/wealth/plans;
    proxy_set_header Authorization $http_authorization;
    proxy_pass_request_body on;
    proxy_intercept_errors off;
    proxy_read_timeout 60s;
    add_header Cache-Control "no-store" always;
}

location ^~ /zt/wealth/plans/ {
    proxy_pass http://wealth_plan_java/wealth/plans/;
    proxy_set_header Authorization $http_authorization;
    proxy_pass_request_body on;
    proxy_intercept_errors off;
    proxy_read_timeout 60s;
    add_header Cache-Control "no-store" always;
}
```

- 精确匹配列表和子路径，优先于 SPA 的 try_files，不能将请求返回 index.html。
- 上述示例按 `/zt` 仅为网关前缀处理：浏览器 `/zt/wealth/plans` 转给 Java `/wealth/plans`，子路径和 Query 保留。如果 Java 本身也已改为 `/zt/wealth/plans`，两处 proxy_pass 应改为不带 URI 的 `http://wealth_plan_java`，原样转发。
- 保留 Query、POST/PUT/DELETE 方法及 JSON Body，尤其不能剥离 DELETE 的 sapId。
- 保留默认 Cookie/Authorization 转发；服务凭证由网关注入，不放浏览器代码。
- 规划 GET 带用户身份，不应跨用户缓存；后端仍需按用户/分行/来源隔离。
- 开发时也需使用配置了这些转发规则的前端网关；仅启动当前 Vite 不会自动获得 Java 上游。新服务地址未提供，本次未写死开发代理或修改线上配置。
- 存量规划迁移、数据表写入所有权及新 Java 服务部署由后端侧完成；不要同时让旧、新发布编排消费同一个请求。

## 验收

新增 `console/src/pages/WealthWorkbench/planGateway.test.ts` 验证真实 fetch 边界：同源 URL、Query/Body 身份、camelCase 映射、前导零、岗位切换、HTTP 200 业务失败、网关失败、DELETE、取消和失败不刷新。现有工作台 Store、编辑人员回显和看板取消测试改为 Java 包装夹具。

部署后还需核对：浏览器 Network 请求是否命中新服务；三类岗位的数据可见性；创建/修改后的真实后台结果与状态；删除级联结果；网关是否透传 DELETE Body。此类线上验收需要实际网关与 Java 服务，不能由本地 mock 代替。

本次本地验证：

- 新增网关契约 13 项通过；规划相关 5 个测试文件共 68 项通过。
- 完整工作台测试 303 项通过，3 项失败与改造前基线一致：名单旧 45 秒超时预期、名单旧请求头预期、统计旧 credentials/Cookie 预期。本次未改变这些接口行为。
- TypeScript 检查和改动文件 Prettier 检查通过。生产构建在 Node 堆上限提高到 6144MB 后通过（默认堆首次耗尽）。
- 改动范围 ESLint 仅报原先已有的 `api.ts::signBaseUrl` 未使用；未顺带删除既有无关函数。
- GitNexus 变更检查为 LOW；本次只调整工作台适配与测试，没有改动通用请求层、Python 发布编排或线上网关。
