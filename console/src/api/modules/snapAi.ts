/**
 * !!!!!!!!!注意，此文件需要对比
 * ！！！！！！
 * ！！！！！！
 * 随手改-AI 调试接口封装
 *
 * 作用：块选中后 AI 问答/调试，用户输入自然语言描述，后端调用 A+ workflow 对 HTML 块
 * 进行改版，返回改写后的块 HTML，最终由编辑器 iframe 渲染到报告中。
 *
 *
 */
import { buildAuthHeaders } from "../authHeaders";
import { request } from "../request";
// import { Base64 } from 'js-base64';

export interface SnapAiRequest {
  /** 选中块的原始 HTML */
  html: string;
  /** 用户自然语言描述 / 调整要求 */
  suggest: string;
  /** 可选，期望的样式描述 */
  style?: string;
}

export interface SnapAiResponse {
  returnCode?: string;
  errorMsg?: string | null;
  body?: {
    output?: {
      result?: string;
    };
  };
}

/**
 * 将「用户描述 + 原始块 HTML + 全局样式」组装为发送给 AI 的输入参数。
 * @param prompt 用户自然语言描述/调整要求
 * @param blockHtml 选中块的原始 HTML
 * @param style 可选，报告全局样式：所有 <style> 标签内容的拼接
 */
export function buildSnapAiInput(
  prompt: string,
  blockHtml: string,
  style?: string,
): SnapAiRequest {
  return {
    //     html: Base64.encode(blockHtml),
    html: blockHtml,
    suggest: prompt,
    //     style: style ? Base64.encode(style) : "",
    style: "",
  };
}

/** 判定当前是否为测试/开发环境 */
function isDevEnv(): boolean {
  if (typeof window === "undefined") return true;
  const href = window.location.href;
  return (
    href.indexOf(".paasst.") !== -1 ||
    href.indexOf("localhost") !== -1 ||
    href.indexOf("127.0.0.1") !== -1
  );
}


/**
 * 调用 A+ AI 调试工作流，返回改写后的块 HTML。
 * @param request 入参
 * @returns 改写后的块 HTML 字符串；失败或为空时返回空字符串
 */
export async function fetchSnapAiHtml(
  request: SnapAiRequest,
): Promise<string> {
  try {
    const isDev = isDevEnv();
    const env = isDev ? "dev" : "prd";
    const baseUrl = isDev ? 'https://xxx.zzzz.yyy.cn' : 'https://xxxx.zzz.yy.cn';
    const apiKey = isDev
      ? "2222"
      : "cccc";
    const apiUrl = `${baseUrl}/openapi/runtime/app/07c1cda53/tag/${env}/workflow/run/WFWw5kFqIK`;

    const response = await fetch(apiUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "api-key": apiKey,
        ...buildAuthHeaders(),
      },
      body: JSON.stringify({
        inputParams: request,
        responseMode: "noStreaming",
      }),
    });

    if (!response.ok) {
      console.error("[SnapAi] API request failed:", response.status);
      return "";
    }

    const data: SnapAiResponse = await response.json();
    if (data.returnCode && data.returnCode !== "SUC0000") {
      console.error("[SnapAi] workflow return error:", data.errorMsg);
      return "";
    }
    return data.body?.output?.result ?? "";
  } catch (error) {
    console.error("[SnapAi] API request error:", error);
    return "";
  }
}

/**
 * 保存应答记录
 *
 * 作用：AI 调试「确认替换」后，父组件将更新后的报告应答数据（recordData / a 对象）
 * 序列化后调用本接口保存到 Elasticsearch，以 resultId（idKey）作为文档 ID。
 *
 * 接口来源：console/模块模版文件上传.md —— 保存应答记录。
 *  - POST /agent-evaluate/answer/save
 *  - 入参：{ idKey, answer }，其中 idKey 为 resultId，answer 为 result 接口数据的字符序列化
 *
 */
export interface SaveAnswerRecordParams {
  /** 应答记录唯一标识，同时作为 ES 文档 ID */
  idKey: string;
  /** 应答记录内容（result 接口返回数据的字符序列化 JSON 字符串） */
  answer: string;
}

export interface SaveAnswerRecordResponse {
  code?: string | number;
  message?: string;
  result?: boolean | unknown;
  data?: unknown;
}

/**
 * 保存应答记录
 *
 * 作用：AI 调试「确认替换」后，父组件将更新后的报告应答数据（recordData / a 对象）
 * 序列化后调用本接口保存到 Elasticsearch，以 resultId（idKey）作为文档 ID。
 *
 * 接口来源：console/模块模版文件上传.md —— 保存应答记录。
 *  - POST /agent-evaluate/answer/save
 *  - 入参：{ idKey, answer }，其中 idKey 为 resultId，answer 为 result 接口数据的字符序列化
 */
export const saveAnswerRecord = (
  params: SaveAnswerRecordParams,
): Promise<boolean> =>
  request<boolean>("/agent-evaluate/answer/save", {
    method: "POST",
    body: JSON.stringify(params),
  }, true).catch((error) => {
    console.error("[SnapAi] save answer record exception:", error);
    return false;
  });
