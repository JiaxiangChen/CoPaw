import { getWPlusCookie } from "../utils/cookie-utils";

export type ConsoleAccessDecision = {
  allowed: boolean;
  reason:
    | "local-development"
    | "embedded"
    | "chat-share"
    | "direct-allowlist"
    | "direct-denied";
  userId: string | null;
};

type ResolveConsoleAccessParams = {
  isDevelopment?: boolean;
  isEmbedded: boolean;
  isChatShare?: boolean;
  userId: string | null;
  directAccessUserWhitelist: readonly string[];
};

type AccessControlledInitializationParams = {
  decision: ConsoleAccessDecision;
  initializeAllowedApp: () => void | Promise<void>;
  renderAccessDenied: (decision: ConsoleAccessDecision) => void;
};

function decodeCookieValue(value: string | null): string | null {
  if (!value) return null;
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

function getDirectAccessUserWhitelist(): readonly string[] {
  const whitelist = window.__env__?.directAccessUserWhitelist;
  return Array.isArray(whitelist) ? whitelist : [];
}

export function isConsoleEmbedded(): boolean {
  try {
    return window.self !== window.top;
  } catch {
    return true;
  }
}

export function isChatSharePath(pathname: string): boolean {
  return /^\/(?:console\/)?chat-share\/[^/]+\/?$/.test(pathname);
}

export function resolveConsoleAccess({
  isDevelopment = false,
  isEmbedded,
  isChatShare = false,
  userId,
  directAccessUserWhitelist,
}: ResolveConsoleAccessParams): ConsoleAccessDecision {
  if (isEmbedded) {
    return { allowed: true, reason: "embedded", userId };
  }

  if (isDevelopment) {
    return { allowed: true, reason: "local-development", userId };
  }

  if (isChatShare) {
    return { allowed: true, reason: "chat-share", userId };
  }

  const normalizedWhitelist = directAccessUserWhitelist
    .map((item) => item.trim())
    .filter((item) => item && item !== "*");
  const allowed = Boolean(userId && normalizedWhitelist.includes(userId));

  return {
    allowed,
    reason: allowed ? "direct-allowlist" : "direct-denied",
    userId,
  };
}

export function getConsoleAccessDecision(
  isEmbedded = isConsoleEmbedded(),
  isDevelopment = import.meta.env.DEV,
  pathname = window.location.pathname,
): ConsoleAccessDecision {
  return resolveConsoleAccess({
    isDevelopment,
    isEmbedded,
    isChatShare: isChatSharePath(pathname),
    userId: decodeCookieValue(getWPlusCookie("userid")),
    directAccessUserWhitelist: getDirectAccessUserWhitelist(),
  });
}

export async function runAccessControlledInitialization({
  decision,
  initializeAllowedApp,
  renderAccessDenied,
}: AccessControlledInitializationParams): Promise<void> {
  if (!decision.allowed) {
    renderAccessDenied(decision);
    return;
  }

  await initializeAllowedApp();
}
