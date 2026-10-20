// TODO: 合并代码时需要修改的地方
export const COOKIE_KEYS = {
  userId: "",
  sysId: "",
  bbk: "",
  orgCode: "",
  orgLvl: "",
  token: "",
  positionId: "",
} as const;
// ── LocalStorage Keys ─────────────────────────────────────────────────────
export const STORAGE_KEYS = {
  authToken: "copaw_auth_token",
} as const;

// ── URLs ──────────────────────────────────────────────────────────────────

export const PYPI_URL = "https://pypi.org/pypi/copaw/json";

export const GITHUB_URL = "https://github.com/agentscope-ai/CoPaw" as const;

// ── Timing ────────────────────────────────────────────────────────────────

export const ONE_HOUR_MS = 60 * 60 * 1000;

// ── Navigation ────────────────────────────────────────────────────────────

export const DEFAULT_OPEN_KEYS = [
  "creation-center",
  "run-center",
  "system-settings",
  "insight-center",
  "quality-engineering",
];

export const KEY_TO_PATH: Record<string, string> = {
  chat: "/chat",
  channels: "/channels",
  sessions: "/sessions",
  "cron-jobs": "/cron-jobs",
  "scheduler-cron-jobs": "/scheduler-cron-jobs",
  heartbeat: "/heartbeat",
  "cases-management": "/cases-management",
  "greeting-management": "/greeting-management",
  "featured-cases-management": "/featured-cases-management",
  "scenario-presets-management": "/scenario-presets-management",
  skills: "/skills",
  "skill-pool": "/skill-pool",
  tools: "/tools",
  mcp: "/mcp",
  workspace: "/workspace",
  agents: "/agents",
  models: "/models",
  environments: "/environments",
  "agent-config": "/agent-config",
  "hook-management": "/hook-management",
  "system-config-page": "/system-config-page",
  "system-check": "/system-check",
  security: "/security",
  "skill-config": "/skill-config",
  "token-usage": "/token-usage",
  "voice-transcription": "/voice-transcription",
  "analytics-users": "/analytics/users",
  "analytics-sessions": "/analytics/sessions",
  "analytics-messages": "/analytics/messages",
  "analytics-traces": "/analytics/traces",
  "analytics-business-overview": "/analytics/business-overview",
  "analytics-claw-data-overview": "/analytics/claw-data-overview",
  "analytics-cron-job-overview": "/analytics/cron-job-overview",
  "analytics-continuous-governance": "/analytics/continuous-governance",
  "monitor-cron-overview": "/monitor/cron-overview",
  "monitor-task-center": "/monitor/tasks",
  "monitor-cron-batch-dispatch": "/monitor/cron-batch-dispatch",
  "monitor-cron-batch-dispatch-new": "/monitor/cron-batch-dispatch-new",
  "instance-overview": "/instance/overview",
  "instance-instances": "/instance/instances",
  "instance-allocations": "/instance/allocations",
  "instance-operation-logs": "/instance/operation-logs",
  "continuous-iteration": "/continuous-iteration",
  market: "/market",
  "my-skills": "/my-skills",
  "my-mcp": "/my-mcp",
  experts: "/experts",
  "expert-community": "/expert-community",
};

export const KEY_TO_LABEL: Record<string, string> = {
  chat: "nav.chat",
  channels: "nav.channels",
  sessions: "nav.sessions",
  "cron-jobs": "nav.cronJobs",
  "scheduler-cron-jobs": "nav.cronJobsNew",
  heartbeat: "nav.heartbeat",
  skills: "nav.skills",
  "skill-pool": "nav.skillPool",
  tools: "nav.tools",
  mcp: "nav.mcp",
  "agent-config": "nav.agentConfig",
  "hook-management": "nav.hookManagement",
  "system-config-page": "nav.currentSourceConfig",
  "system-check": "nav.systemCheck",
  workspace: "nav.workspace",
  models: "nav.models",
  environments: "nav.environments",
  security: "nav.security",
  "skill-config": "nav.skillConfig",
  "token-usage": "nav.tokenUsage",
  agents: "nav.agents",
  "analytics-users": "nav.analyticsUsers",
  "analytics-sessions": "nav.analyticsSessions",
  "analytics-messages": "nav.analyticsMessages",
  "analytics-traces": "nav.analyticsTraces",
  "analytics-business-overview": "nav.analyticsBusinessOverview",
  "analytics-claw-data-overview": "nav.analyticsClawDataOverview",
  "analytics-cron-job-overview": "nav.analyticsCronJobOverview", //定时任务概览
  "analytics-continuous-governance": "nav.analyticsContinuousGovernance",
  "monitor-task-center": "nav.monitorTaskCenter",
  "monitor-cron-batch-dispatch": "nav.monitorCronBatchDispatch",
  "monitor-cron-batch-dispatch-new": "nav.monitorCronBatchDispatchNew",
  "instance-overview": "nav.instanceOverview",
  "instance-instances": "nav.instanceInstances",
  "instance-allocations": "nav.instanceAllocations",
  "instance-operation-logs": "nav.instanceOperationLogs",
  "continuous-iteration": "nav.continuousIteration",
  market: "nav.market",
  "my-skills": "nav.mySkills",
  "my-mcp": "nav.myMcp",
  "expert-community": "nav.expertCommunity",
};

