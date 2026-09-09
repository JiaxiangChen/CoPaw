import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { IconButton } from "@agentscope-ai/design";
import { SparkShareLine } from "@agentscope-ai/icons";
import { Button, Checkbox, Flex, Tooltip, message } from "antd";
import { CloseOutlined, GlobalOutlined, LinkOutlined } from "@ant-design/icons";
import { chatApi } from "@/api/modules/chat";
import type { ChatShareOptions } from "@/api/types";
import { buildChatShareUrl } from "./shareUrl";
import { getShareSelectionState } from "./shareSelection";
import { useChatShareSelection } from "../../chatShareContext";
import { copyToClipboard } from "@/utils/clipboard";
import styles from "./index.module.less";

interface ChatActionGroupProps {
  chatId?: string;
}

interface ChatAreaBounds {
  left: number;
  width: number;
}

const ChatActionGroup: React.FC<ChatActionGroupProps> = ({ chatId }) => {
  const shareSelection = useChatShareSelection();
  const { close } = shareSelection;
  const selectedTurnIdsRef = useRef(shareSelection.selectedTurnIds);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [chatAreaBounds, setChatAreaBounds] = useState<ChatAreaBounds | null>(
    null,
  );

  useEffect(() => {
    close();
  }, [chatId, close]);

  useEffect(() => {
    selectedTurnIdsRef.current = shareSelection.selectedTurnIds;
  }, [shareSelection.selectedTurnIds]);

  useLayoutEffect(() => {
    if (!shareSelection.active) {
      setChatAreaBounds(null);
      return;
    }
    const chatArea = document.querySelector<HTMLElement>(
      "[data-chat-messages-area]",
    );
    if (!chatArea) return;

    const updateBounds = () => {
      const { left, width } = chatArea.getBoundingClientRect();
      setChatAreaBounds({ left, width });
    };
    updateBounds();
    window.addEventListener("resize", updateBounds);
    const resizeObserver =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(updateBounds);
    resizeObserver?.observe(chatArea);
    return () => {
      window.removeEventListener("resize", updateBounds);
      resizeObserver?.disconnect();
    };
  }, [shareSelection.active]);

  const openShare = async () => {
    if (!chatId) {
      message.warning("请先打开一个已有会话");
      return;
    }
    setLoading(true);
    try {
      const options: ChatShareOptions = await chatApi.getChatShareOptions(
        chatId,
      );
      shareSelection.open(options.messages || [], options.turn_statuses || {});
    } catch {
      message.error("加载会话记录失败");
    } finally {
      setLoading(false);
    }
  };

  const getShareUrl = async () => {
    if (shareSelection.shareUrl) return shareSelection.shareUrl;
    if (!chatId || shareSelection.selectedTurnIds.length === 0) return null;
    const selectedTurnIds = [...shareSelection.selectedTurnIds];
    setGenerating(true);
    try {
      const result = await chatApi.createChatShare(chatId, selectedTurnIds);
      if (
        selectedTurnIdsRef.current.length !== selectedTurnIds.length ||
        selectedTurnIdsRef.current.some(
          (turnId, index) => turnId !== selectedTurnIds[index],
        )
      ) {
        return null;
      }
      const url = buildChatShareUrl(result.share_path);
      shareSelection.setShareUrl(url);
      return url;
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : "生成分享链接失败",
      );
      return null;
    } finally {
      setGenerating(false);
    }
  };

  const copyShareUrl = async () => {
    const url = await getShareUrl();
    if (!url) return;
    try {
      const copied = await copyToClipboard(url);
      if (!copied) throw new Error("clipboard unavailable");
      message.success("分享链接已复制");
      close();
    } catch {
      message.error("复制分享链接失败");
    }
  };

  const openShareUrl = async () => {
    const url = await getShareUrl();
    if (!url) return;
    const opened = window.open(url, "_blank", "noopener,noreferrer");
    if (!opened) {
      message.error("浏览器阻止了新窗口，请允许弹窗后重试");
      return;
    }
    close();
  };

  const selectableCount = shareSelection.selectableTurnIds.length;
  const selectedCount = shareSelection.selectedTurnIds.length;
  const selectionState = getShareSelectionState(selectableCount, selectedCount);
  const allSelected = selectionState === "all";

  const shareToolbar = shareSelection.active ? (
    <div
      className={styles.toolbar}
      role="region"
      aria-label="分享选择操作"
      style={
        chatAreaBounds
          ? {
              left: `${chatAreaBounds.left}px`,
              width: `${chatAreaBounds.width}px`,
              right: "auto",
            }
          : undefined
      }
    >
      <div className={styles.toolbarContent}>
        <div className={styles.toolbarSelection}>
          <Checkbox
            checked={allSelected}
            indeterminate={selectionState === "partial"}
            disabled={selectableCount === 0}
            onChange={(event) => shareSelection.selectAll(event.target.checked)}
          >
            全选
          </Checkbox>
          <span className={styles.selectionStatus} role="status">
            {selectableCount === 0
              ? "暂无可分享内容"
              : `已选 ${selectedCount} / ${selectableCount} 轮对话`}
          </span>
        </div>
        <div className={styles.toolbarActions}>
          <Button
            type="text"
            className={styles.toolbarAction}
            icon={<LinkOutlined />}
            loading={generating}
            disabled={selectedCount === 0}
            onClick={() => void copyShareUrl()}
          >
            复制链接
          </Button>
          <Button
            type="text"
            className={styles.toolbarAction}
            icon={<GlobalOutlined />}
            loading={generating}
            disabled={selectedCount === 0}
            onClick={() => void openShareUrl()}
          >
            浏览器打开
          </Button>
        </div>
        <Button
          className={styles.toolbarClose}
          type="text"
          aria-label="退出分享模式"
          icon={<CloseOutlined />}
          onClick={close}
        />
      </div>
    </div>
  ) : null;

  return (
    <>
      <Flex gap={8} align="center">
        <Tooltip title="分享会话" mouseEnterDelay={0.5}>
          <IconButton
            bordered={false}
            icon={<SparkShareLine />}
            loading={loading}
            onClick={() => void openShare()}
          />
        </Tooltip>
      </Flex>
      {shareToolbar ? createPortal(shareToolbar, document.body) : null}
    </>
  );
};

export default ChatActionGroup;
