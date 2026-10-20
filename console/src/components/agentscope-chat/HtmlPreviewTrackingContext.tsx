import React, { createContext, useContext } from "react";
import type { CronTaskOrigin } from "@/api/types/cronTaskSource";

export interface HtmlPreviewTrackingContextValue {
  cronTaskId?: string | null;
  cronTaskName?: string | null;
  /** 由当前任务会话绑定提供，仅用于已读接口分流，不发送给埋点接口。 */
  cronTaskOrigin?: CronTaskOrigin | null;
  disableEventRecording?: boolean;
}

const defaultValue: HtmlPreviewTrackingContextValue = {};

const HtmlPreviewTrackingContext =
  createContext<HtmlPreviewTrackingContextValue>(defaultValue);

export function HtmlPreviewTrackingProvider(props: {
  value: HtmlPreviewTrackingContextValue;
  children: React.ReactNode;
}) {
  return (
    <HtmlPreviewTrackingContext.Provider value={props.value}>
      {props.children}
    </HtmlPreviewTrackingContext.Provider>
  );
}

export function useHtmlPreviewTracking(): HtmlPreviewTrackingContextValue {
  return useContext(HtmlPreviewTrackingContext);
}
