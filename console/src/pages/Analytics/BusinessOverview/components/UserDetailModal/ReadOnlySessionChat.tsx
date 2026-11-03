import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Button, Empty, Spin } from "antd";
import {
  AgentScopeRuntimeWebUIComposedProvider,
  Bubble,
  IAgentScopeRuntimeWebUIMessage,
  IAgentScopeRuntimeWebUIOptions,
} from "@/components/agentscope-chat";
import { HtmlPreviewTrackingProvider } from "@/components/agentscope-chat/HtmlPreviewTrackingContext";
import { tracingApi } from "../../../../../api/modules/tracing";
import type { Message } from "../../../../../api/types";
import {
  convertMessages,
  convertMessagesForSession,
  convertArchivedPage,
  withArchiveBoundaries,
} from "../../../../Chat/sessionApi";
import { mergeHistoryResponses } from "../../../../Chat/sessionApi/historyAssembly";
import { useAgentStore } from "@/stores/agentStore";
import { useIframeStore } from "@/stores/iframeStore";
import useHistoryPreload from "@/components/agentscope-chat/Bubble/hooks/useHistoryPreload";
import HistoryLoadStatus from "@/components/agentscope-chat/AgentScopeRuntimeWebUI/core/Chat/MessageList/HistoryLoadStatus";
import type { BubbleListRef } from "@/components/agentscope-chat/Bubble/BubbleList";
import ConversationCompactionBoundary from "../../../../Chat/components/ConversationCompactionBoundary";
import RuntimeRequestCard from "../../../../Chat/components/RuntimeRequestCard";
import RuntimeResponseCard from "../../../../Chat/components/RuntimeResponseCard";
import { PlanReviewSnapshot } from "../../../../Chat/components/PlanInteractionCards";
import type {
  ChatRuntimeRequestCardData,
  ChatRuntimeResponseCardData,
  ChatTaskRunGroupCardData,
} from "../../../../Chat/messageMeta";
import ConversationQuickNav from "../../../../../components/ConversationQuickNav";
import styles from "./index.module.less";

const CURRENT_HISTORY_BATCH_SIZE = 10;
const ARCHIVE_HISTORY_PAGE_LIMIT = 20;

const READONLY_OPTIONS: IAgentScopeRuntimeWebUIOptions = {
  theme: {
    locale: "zh-CN",
    bubbleList: {
      pagination: false,
    },
  },
  session: {
    multiple: false,
    api: {
      getSessionList: async () => [],
      getSession: async () => ({ id: "", name: "", messages: [] }),
      createSession: async () => [],
      updateSession: async () => [],
      removeSession: async () => [],
    },
  },
  actions: {
    list: [],
    replace: false,
  },
  api: {
    replaceMediaURL: (url: string) => url,
  },
} as unknown as IAgentScopeRuntimeWebUIOptions;

const READONLY_CARDS = {
  ConversationCompactionBoundary,
  AgentScopeRuntimeRequestCard: (props: {
    data: ChatRuntimeRequestCardData;
  }) => <RuntimeRequestCard {...props} />,
  AgentScopeRuntimeResponseCard: (props: {
    data: ChatRuntimeResponseCardData;
    isLast?: boolean;
  }) => (
    <>
      <RuntimeResponseCard
        {...props}
        data={{ ...props.data, planReviewCard: undefined }}
      />
      {props.data.planReviewCard && (
        <PlanReviewSnapshot data={props.data.planReviewCard} />
      )}
    </>
  ),
  ResponseFeedback: () => null,
  // Plan reviews are shown once as a read-only snapshot beside the response.
  PlanInteraction: () => null,
  ApprovalAction: ({ data }: { data: { status?: string } }) => (
    <span>工具审批状态：{data.status || "未知"}</span>
  ),
  WPlusSopEntryProposal: () => null,
  TaskRunGroupCard: ({ data }: { data: ChatTaskRunGroupCardData }) => (
    <div>
      <div>{data.taskName || `任务 ${data.runIndex + 1}`}</div>
      {data.finalMessages.map((message) => (
        <Bubble key={message.id} {...message} />
      ))}
      {data.stepMessages.length > 0 && (
        <details>
          <summary>查看执行过程</summary>
          {data.stepMessages.map((message) => (
            <Bubble key={message.id} {...message} />
          ))}
        </details>
      )}
    </div>
  ),
};

interface ReadOnlySessionChatProps {
  selectedSessionId: string | null;
  chatIdBySessionId: Record<string, string>;
  targetUserId?: string;
  mockMessages?: Message[];
}

export default function ReadOnlySessionChat(props: ReadOnlySessionChatProps) {
  const source = useIframeStore((state) => state.source);
  const agent = useAgentStore((state) => state.selectedAgent);
  const chatId = props.selectedSessionId
    ? props.chatIdBySessionId[props.selectedSessionId]
    : undefined;
  const identity = JSON.stringify([
    source,
    agent,
    props.targetUserId,
    props.selectedSessionId,
    chatId,
  ]);
  return <SessionHistory key={identity} {...props} chatId={chatId} />;
}