// ── URL helpers ───────────────────────────────────────────────────────────

export const getWebsiteLang = (lang: string): string =>
  lang.startsWith("zh") ? "zh" : "en";

export const getDocsUrl = (lang: string): string =>
  `https://copaw.agentscope.io/docs/intro?lang=${getWebsiteLang(lang)}`;

export const getFaqUrl = (lang: string): string =>
  `https://copaw.agentscope.io/docs/faq?lang=${getWebsiteLang(lang)}`;

export const getReleaseNotesUrl = (lang: string): string =>
  `https://copaw.agentscope.io/release-notes?lang=${getWebsiteLang(lang)}`;

// ── Version helpers ────────────────────────────────────────────────────────

// Filter out pre-release versions; post-releases are treated as stable.
// PEP 440 pre-release suffixes: aN / bN / rcN (or cN) / devN.
export const isStableVersion = (v: string): boolean =>
  !/(\d)(a|alpha|b|beta|rc|c|dev)\d*/i.test(v);

// Compare two PEP 440 version strings. Returns >0 if a>b, <0 if a<b, 0 if equal.
// .postN releases sort after their base version (e.g. 1.0.0.post1 > 1.0.0).
export const compareVersions = (a: string, b: string): number => {
  const normalise = (v: string) =>
    v
      .replace(/\.post(\d+)/i, ".$1")
      .split(/[.-]/)
      .map((seg) => (isNaN(Number(seg)) ? 0 : Number(seg)));
  const aN = normalise(a);
  const bN = normalise(b);
  const len = Math.max(aN.length, bN.length);
  for (let i = 0; i < len; i++) {
    const diff = (aN[i] ?? 0) - (bN[i] ?? 0);
    if (diff !== 0) return diff;
  }
  return 0;
};

// ── Update markdown ───────────────────────────────────────────────────────

export const UPDATE_MD: Record<string, string> = {
  zh: `### CoPaw如何更新

要更新 CoPaw 到最新版本，可根据你的安装方式选择对应方法：

1. 如果你使用的是一键安装脚本，直接重新运行安装命令即可自动升级。

2. 如果你是通过 pip 安装，在终端中执行以下命令升级：

\`\`\`
pip install --upgrade copaw
\`\`\`

3. 如果你是从源码安装，进入项目目录并拉取最新代码后重新安装：

\`\`\`
cd CoPaw
git pull origin main
pip install -e .
\`\`\`

4. 如果你使用的是 Docker，拉取最新镜像并重启容器：

\`\`\`
docker pull agentscope/copaw:latest
docker run -p 127.0.0.1:8088:8088 -v copaw-data:/app/working agentscope/copaw:latest
\`\`\`

升级后重启服务 copaw app。`,

  ru: `### Как обновить CoPaw

Чтобы обновить CoPaw, выберите способ в зависимости от типа установки:

1. Если вы устанавливали через однострочный скрипт, повторно запустите установщик для обновления.

2. Если устанавливали через pip, выполните:

\`\`\`
pip install --upgrade copaw
\`\`\`

3. Если устанавливали из исходников, получите последние изменения и переустановите:

\`\`\`
cd CoPaw
git pull origin main
pip install -e .
\`\`\`

4. Если используете Docker, загрузите новый образ и перезапустите контейнер:

\`\`\`
docker pull agentscope/copaw:latest
docker run -p 127.0.0.1:8088:8088 -v copaw-data:/app/working agentscope/copaw:latest
\`\`\`

После обновления перезапустите сервис с помощью \`copaw app\`.`,

  en: `### How to update CoPaw

To update CoPaw, use the method matching your installation type:

1. If installed via one-line script, re-run the installer to upgrade.

2. If installed via pip, run:

\`\`\`
pip install --upgrade copaw
\`\`\`

3. If installed from source, pull the latest code and reinstall:

\`\`\`
cd CoPaw
git pull origin main
pip install -e .
\`\`\`

4. If using Docker, pull the latest image and restart the container:

\`\`\`
docker pull agentscope/copaw:latest
docker run -p 127.0.0.1:8088:8088 -v copaw-data:/app/working agentscope/copaw:latest
\`\`\`

After upgrading, restart the service with \`copaw app\`.`,
};
