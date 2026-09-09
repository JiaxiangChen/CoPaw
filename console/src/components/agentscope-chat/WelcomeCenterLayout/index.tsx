import React, {
  useState,
  useCallback,
  useRef,
  useEffect,
  useMemo,
} from "react";
import { Input, Upload, message } from "antd";
import type { GetRef, UploadFile } from "antd";
import { SparkAttachmentLine } from "@agentscope-ai/icons";
import {
  Attachments,
  type IAgentScopeRuntimeWebUIInputData,
  type IAgentScopeRuntimeWebUISenderOptions,
  useChatAnywhereInput,
} from "@/components/agentscope-chat";
import { chatApi } from "@/api/modules/chat";
import Style from "./style";
import DictationControl from "../DictationControl";
import dictationStyles from "../DictationControl/index.module.less";
import FeaturedCases from "../FeaturedCases";
import CaseDetailDrawer from "../CaseDetailDrawer";
import { featuredCasesApi } from "@/api/modules/featuredCases";
import type { FeaturedCase } from "@/api/types/featuredCases";
import type { SkillMentionsData } from "../SkillMentions/useSkillMentions";
import { SkillTokenEditor } from "../SkillMentions/SkillTokenEditor";
import ScenarioPresetSelector from "../ScenarioPresetSelector";
import type {
  ScenarioPresetCapability,
  ScenarioPresetScenario,
} from "@/api/types/scenarioPreset";
import sendIcon from "../../../assets/icons/send_highlight.svg";
import { useTranslation } from "react-i18next";
import VoiceRecorderQuickMenuItem from "@/components/GlobalVoiceRecorder/VoiceRecorderQuickMenuItem";
import { useVoiceRecorderTrigger } from "@/components/GlobalVoiceRecorder/context";
import { DESIGN_TOKENS } from "@/config/designTokens";
import ComposerQuickMenu, {
  ComposerQuickMenuItem,
} from "@/components/agentscope-chat/ComposerQuickMenu";
import quickMenuStyles from "@/components/agentscope-chat/ComposerQuickMenu/index.module.less";
import {
  appendChatInputText,
  CHAT_INPUT_APPEND_TEXT_EVENT,
  CHAT_INPUT_REPLACE_TEXT_EVENT,
  type ChatInputAppendTextPayload,
  type ChatInputReplaceTextPayload,
} from "../chatInputDraft";

const RUNTIME_INPUT_UPLOAD_FILE_EVENT = "pasteFile";
const WELCOME_INPUT_CARD_STYLE = {
  boxSizing: "border-box",
  maxWidth: "100%",
  width: `${DESIGN_TOKENS.inputCardWidth + 40}px`,
} as const;
const PLACEHOLDER_OPTIONS = [
  "告诉我你要做什么，我将召唤相应专家，为你执行...",
  "有什么要求都告诉我，我会越用越懂你...",
  "你可以给我取个名字，甚至设定我的人设...",
];

interface WelcomeCenterLayoutProps {
  greeting?: string;
  placeholder?: string;
  beforeSubmit?: IAgentScopeRuntimeWebUISenderOptions["beforeSubmit"];
  quickMenuItems?: React.ReactNode | React.ReactNode[];
  prefixItems?: React.ReactNode | React.ReactNode[];
  onSubmit: (data: IAgentScopeRuntimeWebUIInputData) => void | Promise<void>;
  onScenarioPresetSubmit?: (scenarioPresetId: string) => void;
  skillMentions?: SkillMentionsData;
}

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

