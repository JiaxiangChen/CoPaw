# 财富工作台 Java 接口迁移评估与接入

日期：2026-10-19。依据用户提供的 `wealth_plans_api.md` V1.0；忽略 Controller 类名和组织方式，以实际路径与 JSON 契约接入。

## 评估结论

方案可行，前端改造可限定在工作台数据适配层。规划、场景、名单和统计使用统一 request 接入 Java/智拓网关；机构人员和支行树也通过统一 request，签名仍保持原生 fetch。页面布局、权限映射、草稿、统计缓存和刷新策略继续沿用。

按用户确认，规划创建后的定时任务、分发、资源处理和状态回写由新服务完整负责；前端不接入 market 分发腿，也不继续调用 Python 发布编排。原方案中 market 等 TODO 不作为本次前端切换的前置项。

Java 实现及实际网关部署不在本仓库，当前验证为前端契约测试和构建；不能将测试通过等同于新服务已部署或后台执行链路已在线验收。

## 规划接口适配

工作台通过 `api/request.ts` 的 `request()` 调用 `/zt/wealth/plans`，由统一层拼接 API base 和 `/api` 前缀，最终请求 `{API base}/api/zt/wealth/plans`；鉴权、响应解析、错误处理及 401 重试均复用统一层。网关上游 URI 如有差异，由网关重写，不依赖 Java Controller 命名。

| 方法/浏览器路径 | 身份位置 | 业务数据/返回 |
|---|---|---|
| GET `/api/zt/wealth/plans` | Query：sapId、role、bbkId | `data.items` → 页面规划列表 |
| POST `/api/zt/wealth/plans` | JSON：sapId、role、bbkId、creatorName、sourceId、agentId | JSON 携带规划、场景、目标；返回 data.id/publishStatus |
| PUT `/api/zt/wealth/plans/{id}` | 同 POST | 整体替换；返回 data.id/publishStatus |
| DELETE `/api/zt/wealth/plans/{id}` | JSON：sapId | `data=true` 才视为删除成功 |
| GET `/api/zt/wealth/plans/{id}` | 文档约定同列表 Query | Java 提供的详情接口；当前页面复用列表数据，没有新增无调用方的详情请求 |

规划、场景、名单和统计请求共用 `request()`：有 JSON 请求体时由统一层补 Content-Type，鉴权及用户/租户/来源等请求头由 `buildAuthHeaders()` 构建，并复用 401 token 刷新处理。不再在这些调用点读取 Cookie token 设置 Authorization。超时、取消信号、业务码解包和页面读失败降级行为保留。

身份取值：

- sapId：现有 `getUserId()` 身份优先级；保留前导零。
- role：每次请求使用 iframeStore.positionId 经现有 resolveRole 映射。
- bbkId、creatorName、sourceId：当前 iframe 上下文；source 缺省沿用 DEFAULT_SOURCE_ID。
- agentId：当前 useAgentStore.selectedAgent，缺省 default。
- sourceLabel：当前账户角色对应的“我的关注 / 行长关注 / 分行关注”。Java 按新契约接收此字段。

Java 必须将这些身份字段与可信登录态校验一致；前端传入 role、sapId、sourceLabel 不构成后端授权。方案中 sourceId 虽为可选，涉及隔离和后台任务时仍应保留。

### 运维模式的前端请求差异（2026-10-30）

本次仅前端实施，Java 支持与授权由服务端同事确认。普通模式仍沿用上面的取值及请求体。真实分行 `V00001`、用户 `80008888` 在头部可见时可开启运维模式，选项读取 `BBK_ID_MAP` 并排除总行 `100`；不新增服务接口。

入口名单从 `env-grey-control.js` 的 `operationsUsersByBranch` 读取，初始值为 `{ V00001: ["80008888"] }`。可追加字符串编号，修改部署文件并重新加载页面生效；缺失或非法配置不开放入口，无代码默认名单，不支持通配符。该配置仅影响前端入口，服务端白名单与授权仍需独立维护。

