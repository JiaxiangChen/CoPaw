import React, { useEffect, useRef } from "react";
import { useLocation, useNavigate } from "react-router-dom";
// ==================== 组件引入方式变更 (Kun He) ====================
import { useChatAnywhereSessionsState } from "@/components/agentscope-chat";
// ==================== 组件引入方式变更结束 ====================
import { useChatContentOnly } from "@/components/agentscope-chat/ChatContentOnlyContext";
import { useAgentStore } from "@/stores/agentStore";
import { getInitialSessionSelection } from "../../sessionApi/initialSessionSelection";
import { matchesResolvedChatId } from "../../sessionApi/resolvedSessionMapping";
import { getSessionAgentId } from "../../sessionApi/sessionAgent";

function matchesRequestedSession(
  session: unknown,
  requestedSessionId: string,
): boolean {
  const candidate = session as
    | {
        id?: string;
        realId?: string;
      }
    | undefined;
  return (
    candidate?.id === requestedSessionId ||
    candidate?.realId === requestedSessionId
  );
}

function isLocalTimestampSessionId(sessionId: string | undefined): boolean {
  return Boolean(sessionId && /^\d+$/.test(sessionId));
}

/**
 * URL chatId → context currentSessionId (one direction of bidirectional sync).
 *
 * Only reacts to URL or session list changes. currentSessionId is read via ref
 * to avoid triggering the effect when the context changes from the other direction
 * (context → URL via onSessionSelected), which would cause circular re-loads.
 */
const ChatSessionInitializer: React.FC = () => {
  const location = useLocation();
  const navigate = useNavigate();
  const isContentOnly = useChatContentOnly();

  const {
    sessions,
    currentSessionId,
    isSessionsListLoading,
    setCurrentSessionId,
    setSessionLoading,
    setSessionNotFound,
  } = useChatAnywhereSessionsState();
  const { selectedAgent, setSelectedAgent } = useAgentStore();

  const currentSessionIdRef = useRef(currentSessionId);
  currentSessionIdRef.current = currentSessionId;

  useEffect(() => {
    const requestedSessionId = location.pathname.match(/^\/chat\/(.+)$/)?.[1];
    const isNumericContentOnlyTarget =
      isContentOnly && isLocalTimestampSessionId(requestedSessionId);

    if (isNumericContentOnlyTarget && isSessionsListLoading) return;
    if (!sessions.length && !isNumericContentOnlyTarget) return;

    const { resolvedSessionId } = getInitialSessionSelection({
      pathname: location.pathname,
      sessionList: sessions,
    });

    const matching = sessions.find((s) => s.id === resolvedSessionId);
    if (isNumericContentOnlyTarget && !matching) {
      setCurrentSessionId(undefined);
      setSessionLoading(false);
      setSessionNotFound(true);
      return;
    }

    if (!resolvedSessionId) return;

    if (isContentOnly && matching) {
      setSessionNotFound(false);
    }

    // Resolving the active draft's backend identity must not reload its composer.
    if (
      !isContentOnly &&
      matchesResolvedChatId({
        requestedSessionId: currentSessionIdRef.current,
        chatId: resolvedSessionId,
      })
    )
      return;

    if (matching && currentSessionIdRef.current !== matching.id) {
      const sessionAgentId = getSessionAgentId(
        (matching as { meta?: Record<string, unknown> | null }).meta,
      );
      if (sessionAgentId && sessionAgentId !== selectedAgent) {
        setSelectedAgent(sessionAgentId);
      }
      setCurrentSessionId(matching.id);
    }

    if (
      !matching &&
      currentSessionIdRef.current !== resolvedSessionId &&
      !isLocalTimestampSessionId(currentSessionIdRef.current)
    ) {
      setCurrentSessionId(resolvedSessionId);
    }

    if (
      requestedSessionId &&
      resolvedSessionId !== requestedSessionId &&
      !matchesRequestedSession(matching, requestedSessionId)
    ) {
      navigate(`/chat/${resolvedSessionId}`, { replace: true });
    }
    // Intentionally exclude currentSessionId from deps: only react to URL / session list changes.
    // currentSessionId is read via ref to avoid circular triggers.
  }, [
    isContentOnly,
    isSessionsListLoading,
    location.pathname,
    navigate,
    selectedAgent,
    sessions,
    setCurrentSessionId,
    setSessionLoading,
    setSessionNotFound,
    setSelectedAgent,
  ]);

  return null;
};

export default ChatSessionInitializer;
