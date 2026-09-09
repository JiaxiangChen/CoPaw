import { useMemo, type ReactNode } from "react";
import { useContextSelector } from "use-context-selector";
import { ChatAnywhereMessagesContext } from "./AgentScopeRuntimeWebUI/core/Context/ChatAnywhereMessagesContext";
import { ChatAnywhereSessionsContext } from "./AgentScopeRuntimeWebUI/core/Context/ChatAnywhereSessionsContext";
import { AutoPreviewHtmlProvider } from "./AutoPreviewHtmlContext";
import { findLatestAutoPreviewUrl } from "./autoPreviewSelection";
import { useChatAnywhereOptions } from "./AgentScopeRuntimeWebUI/core/Context/ChatAnywhereOptionsContext";

export function ChatAutoPreviewHtmlProvider(props: {
  children: ReactNode;
  triggerKey: number;
  onConsumed: () => void;
}) {
  const messages = useContextSelector(
    ChatAnywhereMessagesContext,
    (v) => v.messages,
  );
  const loading = useContextSelector(
    ChatAnywhereSessionsContext,
    (v) => v.isSessionLoading,
  );
  const resolveUrl = useChatAnywhereOptions((v) => v.api?.replaceMediaURL);
  const ready = !loading;
  const targetUrl = useMemo(
    () =>
      props.triggerKey > 0 && ready ? findLatestAutoPreviewUrl(messages) : null,
    [messages, props.triggerKey, ready],
  );

  return (
    <AutoPreviewHtmlProvider
      {...props}
      ready={ready}
      targetUrl={targetUrl}
      resolveUrl={resolveUrl}
    />
  );
}