export default function WelcomeCenterLayout(props: WelcomeCenterLayoutProps) {
  const {
    greeting,
    onSubmit,
    skillMentions,
    beforeSubmit,
    placeholder,
    quickMenuItems,
    prefixItems,
    onScenarioPresetSubmit,
  } = props;
  const { t } = useTranslation();
  const inputState = useChatAnywhereInput((value) => ({
    disabled: Boolean(value.disabled),
  }));
  const inputDisabled = Boolean(inputState.disabled);
  const [inputValue, setInputValue] = useState("");
  const [fileList, setFileList] = useState<UploadFile[]>([]);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [dictating, setDictating] = useState(false);
  const [drawerVisible, setDrawerVisible] = useState(false);
  const [selectedCase, setSelectedCase] = useState<FeaturedCase | null>(null);
  const [randomPlaceholder, setRandomPlaceholder] = useState("");
  const [loadingCase, setLoadingCase] = useState(false);
  const [mentionMenuContainer, setMentionMenuContainer] =
    useState<HTMLDivElement | null>(null);
  const [selectedScenario, setSelectedScenario] = useState<{
    capability: ScenarioPresetCapability;
    scenario: ScenarioPresetScenario;
  } | null>(null);
  const uploadRef = useRef<GetRef<typeof Upload>>(null);
  const voiceRecorder = useVoiceRecorderTrigger();
  const inputValueRef = useRef(inputValue);
  const fileListRef = useRef(fileList);
  const isSubmittingRef = useRef(false);
  const setCurrentInputValue = useCallback((value: string) => {
    inputValueRef.current = value;
    setInputValue(value);
  }, []);
  const setCurrentFileList = useCallback(
    (
      nextFileList: UploadFile[] | ((previous: UploadFile[]) => UploadFile[]),
    ) => {
      const next =
        typeof nextFileList === "function"
          ? nextFileList(fileListRef.current)
          : nextFileList;
      fileListRef.current = next;
      setFileList(next);
    },
    [],
  );
  const effectiveSkillMentions = useMemo<SkillMentionsData>(
    () =>
      skillMentions || {
        items: [],
        selected: [],
        loading: false,
        error: false,
        onOpen: () => undefined,
        onChange: () => undefined,
        onRetry: () => undefined,
      },
    [skillMentions],
  );

  // 组件挂载时随机选择placeholder文案
  useEffect(() => {
    const randomIndex = Math.floor(Math.random() * PLACEHOLDER_OPTIONS.length);
    setRandomPlaceholder(PLACEHOLDER_OPTIONS[randomIndex]);
  }, []);

  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<ChatInputAppendTextPayload>).detail;
      if (typeof detail?.content !== "string" || !detail.content) {
        return;
      }

      setCurrentInputValue(
        appendChatInputText(inputValueRef.current, detail.content),
      );
    };

    const replaceHandler = (event: Event) => {
      const detail = (event as CustomEvent<ChatInputReplaceTextPayload>).detail;
      if (typeof detail?.content !== "string" || !detail.content) {
        return;
      }

      setCurrentInputValue(detail.content);
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
  }, [setCurrentInputValue]);

  const handleSend = useCallback(async () => {
    if (isSubmittingRef.current) return;

    const submittedInputValue = inputValueRef.current;
    const promptDraft = submittedInputValue.trim();
    const capabilityMarker = selectedScenario
      ? `@${selectedScenario.capability.name}`
      : "";
    const query = [capabilityMarker, promptDraft].filter(Boolean).join(" ");
    if (!query) return;
    const uploadedFiles = fileListRef.current.filter((file) =>
      Boolean(file.response?.url),
    );
    const inputData: IAgentScopeRuntimeWebUIInputData = {
      query,
      fileList: uploadedFiles,
    };

    isSubmittingRef.current = true;
    setIsSubmitting(true);

    try {
      const next = beforeSubmit ? await beforeSubmit(inputData) : inputData;
      if (!next) return;
      if (isSubmitCancelled(next)) {
        if (next.clearInput) {
          setCurrentInputValue("");
          setCurrentFileList([]);
        }
        return;
      }

      if (selectedScenario) {
        onScenarioPresetSubmit?.(selectedScenario.scenario.id);
      }
      await Promise.resolve(
        onSubmit(typeof next === "object" ? next : inputData),
      );
      if (inputValueRef.current === submittedInputValue) {
        setCurrentInputValue("");
      }
      setCurrentFileList((currentFiles) =>
        currentFiles.filter(
          (file) => !uploadedFiles.some(({ uid }) => uid === file.uid),
        ),
      );
    } finally {
      isSubmittingRef.current = false;
      setIsSubmitting(false);
    }
  }, [
    beforeSubmit,
    onScenarioPresetSubmit,
    onSubmit,
    selectedScenario,
    setCurrentFileList,
    setCurrentInputValue,
  ]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLElement>) => {
      if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend],
  );

  const handleFillInput = useCallback(
    (text: string) => {
      if (inputDisabled) return;
      setCurrentInputValue(text);
    },
    [inputDisabled, setCurrentInputValue],
  );

  const handleScenarioSelect = useCallback(
    ({
      capability,
      scenario,
    }: {
      capability: ScenarioPresetCapability;
      scenario: ScenarioPresetScenario;
    }) => {
      setSelectedScenario({ capability, scenario });
      setCurrentInputValue(scenario.prompt_draft);
    },
    [setCurrentInputValue],
  );

  // Handle "看案例" click - fetch detail from API
  const handleViewCase = useCallback(async (id: number) => {
    setLoadingCase(true);
    setDrawerVisible(true);
    setSelectedCase(null); // Clear previous case

    try {
      const caseData = await featuredCasesApi.getCaseDetail(id);
      setSelectedCase(caseData);
    } catch (error) {
      console.error("Failed to load case detail:", error);
      // Close drawer on error
      setDrawerVisible(false);
    } finally {
      setLoadingCase(false);
    }
  }, []);

  const handleCloseDrawer = useCallback(() => {
    setDrawerVisible(false);
    setSelectedCase(null);
  }, []);

  // Handle file upload - use chatApi to upload files (same as bottom Input)
  const handleBeforeUpload = useCallback(
    (file: File) => {
      if (inputDisabled) return false;
      const uid = `welcome-${Date.now()}-${Math.random()
        .toString(36)
        .slice(2)}`;
      const uploadFile: UploadFile = {
        uid,
        name: file.name,
        size: file.size,
        type: file.type,
        status: "uploading",
        percent: 0,
        originFileObj: file as UploadFile["originFileObj"],
      };

      setCurrentFileList((prev) => [...prev, uploadFile]);

      // If it's an image, generate thumbnail for preview
      if (file.type.startsWith("image/")) {
        const reader = new FileReader();
        reader.onload = (e) => {
          const dataUrl = e.target?.result;
          if (typeof dataUrl === "string") {
            setCurrentFileList((prev) =>
              prev.map((f) =>
                f.uid === uid ? { ...f, thumbUrl: dataUrl } : f,
              ),
            );
          }
        };
        reader.readAsDataURL(file);
      }

      // Actually upload the file using chatApi
      chatApi
        .uploadFile(file)
        .then((res) => {
          // Upload succeeded, update with URL
          setCurrentFileList((prev) =>
            prev.map((f) =>
              f.uid === uid
                ? {
                    ...f,
                    status: "done" as const,
                    percent: 100,
                    response: { url: chatApi.filePreviewUrl(res.url) },
                  }
                : f,
            ),
          );
        })
        .catch((error) => {
          console.error("File upload failed:", error);
          message.error(t("chat.attachments.uploadFailed"));
          // Mark as error and remove from list
          setCurrentFileList((prev) => prev.filter((f) => f.uid !== uid));
        });

      return false; // Prevent default upload behavior
    },
    [inputDisabled, setCurrentFileList, t],
  );

  const mergedQuickMenuItems = useMemo(() => {
    const externalItems =
      React.Children.toArray(quickMenuItems).filter(Boolean);
    const uploadItem = (
      <div
        key="welcome-upload"
        className={quickMenuStyles.uploadTrigger}
        onClick={(event) => event.stopPropagation()}
      >
        <Upload
          ref={uploadRef}
          showUploadList={false}
          accept="*/*"
          beforeUpload={handleBeforeUpload}
          disabled={inputDisabled || isSubmitting}
        >
          <ComposerQuickMenuItem
            icon={<SparkAttachmentLine />}
            interactive
            label={t("chat.quickMenu.upload", "上传文件")}
          />
        </Upload>
      </div>
    );

    return [
      uploadItem,
      voiceRecorder ? (
        <VoiceRecorderQuickMenuItem
          key="voice-recorder"
          control={voiceRecorder}
        />
      ) : null,
      ...externalItems,
    ].filter(Boolean);
  }, [
    handleBeforeUpload,
    inputDisabled,
    isSubmitting,
    quickMenuItems,
    t,
    voiceRecorder,
  ]);

  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<{ file?: File }>).detail;
      if (detail?.file instanceof File) {
        handleBeforeUpload(detail.file);
      }
    };

    document.addEventListener(RUNTIME_INPUT_UPLOAD_FILE_EVENT, handler);
    return () =>
      document.removeEventListener(RUNTIME_INPUT_UPLOAD_FILE_EVENT, handler);
  }, [handleBeforeUpload]);

  return (
    <>
      <Style />
      <div className="welcome-center-layout">
        {/* Greeting */}
        <div className="welcome-greeting">{greeting}</div>

        <ScenarioPresetSelector
          disabled={inputDisabled || isSubmitting}
          onBrowseChange={() => setSelectedScenario(null)}
          onSelect={handleScenarioSelect}
          selectedScenarioId={selectedScenario?.scenario.id}
        >
          {({
            capability,
            onScenarioSelect,
            scenarios,
            selectedScenarioId,
          }) => (
            <div
              className="welcome-input-card"
              ref={setMentionMenuContainer}
              style={WELCOME_INPUT_CARD_STYLE}
            >
              {capability && scenarios.length > 0 && (
                <div className="welcome-scene-strip" aria-label="推荐场景">
                  <span className="welcome-scene-title">推荐场景</span>
                  <div className="welcome-scene-list">
                    {scenarios.map((scenario) => (
                      <button
                        key={scenario.id}
                        aria-pressed={selectedScenarioId === scenario.id}
                        className={`welcome-scene-button${
                          selectedScenarioId === scenario.id ? " is-active" : ""
                        }`}
                        disabled={inputDisabled || isSubmitting}
                        onClick={() => onScenarioSelect(scenario)}
                        type="button"
                      >
                        {scenario.name}
                      </button>
                    ))}
                  </div>
                </div>
              )}
              {/* Attachment preview area */}
              {fileList.length > 0 && (
                <div
                  style={{ marginBottom: -8, marginTop: -8, marginLeft: -20 }}
                >
                  <Attachments
                    items={fileList}
                    onChange={(info) => setCurrentFileList(info.fileList)}
                  />
                </div>
              )}

              {skillMentions || selectedScenario ? (
                <SkillTokenEditor
                  aria-label="消息"
                  className="welcome-input-placeholder welcome-skill-editor"
                  disabled={inputDisabled || isSubmitting}
                  fixedToken={
                    selectedScenario
                      ? {
                          text: `@${selectedScenario.capability.name}`,
                          onRemove: () => setSelectedScenario(null),
                        }
                      : undefined
                  }
                  mentionMenuContainer={mentionMenuContainer}
                  mentionMenuPlacement="bottom"
                  onKeyDown={dictating ? undefined : handleKeyDown}
                  onValueChange={setCurrentInputValue}
                  placeholder={placeholder || randomPlaceholder}
                  skillMentions={effectiveSkillMentions}
                  value={inputValue}
                />
              ) : (
                <Input.TextArea
                  className="welcome-input-placeholder"
                  value={inputValue}
                  onChange={(event) => setCurrentInputValue(event.target.value)}
                  onKeyDown={dictating ? undefined : handleKeyDown}
                  placeholder={placeholder || randomPlaceholder}
                  autoSize={{ minRows: 1, maxRows: 5 }}
                  bordered={false}
                  disabled={inputDisabled || isSubmitting}
                />
              )}
              <div
                className={`welcome-input-actions ${dictationStyles.toolbar}`}
              >
                {!dictating && (
                  <div className="welcome-input-actions-left">
                    <ComposerQuickMenu
                      disabled={inputDisabled || isSubmitting}
                      triggerLabel={t("chat.quickMenu.trigger", "快捷操作")}
                    >
                      {mergedQuickMenuItems}
                    </ComposerQuickMenu>
                    {prefixItems}
                  </div>
                )}
                {!dictating && <span className={dictationStyles.prefix} />}
                <DictationControl
                  disabled={
                    inputDisabled || isSubmitting || voiceRecorder?.recording
                  }
                  onActiveChange={setDictating}
                  onTranscript={(text) =>
                    setCurrentInputValue(
                      appendChatInputText(inputValueRef.current, text),
                    )
                  }
                />
                <button
                  className={`welcome-input-send-btn ${dictationStyles.send}`}
                  onClick={handleSend}
                  disabled={
                    inputDisabled ||
                    dictating ||
                    isSubmitting ||
                    (!inputValue.trim() && !selectedScenario)
                  }
                  type="button"
                >
                  <img src={sendIcon} alt="发送" width={24} height={24} />
                </button>
              </div>
            </div>
          )}
        </ScenarioPresetSelector>

        {/* Featured Cases */}
        <div className="welcome-cases-area">
          <FeaturedCases
            onFillInput={handleFillInput}
            onViewCase={handleViewCase}
          />
        </div>
      </div>

      {/* Case Detail Drawer */}
      <CaseDetailDrawer
        visible={drawerVisible}
        onClose={handleCloseDrawer}
        caseData={selectedCase}
        loading={loadingCase}
        onMakeSimilar={(value) => {
          setCurrentInputValue(value);
          handleCloseDrawer();
        }}
      />
    </>
  );
}
