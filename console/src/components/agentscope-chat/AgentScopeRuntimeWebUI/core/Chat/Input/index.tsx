import React, { useCallback, useEffect, useMemo, useRef } from "react";
import {
  useProviderContext,
  ChatInput,
  Disclaimer,
} from "@/components/agentscope-chat";
import { useChatAnywhereOptions } from "../../Context/ChatAnywhereOptionsContext";
import { useGetState } from "ahooks";
import { useChatAnywhereInput } from "../../Context/ChatAnywhereInputContext";
import { ChatAnywhereSessionsContext } from "../../Context/ChatAnywhereSessionsContext";
import useAttachments from "./useAttachments";
import { IAgentScopeRuntimeWebUIInputData } from "@/components/agentscope-chat";
import {
  RUNTIME_INPUT_SET_CONTENT_EVENT,
  type RuntimeInputRestorePayload,
} from "../hooks/followUpSubmit";
import { ChatAnywhereMessagesContext } from "../../Context/ChatAnywhereMessagesContext";
import { useContextSelector } from "use-context-selector";
import { useTranslation } from "react-i18next";
import VoiceRecorderQuickMenuItem from "@/components/GlobalVoiceRecorder/VoiceRecorderQuickMenuItem";
import ComposerQuickMenu from "@/components/agentscope-chat/ComposerQuickMenu";
import {
  appendChatInputText,
  CHAT_INPUT_APPEND_TEXT_EVENT,
  CHAT_INPUT_REPLACE_TEXT_EVENT,
  type ChatInputAppendTextPayload,
  type ChatInputReplaceTextPayload,
} from "@/components/agentscope-chat/chatInputDraft";
import { useVoiceRecorderTrigger } from "@/components/GlobalVoiceRecorder/context";
import { useChatShareSelection } from "@/pages/Chat/chatShareContext";

const RUNTIME_INPUT_UPLOAD_FILE_EVENT = "pasteFile";

function isSubmitCancelled(result: unknown): result is {
  shouldSubmit: false;
  clearInput?: boolean;
} {
  return (
    Boolean(result) &&
    typeof result === "object" &&
    (result as { shouldSubmit?: unknown }).shouldSubmit === false
  );
}

export interface InputProps {
  onCancel: () => void;
  onSubmit: (data: IAgentScopeRuntimeWebUIInputData) => void;
}

