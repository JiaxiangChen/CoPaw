import { useEffect, useLayoutEffect, useRef, useState } from "react";
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
  const anchor = useRef<{
    element: HTMLElement;
    top: number;
    scrollTop: number;
    height: number;
  } | null>(null);
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

  useEffect(() => {
    alive.current = true;
    let cancelled = false;
    const load = async () => {
      setChatLoading(true);
      setDetailError(false);
      try {
        if (mockMessages) {
          setChatMessages(convertMessages(mockMessages));
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

  useLayoutEffect(() => {
    const previous = anchor.current;
    const scroll = listRef.current?.getScrollElement();
    if (!previous || !scroll) return;
    scroll.scrollTop = previous.element.isConnected
      ? previous.scrollTop +
        previous.element.getBoundingClientRect().top -
        previous.top
      : previous.scrollTop + scroll.scrollHeight - previous.height;
    anchor.current = null;
  }, [chatMessages]);

  const loadEarlier = async () => {
    if (!targetUserId || !chatId || paging.current || !hasMore) return;
    paging.current = true;
    setHistoryLoading(true);
    setHistoryError(false);
    try {
      const page = await tracingApi.getUserChatHistory(
        targetUserId,
        chatId,
        cursor.current,
        20,
      );
      if (!alive.current) return;
      if (
        page.has_more &&
        (!page.next_cursor || page.next_cursor === cursor.current)
      )
        throw new Error("Archive cursor did not advance");
      const scroll = listRef.current?.getScrollElement();
      if (scroll) {
        const bounds = scroll.getBoundingClientRect();
        const isVisible = (item: HTMLElement) => {
          const rect = item.getBoundingClientRect();
          return rect.bottom > bounds.top && rect.top < bounds.bottom;
        };
        const element =
          Array.from(
            scroll.querySelectorAll<HTMLElement>("[data-history-anchor-id]"),
          ).find(isVisible) ||
          Array.from(
            scroll.querySelectorAll<HTMLElement>("[data-role][id]"),
          ).find(isVisible);
        if (element)
          anchor.current = {
            element,
            top: element.getBoundingClientRect().top,
            scrollTop: scroll.scrollTop,
            height: scroll.scrollHeight,
          };
      }
      setChatMessages((current) =>
        mergeHistoryResponses([
          ...convertArchivedPage(page.messages || [], page.boundaries || []),
          ...current,
        ]),
      );
      cursor.current = page.next_cursor || null;
      setHasMore(page.has_more);
      setHistoryStarted(true);
    } catch {
      if (alive.current) setHistoryError(true);
    } finally {
      if (alive.current) {
        paging.current = false;
        setHistoryLoading(false);
      }
    }
  };

  const unavailable = !mockMessages && (!chatId || !targetUserId);
  const historyControl = (hasMore || historyStarted) && (
    <div className={styles.readonlyHistoryControl} aria-live="polite">
      {historyError && <span role="alert">历史记录加载失败，请重试</span>}
      {hasMore ? (
        <Button
          type="link"
          loading={historyLoading}
          onClick={() => void loadEarlier()}
        >
          {historyError ? "重试加载历史" : "加载更早历史"}
        </Button>
      ) : (
        <span>已到达会话开始处</span>
      )}
    </div>
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
                ref={listRef}
                pagination={false}
                order="asc"
                items={chatMessages}
                autoScrollToBottom="initial"
                disableBrowserScrollAnchoring
                topContent={historyControl}
                classNames={{
                  wrapper: styles.readonlyBubbleWrapper,
                  list: styles.readonlyBubbleList,
                }}
              />
              <ConversationQuickNav
                messages={chatMessages}
                scrollRootRef={chatContentRef}
              />
            </div>
          </AgentScopeRuntimeWebUIComposedProvider>
        </HtmlPreviewTrackingProvider>
      )}
    </div>
  );
}
