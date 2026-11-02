/// <reference types="vite/client" />

declare module "*.less" {
  const classes: { [key: string]: string };
  export default classes;
}

interface PyWebViewAPI {
  open_external_link: (url: string) => void;
}

interface RuntimeEnvConfig {
  baseUrl?: string;
  serviceUnitId?: string;
  env?: string;
  systemCode?: string;
  systemSect?: string;
  responseFeedbackUserWhitelist?: string[];
  voiceRecorderUserWhitelist?: string[];
  directAccessUserWhitelist?: string[];
  operationsUsersByBranch?: Record<string, readonly string[]>;
  chatSessionPageSize?: number | string;
}

declare global {
  interface Window {
    pywebview?: {
      api: PyWebViewAPI;
    };
    __env__?: RuntimeEnvConfig;
  }
}

export {};
