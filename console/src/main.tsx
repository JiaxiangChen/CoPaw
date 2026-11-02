import "./utils/browserCompat";
import { createRoot } from "react-dom/client";
import App from "./App.tsx";
import "./i18n";
// 在 React 渲染之前尽早初始化 iframe 消息监听器
// 确保不遗漏父窗口发送的任何初始化消息 (USER_DATA)
import {
  initIframeMessageListener,
  resetIframeContextForEntry,
  handleUrlOriginParam,
  fetchAndSetUserName,
  isWealthMessageEntry,
  waitForWealthUserData,
} from "./utils/iframeMessage";
import { isExternalTokenEnabled, ensureValidToken } from "./api/externalToken";
import { applyConsoleDesignTokens } from "./config/consoleDesignTokens";
import { initializeChatPresentationFromUrl } from "./stores/chatPresentationStore";
import AccessDeniedPage from "./access/AccessDeniedPage";
import {
  getConsoleAccessDecision,
  runAccessControlledInitialization,
} from "./access/consoleAccess";

applyConsoleDesignTokens();

/**
 * 初始化流程：
 * 1. 校验 iframe 或顶层窗口用户白名单
 * 2. 尽早初始化 iframe 消息监听器，避免遗漏父窗口消息
 * 3. 获取并等待外部 token（如果配置了）
 * 4. 处理 URL 参数场景
 * 5. 查询用户名称
 * 6. 渲染 React 应用
 */
async function initializeAllowedApp(
  root: ReturnType<typeof createRoot>,
): Promise<void> {
  // URL presentation flags initialize once per full page load and remain stable
  // while the router normalizes or replaces chat URLs.
  initializeChatPresentationFromUrl(
    window.location.pathname,
    window.location.search,
  );

  // 先清理旧入口，再接收本次页面的消息，避免等待 token 后误清新上下文。
  resetIframeContextForEntry();
  // 初始化 iframe 消息监听器（在 React 渲染之前）
  // 确保不遗漏父窗口发送的任何消息
  initIframeMessageListener();

  // 在需要鉴权的初始化逻辑之前获取 token，同步等待完成
  if (isExternalTokenEnabled()) {
    try {
      await ensureValidToken();
    } catch (error) {
      console.warn("SWE: 初始化token失败", error);
    }
  }

  // 处理传递URL参数的场景（需要在 token 初始化之后）
  await handleUrlOriginParam();

  // 财富消息入口等待父应用身份，避免用残留用户挂载业务页面。
  if (isWealthMessageEntry()) {
    await waitForWealthUserData();
  } else {
    await fetchAndSetUserName();
  }

  root.render(<App />);
}

async function initializeApp(): Promise<void> {
  const root = createRoot(document.getElementById("root")!);
  const decision = getConsoleAccessDecision();

  await runAccessControlledInitialization({
    decision,
    initializeAllowedApp: () => initializeAllowedApp(root),
    renderAccessDenied: () => {
      root.render(<AccessDeniedPage />);
    },
  });
}

// 启动初始化
initializeApp();
