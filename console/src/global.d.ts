/**
 * ============================================================
 * Author: Kun He
 * Description: 全局类型定义
 * Date: 2026-04-07
 * ============================================================
 */
declare global {
  interface Window {
    __env__?: {
      baseUrl?: string;
      env?: string;
      serviceUnitId?: string;
      systemCode?: string;
      systemSect?: string;
      responseFeedbackUserWhitelist?: string[];
      voiceRecorderUserWhitelist?: string[];
      directAccessUserWhitelist?: string[];
      operationsUsersByBranch?: Record<string, readonly string[]>;
      chatSessionPageSize?: number | string;
    };
    __postMsgSwe__?: string;
  }
}

// iframe postMessage 通信类型导出
// 使其他模块可以直接从 global.d.ts 导入类型
export type { AuthHeaderItem, IframeContext } from "./types/iframe";
