import { getApiUrl, clearAuthToken } from "./config";
import { buildAuthHeaders } from "./authHeaders";
import {
  isExternalTokenEnabled,
  ensureValidToken,
  clearExternalToken,
} from "./externalToken";

function getErrorMessageFromBody(
  text: string,
  contentType: string,
): string | null {
  if (!text) {
    return null;
  }

  if (!contentType.includes("application/json")) {
    return text;
  }

  try {
    const payload = JSON.parse(text) as {
      detail?: unknown;
      message?: unknown;
      error?: unknown;
    };

    if (typeof payload.detail === "string" && payload.detail) {
      return payload.detail;
    }
    if (typeof payload.message === "string" && payload.message) {
      return payload.message;
    }
    if (typeof payload.error === "string" && payload.error) {
      return payload.error;
    }
  } catch {
    return text;
  }

  return text;
}

function buildHeaders(
  method?: string,
  extra?: HeadersInit,
  body?: BodyInit | null,
): Headers {
  // 统一转为 Headers，保证后续读取和写入行为一致。
  const headers = extra instanceof Headers ? extra : new Headers(extra);
  const hasBody = body !== undefined && body !== null;

  // 仅对通常携带请求体的方法补默认 Content-Type。
  if (
    method &&
    hasBody &&
    ["POST", "PUT", "PATCH", "DELETE"].includes(method.toUpperCase())
  ) {
    // FormData 需要浏览器自动生成 multipart boundary，不能强行写死 Content-Type。
    if (!headers.has("Content-Type") && !(body instanceof FormData)) {
      headers.set("Content-Type", "application/json");
    }
  }

  for (const [key, value] of Object.entries(buildAuthHeaders())) {
    if (!headers.has(key)) {
      headers.set(key, value);
    }
  }

  return headers;
}

async function parseResponse<T>(response: Response): Promise<T> {
  if (response.status === 204) {
    return undefined as T;
  }

  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) {
    return (await response.text()) as unknown as T;
  }

  return (await response.json()) as T;
}

async function throwRequestError(response: Response): Promise<never> {
  const text = await response.text().catch(() => "");
  const contentType = response.headers.get("content-type") || "";
  const errorMessage = getErrorMessageFromBody(text, contentType);

  // JSON 响应已提取到 detail/message/error 时不再拼接原始 JSON
  const isJson = contentType.includes("application/json");
  const finalMessage = isJson && errorMessage
    ? errorMessage
    : errorMessage
      ? `${errorMessage} - ${text}`
      : `Request failed: ${response.status} ${response.statusText}`;

  const error = new Error(finalMessage);
  // 将 HTTP 状态码和解析后的响应体挂载到 Error 上，
  // 方便调用方根据 status / data 做差异化处理（如 409 同名冲突）。
  (error as Error & { status?: number; data?: unknown }).status =
    response.status;

  if (contentType.includes("application/json") && text) {
    try {
      (error as Error & { status?: number; data?: unknown }).data =
        JSON.parse(text);
    } catch {
      // 忽略 JSON 解析失败
    }
  }

  throw error;
}

function throwLocalAuthError(): never {
  clearAuthToken();
  throw new Error("认证已失效，请刷新页面或重新进入系统后再试");
}

function throwExternalAuthError(): never {
  clearExternalToken();
  throw new Error("登录状态已失效，请刷新页面或重新进入后再试");
}

export async function request<T = unknown>(
  path: string,
  options: RequestInit = {},
  disDefaultHeaders?: boolean,
): Promise<T> {
  const url = getApiUrl(path);
  const method = options.method || "GET";
  const headers = disDefaultHeaders?{"Content-Type":"application/json"}:buildHeaders(method, options.headers, options.body);

  const response = await fetch(url, {
    ...options,
    headers,
  });

  if (!response.ok) {
    // 401 时优先尝试刷新外部 token 并重试，最终失败也只抛错，不跳转页面。
    if (response.status === 401) {
      if (isExternalTokenEnabled()) {
        let canRetry = false;
        try {
          await ensureValidToken(true);
          canRetry = true;
        } catch {
          // 刷新失败说明缓存 token 不可信，清理后向调用方暴露认证失败。
          clearExternalToken();
        }

        if (canRetry) {
          // 重试仍走统一 header 构建，避免外部 token 与普通 token 逻辑分叉。
          const newHeaders = buildHeaders(
            method,
            options.headers,
            options.body,
          );

          const retryResponse = await fetch(url, {
            ...options,
            headers: newHeaders,
          });

          if (retryResponse.ok) {
            return parseResponse<T>(retryResponse);
          }

          // 外部 token 由宿主系统负责登录态，重试仍 401 时只抛错，不跳转内部登录页。
          if (retryResponse.status === 401) {
            throwExternalAuthError();
          }

          await throwRequestError(retryResponse);
        }

        throwExternalAuthError();
      }

      // 项目作为宿主系统内嵌页面使用，认证失效只抛错，不跳转内部登录页。
      throwLocalAuthError();
    }

    await throwRequestError(response);
  }

  return parseResponse<T>(response);
}