function SessionHistory({
  selectedSessionId,
  targetUserId,
  mockMessages,
  chatId,
}: ReadOnlySessionChatProps & { chatId?: string }) {
  const chatContentRef = useRef<HTMLDivElement | null>(null);
  const listRef = useRef<BubbleListRef>(null);
  const alive = useRef(false);
  const paging = useRef(false);
  const cursor = useRef<string | null>(null);
  type Anchor = { element: HTMLElement; id: string; offset: number };
  const anchor = useRef<Anchor | null>(null);
  const readingAnchor = useRef<Anchor | null>(null);
  const restoredTop = useRef<number | null>(null);
  const [historyScrollElement, setHistoryScrollElement] =
    useState<HTMLDivElement | null>(null);
  const bindListRef = useCallback((node: BubbleListRef | null) => {
    listRef.current = node;
    setHistoryScrollElement(node?.getScrollElement() || null);
  }, []);
  const [chatMessages, setChatMessages] = useState<
    IAgentScopeRuntimeWebUIMessage[]
  >([]);
  const [chatLoading, setChatLoading] = useState(true);
  const [detailError, setDetailError] = useState(false);
  const [reload, setReload] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [historyStarted, setHistoryStarted] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState(false);
  const [historyRetrying, setHistoryRetrying] = useState(false);
  const [visibleHistoryCount, setVisibleHistoryCount] = useState(
    CURRENT_HISTORY_BATCH_SIZE,
  );
  const hasHiddenLocalHistory =
    !historyStarted && chatMessages.length > visibleHistoryCount;
  const visibleMessages = useMemo(
    () =>
      historyStarted ? chatMessages : chatMessages.slice(-visibleHistoryCount),
    [chatMessages, historyStarted, visibleHistoryCount],
  );
  const reversedMessages = useMemo(
    () => [...visibleMessages].reverse(),
    [visibleMessages],
  );

  useEffect(() => {
    alive.current = true;
    let cancelled = false;
    const load = async () => {
      setChatLoading(true);
      setDetailError(false);
      try {
        if (mockMessages) {
          setChatMessages(
            convertMessages(mockMessages).map((message) => ({
              ...message,
              history: true,
            })),
          );
        } else if (chatId && targetUserId) {
          const history = await tracingApi.getUserChat(targetUserId, chatId);
          if (cancelled) return;
          setChatMessages(
            withArchiveBoundaries(
              convertMessagesForSession(
                history.messages || [],
                history.chat?.meta,
                history.chat?.name,
              ),
              history.archive?.boundaries,
            ).map((message) => ({ ...message, history: true })),
          );
          setHasMore(Boolean(history.archive?.has_more));
        }
      } catch {
        if (!cancelled) setDetailError(true);
      } finally {
        if (!cancelled) setChatLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
      alive.current = false;
    };
  }, [chatId, targetUserId, mockMessages, reload]);

  const captureAnchor = () => {
    const scroll = listRef.current?.getScrollElement();
    if (!scroll) return;
    const bounds = scroll.getBoundingClientRect();
    const isVisible = (element: HTMLElement) => {
      const rect = element.getBoundingClientRect();
      return (
        rect.height > 0 && rect.bottom > bounds.top && rect.top < bounds.bottom
      );
    };
    const element =
      Array.from(
        scroll.querySelectorAll<HTMLElement>("[data-history-anchor-id]"),
      ).find(isVisible) ||
      Array.from(scroll.querySelectorAll<HTMLElement>("[data-role][id]")).find(
        isVisible,
      );
    anchor.current = element
      ? {
          element,
          id: element.dataset.historyAnchorId || element.id,
          offset: element.getBoundingClientRect().top - bounds.top,
        }
      : null;
  };

  useLayoutEffect(() => {
    const previous = anchor.current || readingAnchor.current;
    const scroll = listRef.current?.getScrollElement();
    if (!previous || !scroll) return;
    anchor.current = previous;
    const restore = () => {
      if (anchor.current !== previous && readingAnchor.current !== previous)
        return;
      const element = previous.element.isConnected
        ? previous.element
        : Array.from(
            scroll.querySelectorAll<HTMLElement>(
              "[data-history-anchor-id], [data-role][id]",
            ),
          ).find(
            (item) => (item.dataset.historyAnchorId || item.id) === previous.id,
          );
      if (!element) {
        anchor.current = null;
        return;
      }
      scroll.scrollTop +=
        element.getBoundingClientRect().top -
        scroll.getBoundingClientRect().top -
        previous.offset;
      restoredTop.current = scroll.scrollTop;
    };
    restore();
    let frames = 2;
    let frameId: number;
    const afterLayout = () => {
      restore();
      frames -= 1;
      if (frames > 0) frameId = requestAnimationFrame(afterLayout);
      else if (anchor.current === previous) {
        anchor.current = null;
        readingAnchor.current = previous;
      }
    };
    frameId = requestAnimationFrame(afterLayout);
    const observer =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(restore);
    Array.from(scroll.children).forEach((child) => observer?.observe(child));
    return () => {
      cancelAnimationFrame(frameId);
      observer?.disconnect();
    };
  }, [chatMessages, visibleHistoryCount, historyStarted, historyScrollElement]);

  const loadEarlier = async (retrying = false): Promise<boolean> => {
    if (!targetUserId || !chatId || paging.current || !hasMore) return false;
    paging.current = true;
    setHistoryRetrying(retrying);
    setHistoryLoading(true);
    setHistoryError(false);
    try {
      const page = await tracingApi.getUserChatHistory(
        targetUserId,
        chatId,
        cursor.current,
        ARCHIVE_HISTORY_PAGE_LIMIT,
      );
      if (!alive.current) return false;
      if (
        page.has_more &&
        (!page.next_cursor || page.next_cursor === cursor.current)
      )
        throw new Error("Archive cursor did not advance");
      captureAnchor();
      setChatMessages((current) =>
        mergeHistoryResponses([
          ...convertArchivedPage(page.messages || [], page.boundaries || []),
          ...current,
        ]),
      );
      cursor.current = page.next_cursor || null;
      setHasMore(page.has_more);
      setHistoryStarted(true);
      return true;
    } catch {
      if (alive.current) setHistoryError(true);
      return false;
    } finally {
      if (alive.current) {
        paging.current = false;
        setHistoryLoading(false);
        setHistoryRetrying(false);
      }
    }
  };

  const unavailable = !mockMessages && (!chatId || !targetUserId);
  const historyLoadState = historyLoading
    ? "loading"
    : historyError
    ? "error"
    : !hasHiddenLocalHistory && !hasMore
    ? "exhausted"
    : "idle";
  const advanceHistory = async () => {
    if (paging.current) return false;
    if (hasHiddenLocalHistory) {
      captureAnchor();
      setVisibleHistoryCount((count) =>
        Math.min(count + CURRENT_HISTORY_BATCH_SIZE, chatMessages.length),
      );
      return true;
    }
    return loadEarlier();
  };
  const historyPreload = useHistoryPreload({
    scrollElement: historyScrollElement,
    onNearStart: advanceHistory,
    onRetry: () => loadEarlier(true),
    disabled:
      chatLoading || detailError || unavailable || historyLoadState !== "idle",
    resetKey: chatId,
    isAnchorRecoveryPending: () => anchor.current !== null,
    isRestoringAnchor: () => {
      if (
        restoredTop.current !== null &&
        historyScrollElement &&
        Math.abs(historyScrollElement.scrollTop - restoredTop.current) <= 2
      )
        return true;
      restoredTop.current = null;
      return false;
    },
  });
  const historyControl = (
    <HistoryLoadStatus
      state={historyLoadState}
      retrying={historyRetrying}
      onRetry={historyPreload.retry}
      onLoadMore={
        hasHiddenLocalHistory || hasMore ? historyPreload.loadMore : undefined
      }
      advancing={historyPreload.isAdvancing}
    />
  );

  return (
    <div className={styles.readonlyChat}>
      <div className={styles.readonlyChatTitleRow}>
        <div className={styles.readonlyChatTitle}>聊天记录</div>
      </div>
      {!selectedSessionId ? (
        <div className={styles.readonlyChatEmpty}>
          <Empty description="请选择左侧会话卡片查看聊天内容" />
        </div>
      ) : chatLoading ? (
        <div className={styles.readonlyChatLoading}>
          <Spin />
        </div>
      ) : detailError ? (
        <div className={styles.readonlyChatEmpty}>
          <div role="alert">
            聊天记录加载失败
            <Button onClick={() => setReload((value) => value + 1)}>
              重试
            </Button>
          </div>
        </div>
      ) : unavailable ? (
        <div className={styles.readonlyChatEmpty}>
          <Empty description="未找到该会话的聊天映射或目标用户" />
        </div>
      ) : chatMessages.length === 0 && !hasMore && !historyStarted ? (
        <div className={styles.readonlyChatEmpty}>
          <Empty description="暂无聊天内容" />
        </div>
      ) : (
        <HtmlPreviewTrackingProvider value={{ disableEventRecording: true }}>
          <AgentScopeRuntimeWebUIComposedProvider
            options={READONLY_OPTIONS}
            cards={READONLY_CARDS}
          >
            <div ref={chatContentRef} className={styles.readonlyChatBubbleList}>
              <Bubble.List
                ref={bindListRef}
                pagination={false}
                order="desc"
                items={reversedMessages}
                autoScrollToBottom="initial"
                disableBrowserScrollAnchoring
                onBottomStateChange={() => {
                  const scroll = listRef.current?.getScrollElement();
                  if (
                    scroll &&
                    restoredTop.current !== null &&
                    Math.abs(scroll.scrollTop - restoredTop.current) <= 2
                  )
                    return;
                  restoredTop.current = null;
                  anchor.current = null;
                  readingAnchor.current = null;
                }}
                topContent={historyControl}
                classNames={{
                  wrapper: styles.readonlyBubbleWrapper,
                  list: styles.readonlyBubbleList,
                }}
              />
              <ConversationQuickNav
                messages={visibleMessages}
                scrollRootRef={chatContentRef}
              />
            </div>
          </AgentScopeRuntimeWebUIComposedProvider>
        </HtmlPreviewTrackingProvider>
      )}
    </div>
  );
}