export default function Input({ onCancel, onSubmit }: InputProps) {
  const { t } = useTranslation();
  const { active: sharing } = useChatShareSelection();
  const sharingRef = useRef(sharing);
  sharingRef.current = sharing;
  const [content, setContent, getContent] = useGetState("");
  const restoredBizParamsRef =
    useRef<IAgentScopeRuntimeWebUIInputData["biz_params"]>(undefined);
  const prefixCls = useProviderContext().getPrefixCls("chat-anywhere-input");
  const senderOptions = useChatAnywhereOptions((v) => v.sender);
  const inputContext = useChatAnywhereInput((v) => v);
  const messages = useContextSelector(
    ChatAnywhereMessagesContext,
    (v) => v.messages,
  );
  const hasMessages = messages && messages.length > 0;
  const currentSessionId = useContextSelector(
    ChatAnywhereSessionsContext,
    (v) => v.currentSessionId,
  );

  const {
    placeholder = "",
    disclaimer = "",
    maxLength,
    beforeSubmit = async () => true,
    beforeUI,
    afterUI,
    renderComposer,
    attachments,
    prefix,
    quickMenuItems,
    allowSpeech,
    suggestions,
    skillMentions,
  } = senderOptions || {};

  const {
    fileList,
    getFileList,
    setFileList,
    handlePasteFile,
    uploadQuickMenuItem,
    uploadFileListHeader,
  } = useAttachments(attachments, {
    disabled: !!inputContext.disabled || sharing,
  });
  const canHandlePasteFile =
    inputContext.disabled || sharing ? undefined : handlePasteFile;
  const voiceRecorder = useVoiceRecorderTrigger();

  const mergedQuickMenuItems = useMemo(() => {
    const externalItems =
      React.Children.toArray(quickMenuItems).filter(Boolean);
    return [
      uploadQuickMenuItem,
      voiceRecorder ? (
        <VoiceRecorderQuickMenuItem
          key="voice-recorder"
          control={voiceRecorder}
        />
      ) : null,
      ...externalItems,
    ].filter(Boolean);
  }, [quickMenuItems, uploadQuickMenuItem, voiceRecorder]);

  // Clear attachments when session changes
  useEffect(() => {
    if (setFileList) {
      setFileList([]);
    }
  }, [currentSessionId, setFileList]);

  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<RuntimeInputRestorePayload>).detail;
      const nextContent = detail?.content;
      if (typeof nextContent !== "string") {
        return;
      }

      setContent(nextContent);

      if (
        Object.prototype.hasOwnProperty.call(detail, "fileList") &&
        setFileList
      ) {
        setFileList(detail.fileList || []);
      }

      if (Object.prototype.hasOwnProperty.call(detail, "biz_params")) {
        restoredBizParamsRef.current = detail.biz_params;
      } else {
        restoredBizParamsRef.current = undefined;
      }
    };

    document.addEventListener(RUNTIME_INPUT_SET_CONTENT_EVENT, handler);
    return () =>
      document.removeEventListener(RUNTIME_INPUT_SET_CONTENT_EVENT, handler);
  }, [setContent, setFileList]);

  useEffect(() => {
    if (!hasMessages) {
      return;
    }

    const handler = (event: Event) => {
      const detail = (event as CustomEvent<ChatInputAppendTextPayload>).detail;
      if (typeof detail?.content !== "string" || !detail.content) {
        return;
      }

      restoredBizParamsRef.current = undefined;
      setContent(appendChatInputText(getContent(), detail.content));
    };

    const replaceHandler = (event: Event) => {
      const detail = (event as CustomEvent<ChatInputReplaceTextPayload>).detail;
      if (typeof detail?.content !== "string" || !detail.content) {
        return;
      }

      restoredBizParamsRef.current = undefined;
      setContent(detail.content);
    };

    document.addEventListener(CHAT_INPUT_APPEND_TEXT_EVENT, handler);
    document.addEventListener(CHAT_INPUT_REPLACE_TEXT_EVENT, replaceHandler);
    return () => {
      document.removeEventListener(CHAT_INPUT_APPEND_TEXT_EVENT, handler);
      document.removeEventListener(
        CHAT_INPUT_REPLACE_TEXT_EVENT,
        replaceHandler,
      );
    };
  }, [getContent, hasMessages, setContent]);

  useEffect(() => {
    if (!canHandlePasteFile) {
      return;
    }

    const handler = (event: Event) => {
      const detail = (event as CustomEvent<{ file?: File }>).detail;
      if (detail?.file instanceof File) {
        canHandlePasteFile(detail.file);
      }
    };

    document.addEventListener(RUNTIME_INPUT_UPLOAD_FILE_EVENT, handler);
    return () =>
      document.removeEventListener(RUNTIME_INPUT_UPLOAD_FILE_EVENT, handler);
  }, [canHandlePasteFile]);

  const handleContentChange = useCallback(
    (value: string) => {
      restoredBizParamsRef.current = undefined;
      setContent(value);
    },
    [setContent],
  );

  const handleSubmit = useCallback(async () => {
    if (sharingRef.current) return;
    const fileList = (getFileList?.() || []).filter((i) => i.response?.url);
    const inputData: IAgentScopeRuntimeWebUIInputData = {
      query: getContent(),
      fileList,
      biz_params: restoredBizParamsRef.current,
    };
    const next = await beforeSubmit(inputData);
    if (!next || sharingRef.current) return;

    if (isSubmitCancelled(next)) {
      if (next.clearInput) {
        setContent("");
        restoredBizParamsRef.current = undefined;
        if (setFileList) {
          setFileList([]);
        }
      }
      return;
    }

    onSubmit(typeof next === "object" ? next : inputData);
    setContent("");
    restoredBizParamsRef.current = undefined;
    if (setFileList) {
      setFileList([]);
    }
  }, [
    beforeSubmit,
    getContent,
    getFileList,
    onSubmit,
    setContent,
    setFileList,
  ]);

  const handleCancel = useCallback(() => {
    onCancel();
  }, [onCancel]);

  const defaultComposer = (
    <ChatInput
      key={`${currentSessionId}:${Boolean(hasMessages || fileList.length)}`}
      loading={inputContext.loading}
      disabled={inputContext.disabled || sharing}
      placeholder={placeholder}
      value={content}
      prefix={
        <>
          <ComposerQuickMenu
            disabled={Boolean(inputContext.disabled)}
            triggerLabel={t("chat.quickMenu.trigger", "快捷操作")}
          >
            {mergedQuickMenuItems}
          </ComposerQuickMenu>
          {prefix}
        </>
      }
      header={fileList.length > 0 ? uploadFileListHeader : undefined}
      onChange={handleContentChange}
      maxLength={maxLength}
      onSubmit={handleSubmit}
      onCancel={handleCancel}
      allowSpeech={allowSpeech && !voiceRecorder?.recording}
      onPasteFile={canHandlePasteFile}
      suggestions={suggestions}
      skillMentions={skillMentions}
    />
  );
  const renderedComposer = renderComposer
    ? renderComposer(defaultComposer)
    : defaultComposer;

  return (
    <div
      className={prefixCls}
      aria-hidden={sharing || undefined}
      style={
        sharing
          ? { visibility: "hidden", opacity: 0, pointerEvents: "none" }
          : undefined
      }
    >
      <div
        className={`${prefixCls}-wrapper`}
        style={{
          display: hasMessages || fileList.length > 0 ? "block" : "none",
        }}
      >
        {beforeUI}
        {renderedComposer}
        {afterUI}
      </div>
      {disclaimer ? (
        <Disclaimer desc={disclaimer} />
      ) : (
        <div className={`${prefixCls}-blank`}></div>
      )}
    </div>
  );
}