| 项目 | 运维模式选定分行后的取值 |
|---|---|
| 鉴权、Cookie 及通用上下文请求头 | 保留真实登录态，包括真实 X-Bbk-Id/X-Position-Id；不伪造目标分行登录 |
| GET `/zt/wealth/plans` Query | sapId=真实用户、role=middle、bbkId=目标分行 |
| POST/PUT 规划 JSON | sapId/creatorName=真实操作人；role=middle；bbkId/bbkOrgId=目标分行根；sourceLabel=分行关注；sourceId/agentId 保留原值 |
| DELETE 规划 JSON | `{sapId: "80008888", role: "middle", bbkId: "所选分行编码"}`；普通模式仍仅传 sapId |
| 场景池 JSON | bbkId=目标分行，category 沿用原值 |
| 机构树 JSON | 根 mmOrgCode=目标分行、mmOrgLvl=30、sysId 沿用真实上下文；选择下级后使用返回条目的编码与级别 |
| 人员 JSON | bbkOrgId=目标分行、mmOrgCode/mmOrgLvl=所选机构编码与级别 |
| 分发目标 | targets[].sapId/name/bbkId/bbkOrgId 沿用所选客户经理返回值 |
| 统计 | 沿用所选分行规划的 planIds，不增加身份覆盖字段 |

Java 必须通过可信登录态联合校验白名单、目标分行与 role，保留真实创建人并按目标分行存储、分发。返回的 `editable=true` 必须仅表示当前真实操作人可管理的本人代建规划，PUT/DELETE 再次校验归属和业务范围；他人规划即使可见也不能被修改或删除。前端对运维规划缺失的 editable 按只读处理。

运维模式业务上下文的 orgLvl 固定为 "30"，根机构和根人员查询的 mmOrgLvl 传数字 30；普通模式仍使用真实登录层级，下级机构仍使用接口返回的 mOrgLvl。机构接口对接方还需确认 bbkOrgId 使用目标分行编码符合其机构模型。未经外部服务确认及联调，不能将前端验证通过当作跨分行生产授权已完成。

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

场景、统计、名单、机构人员和支行树通过统一 request 调用下列路径（配置 API base 时带对应前缀）；仅 `/expert/get-sign` 按接入约定保留原生 fetch，不改变原有路径与鉴权方式：

| 方法 | 浏览器路径 | 目标能力 |
|---|---|---|
| POST | `/api/agent/workspace/skill-config/list` | 场景池，bbkId/category |
| POST | `/api/agent/workspace/skill-stats` | 规划统计，planIds |
| POST | `/api/agent/workspace/name-list` | 名单/汇总，skillIds/sapId/touched 等 |
| POST | `/api/zt/wealth/wmadm/branchTreeInfo` | 机构树 |
| POST | `/api/zt/wealth/wmadm/getUserPostionByBrn` | 分发人员 |
| POST | `/expert/get-sign` | 客户操作签名 |

不向 Python `/api/wealth/*` 或 `/api/user-info/*` 回退。旧 Python handler 保留供已有调用方兼容，本次没有删除共享后端代码或切换全局 API base。

## 网关部署要求

以下为 Nginx 配置示意，`wealth_plan_java` 是需由部署方定义的上游，不是当前已部署地址：

```nginx
location = /api/zt/wealth/plans {
    proxy_pass http://wealth_plan_java/wealth/plans;
    proxy_set_header Authorization $http_authorization;
    proxy_pass_request_body on;
    proxy_intercept_errors off;
    proxy_read_timeout 60s;
    add_header Cache-Control "no-store" always;
}

location ^~ /api/zt/wealth/plans/ {
    proxy_pass http://wealth_plan_java/wealth/plans/;
    proxy_set_header Authorization $http_authorization;
    proxy_pass_request_body on;
    proxy_intercept_errors off;
    proxy_read_timeout 60s;
    add_header Cache-Control "no-store" always;
}
```

- 精确匹配列表和子路径，优先于 SPA 的 try_files，不能将请求返回 index.html。
- 上述示例按 `/api/zt` 仅为网关前缀处理：浏览器 `/api/zt/wealth/plans` 转给 Java `/wealth/plans`，子路径和 Query 保留。如果 Java 本身使用 `/zt/wealth/plans`，两处 proxy_pass 应分别改为 `http://wealth_plan_java/zt/wealth/plans` 和 `http://wealth_plan_java/zt/wealth/plans/`，保留正确的上游路径。
- 保留 Query、POST/PUT/DELETE 方法及 JSON Body，尤其不能剥离 DELETE 的 sapId。
- 保留默认 Cookie/Authorization 转发；服务凭证由网关注入，不放浏览器代码。
- 规划 GET 带用户身份，不应跨用户缓存；后端仍需按用户/分行/来源隔离。
- 开发时也需使用配置了这些转发规则的前端网关；仅启动当前 Vite 不会自动获得 Java 上游。新服务地址未提供，本次未写死开发代理或修改线上配置。
- 存量规划迁移、数据表写入所有权及新 Java 服务部署由后端侧完成；不要同时让旧、新发布编排消费同一个请求。

## 验收

新增 `console/src/pages/WealthWorkbench/planGateway.test.ts` 验证统一 request 的真实 fetch 边界：API URL、Query/Body 身份、camelCase 映射、前导零、岗位切换、HTTP 200 业务失败、网关失败、DELETE、取消和失败不刷新。现有工作台 Store、编辑人员回显和看板取消测试改为 Java 包装夹具。

部署后还需核对：浏览器 Network 请求是否命中新服务；三类岗位的数据可见性；创建/修改后的真实后台结果与状态；删除级联结果；网关是否透传 DELETE Body。此类线上验收需要实际网关与 Java 服务，不能由本地 mock 代替。

本次本地验证：

- 新增网关契约 13 项通过；规划相关 5 个测试文件共 68 项通过。
- 完整工作台测试 303 项通过，3 项失败与改造前基线一致：名单旧 45 秒超时预期、名单旧请求头预期、统计旧 credentials/Cookie 预期。本次未改变这些接口行为。
- TypeScript 检查和改动文件 Prettier 检查通过。生产构建在 Node 堆上限提高到 6144MB 后通过（默认堆首次耗尽）。
- 改动范围 ESLint 仅报原先已有的 `api.ts::signBaseUrl` 未使用；未顺带删除既有无关函数。
- GitNexus 变更检查为 LOW；本次只调整工作台适配与测试，没有改动通用请求层、Python 发布编排或线上网关。


## 督办策略字段接入（2026-10-28）

创建与编辑继续使用原接口。请求顶层增加 `bbkOrgId`，取登录人的 `orgCode`；原 `bbkId` 保持现有登录上下文取值。

`targets[]` 每项增加 `bbkId` 和 `bbkOrgId`：分别取接收人的人员接口 `branchID`、`subBranchID`，跨机构接收人各自取值，不能复用创建人机构。客户经理分发给本人时使用本人的登录上下文。规划查询返回的 `targets[]` 也需包含这两个同名字段，以便编辑后重新发布保留原接收人机构编码；均按字符串传递，保留前导零。

仅 `role=president` 时，`scenes[]` 增加 `schedule`（1当日、2 T+7日内）、`monitorTimeCron`（与执行频率一致的五段cron）和 `finishRate`（整数百分比，80代表80%）。新场景默认值分别为1、`0 9 * * *`、60。客户经理和分行中台的场景项不发送这三个字段。

`GET /zt/wealth/plans` 的 `scenes[]` 需返回同名字段，前端据此回填编辑页和行长工作任务详情。未返回字段的存量规划，在详情中显示未设置策略；编辑时使用默认值。取消编辑不会写入，提交成功后以服务端查询结果为准。

督办配置的localStorage逻辑已移除，旧本地缓存不再读取或自动上传。Java必须依据可信登录态校验行长权限，负责落库和督办执行；本仓库前端不新增独立督办保存接口，也不修改旧Python发布流程。
