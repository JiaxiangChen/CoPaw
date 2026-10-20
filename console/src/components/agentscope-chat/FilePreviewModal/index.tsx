import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Button,
  Drawer,
  Input,
  message,
  Modal,
  Tooltip,
  Spin,
  Tabs,
} from "antd";
import { ArrowLeftOutlined, FullscreenOutlined } from "@ant-design/icons";
import { SparkFalseLine, SparkDownloadLine } from "@agentscope-ai/icons";
import { IconButton } from "@agentscope-ai/design";
import {
  getFileIcon,
  getFileType,
  getContentType,
  isDynamicRenderHtmlLink,
  extractResultIdFromUrl,
  extractTemplateIdFromUrl,
} from "./fileUtils";
import Markdown from "../Markdown";
import { htmlPreviewEventsApi } from "@/api/modules/htmlPreviewEvents";
import type { HtmlPreviewTemplateType } from "@/api/types/htmlPreviewEvents";
import { useHtmlPreviewTracking } from "../HtmlPreviewTrackingContext";
import { useDynamicRender } from "../DynamicRenderContext";
import { useIframeHtmlPreviewTracking } from "./useHtmlPreviewTracking";
import type { NestedHtmlPreviewRequest } from "./htmlPreviewClickTracking";
import {
  dynamicRenderApi,
  RecordDataResponse,
  type ClawFilePlanItem,
} from "@/api/modules/dynamicRender";
import { useIframeStore } from "@/stores/iframeStore";
import type { FilePreviewPresentation } from "../FilePreviewPresentationContext";
import styles from "./index.module.less";
import { useHtmlAnnotations } from "../HtmlAnnotations/context";
import { useHtmlAnnotationController } from "../HtmlAnnotations/useHtmlAnnotationController";
import { cronAccessKey } from "@/api/cronTaskContext";
import { useCronAccessKey } from "@/hooks/useCronAccessKey";
import { useCronReportRead } from "./useCronReportRead";

let splitPreviewCount = 0;

function acquireSplitPreviewLayout() {
  splitPreviewCount += 1;
  document.documentElement.classList.add("copaw-file-preview-drawer-open");

  return () => {
    splitPreviewCount = Math.max(0, splitPreviewCount - 1);
    if (splitPreviewCount === 0) {
      document.documentElement.classList.remove(
        "copaw-file-preview-drawer-open",
      );
    }
  };
}

export interface FilePreviewModalProps {
  open: boolean;
  onClose: () => void;
  fileUrl: string;
  fileName: string;
  enableClickTracking?: boolean;
  enableListSnapshotTracking?: boolean;
  trackingListKey?: string | null;
  trackingListName?: string | null;
  defaultCustomerInfo?: Record<string, string> | null;
  rootTemplateId?: string;
  rootResultId?: string;
  custUid?: string | null;
  urlParams?: Record<string, string>;
  presentation?: FilePreviewPresentation;
  nestedPreviewMode?: "stack" | "replace";
  onBack?: () => void;
  enableAnnotations?: boolean;
}

function FilePreviewModal(props: FilePreviewModalProps) {
  const {
    open,
    onClose,
    fileUrl,
    fileName,
    enableClickTracking = false,
    enableListSnapshotTracking = true,
    trackingListKey,
    trackingListName,
    defaultCustomerInfo,
    rootResultId,
    rootTemplateId,
    custUid,
    urlParams,
    presentation = "modal",
    nestedPreviewMode = "stack",
    onBack,
    enableAnnotations = false,
  } = props;
  const iframeState = useIframeStore((state) => state);
  const { userId, bbk, pageSource, platformSource } = iframeState;
  // 从当前页面 url 上获取 actvId（活动ID）
  const {actvId, actvTyp} = useMemo(() => {
    const params = new URLSearchParams(window.location.search);
    return {
      actvId: params.get("actvId") || "",
      actvTyp: params.get("actvTyp") || "",
    }
  }, []);
  const [copied, setCopied] = useState(false);
  const [fullscreen, setFullscreen] = useState(presentation === "modal");
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [markdownContent, setMarkdownContent] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nestedPreview, setNestedPreview] =
    useState<NestedHtmlPreviewRequest | null>(null);
  const [iframeLoadKey, setIframeLoadKey] = useState(0);
  const [dynamicRenderLoading, setDynamicRenderLoading] = useState(false);
  const [isFileGenerating, setIsFileGenerating] = useState(false);
  const pollingTimerRef = useRef<NodeJS.Timeout | null>(null);
  const previewLoadGenerationRef = useRef(0);
  // 存储动态渲染的 HTML 内容（直接渲染到 div 时使用）
  const [renderedHtmlContent, setRenderedHtmlContent] = useState<string | null>(
    null,
  );
  const [canonicalHtml, setCanonicalHtml] = useState<string | null>(null);
  const [templateResult, setTemplateResult] = useState<RecordDataResponse>();
  const [templateResultKey, setTemplateResultKey] = useState("");
  const reportScopeKey = useCronAccessKey();
  // 客户多模板状态
  const [clawFilePlanList, setClawFilePlanList] = useState<ClawFilePlanItem[]>(
    [],
  );
  const [activeTemplate, setActiveTemplate] = useState<ClawFilePlanItem | null>(
    null,
  );
  const [clawPlanLoading, setClawPlanLoading] = useState(false);
  const [clawPlanFailed, setClawPlanFailed] = useState(false);
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const personEventsFlagRef = useRef<boolean>(false);
  const clawPlanInitializedRef = useRef(false);
  const cleanupCaptureClickRef = useRef<(() => void) | null>(null);
  const trackingContext = useHtmlPreviewTracking();
  const {
    renderTemplate,
    renderStaticTemplate,
    isStaticTemplate,
    templateList,
    isTemplateListLoaded,
  } = useDynamicRender();
  const fileType = useMemo(() => getFileType(fileName), [fileName]);
  const isMarkdownFile = useMemo(() => /\.mdx?$/i.test(fileName), [fileName]);
  const { icon, color } = useMemo(() => getFileIcon(fileName, 48), [fileName]);
  const isHtmlPreview = useMemo(
    () =>
      fileType === "previewable" && getContentType(fileName) === "text/html",
    [fileName, fileType],
  );
  // 判断是否为动态渲染类型
  const isDynamicRender = useMemo(
    () => isDynamicRenderHtmlLink(fileUrl),
    [fileUrl],
  );
  const resultId = useMemo(() => {
    return extractResultIdFromUrl(fileUrl) || "";
  }, [fileUrl]);
  const templateId = useMemo(
    () => extractTemplateIdFromUrl(fileUrl) || "",
    [fileUrl],
  );
  // 计算有效的 templateId 和 resultId（当 custUid 存在时使用 activeTemplate 的值）
  // 若 clawPlanFailed 为 true（接口失败或返回空），则回退到 URL 中的 templateId/resultId
  const effectiveTemplateId = custUid
    ? activeTemplate?.templateId
      ? String(activeTemplate.templateId)
      : clawPlanFailed
      ? templateId
      : ""
    : templateId;
  const effectiveResultId = custUid
    ? activeTemplate?.resultId ?? (clawPlanFailed ? resultId : "")
    : resultId;
  const reportContentKey = JSON.stringify([
    reportScopeKey,
    effectiveResultId,
    effectiveTemplateId,
  ]);
  const notifyReportReadFailure = useCallback((text: string) => {
    message.warning(text);
  }, []);
  useCronReportRead({
    open,
    taskId:
      templateResultKey === reportContentKey
        ? templateResult?.CRON_JOB_ID
        : null,
    contentKey: `${reportContentKey}:${templateResult?.TRACE_ID || ""}`,
    knownTaskId: trackingContext.cronTaskId,
    knownOrigin: trackingContext.cronTaskOrigin,
    onFailure: notifyReportReadFailure,
  });
  // 是否展示空状态：custUid 存在且接口失败/返回空，且 URL 中没有 templateId 和 resultId
  const showClawPlanEmpty =
    custUid && clawPlanFailed && !templateId && !resultId;
  // 获取当前模板配置
  const templateInfo = useMemo(() => {
    if (fileUrl && isTemplateListLoaded) {
      if (effectiveTemplateId) {
        const templateIdNum = parseInt(effectiveTemplateId, 10);
        return templateList.current.find(
          (item) => item.templateId === templateIdNum,
        );
      } else {
        return null;
      }
    }
    return null;
  }, [isTemplateListLoaded, effectiveTemplateId]);

  const templateType = useMemo<HtmlPreviewTemplateType | null>(() => {
    if (templateInfo) {
      return templateInfo.templateFlag === "main" ? "main" : "sub";
    } else {
      return custUid ? "sub" : "main";
    }
  }, [templateInfo, custUid]);

  // 获取动态渲染数据的函数（带轮询逻辑）
  // 对于静态模板（templateFlag === 'no_query'），跳过数据获取，直接渲染模板内容
  const fetchDynamicRenderData = useCallback(
    async (resultId: string, templateId: string, loadGeneration: number) => {
      if (previewLoadGenerationRef.current !== loadGeneration) return;
      const loadScope = cronAccessKey();

      try {
        const templateIdNum = parseInt(templateId, 10);

        // 静态模板（templateFlag === 'no_query'）：无需调用 /api/template/result 获取数据
        // 直接渲染模板内容，模板内容加载不受数据获取逻辑阻塞
        if (isStaticTemplate(templateIdNum)) {
          const renderedHtml = await renderStaticTemplate(templateIdNum);
          if (previewLoadGenerationRef.current !== loadGeneration) return;
          if (renderedHtml) {
            setRenderedHtmlContent(renderedHtml);
            setCanonicalHtml(enableAnnotations ? renderedHtml : null);
          } else {
            setError("静态模板渲染失败");
          }
          return;
        }

        // 非静态模板：需要先获取数据再渲染
        const res = await dynamicRenderApi.getRecordData(resultId, templateId);
        if (previewLoadGenerationRef.current !== loadGeneration) return;
        // 如果返回码不是 200，说明文件正在生成中
        if (res.code !== "200") {
          setIsFileGenerating(true);
          setDynamicRenderLoading(true);
          setLoading(true);
          // 清除之前的错误
          setError(null);
          // 设置定时器，每10秒再次查询
          if (pollingTimerRef.current) {
            clearTimeout(pollingTimerRef.current);
          }
          pollingTimerRef.current = setTimeout(() => {
            fetchDynamicRenderData(resultId, templateId, loadGeneration);
          }, 10000);
          return;
        }
        // 成功获取数据，停止轮询
        setIsFileGenerating(false);
        if (pollingTimerRef.current) {
          clearTimeout(pollingTimerRef.current);
          pollingTimerRef.current = null;
        }
        const { TRACE_ID, CRON_JOB_ID, custUid, custName, ...data } =
          res.data as RecordDataResponse;
        const renderedHtml = await renderTemplate(templateIdNum, res.data);
        if (
          previewLoadGenerationRef.current !== loadGeneration ||
          cronAccessKey() !== loadScope
        )
          return;
        if (renderedHtml) {
          setTemplateResult({
            TRACE_ID,
            CRON_JOB_ID,
            custUid,
            custName,
            ...data,
          });
          setTemplateResultKey(
            JSON.stringify([loadScope, resultId, templateId]),
          );
          setRenderedHtmlContent(renderedHtml);
          setCanonicalHtml(enableAnnotations ? renderedHtml : null);
        } else {
          setError("模板渲染失败");
        }
      } catch (err) {
        if (previewLoadGenerationRef.current !== loadGeneration) return;
        console.error("获取数据失败:", err);
        setError("数据加载失败");
        setIsFileGenerating(false);
      } finally {
        if (previewLoadGenerationRef.current === loadGeneration) {
          setLoading(false);
          setDynamicRenderLoading(false);
        }
      }
    },
    [
      renderTemplate,
      renderStaticTemplate,
      isStaticTemplate,
      isTemplateListLoaded,
      enableAnnotations,
    ],
  );

  // 当 custUid 存在时，获取客户的所有报告模板列表
  const fetchClawFilePlan = useCallback(async () => {
    if (!custUid) {
      setClawFilePlanList([]);
      setActiveTemplate(null);
      setClawPlanFailed(false);
      setClawPlanLoading(false);
      return;
    }
    setClawPlanLoading(true);
    try {
      const res = await dynamicRenderApi.getAllClawFilePlan({
        sapId: urlParams?.sapId || userId || "",
        bbkOrgId: urlParams?.bbkOrgId || bbk || "",
        custUid,
        pageSource: pageSource || "default",
        platformSource: platformSource || "default",
        actvId: actvId || "",
        actvTyp: actvTyp || "",
      });
      if (res.data?.length) {
        const sorted = [...res.data].sort((a, b) => a.sortOrder - b.sortOrder);
        const _curTemplateId = parseInt(templateId, 10);
        sorted.forEach((item, index) => {
          item.key = item.templateId + item.resultId + index;
        });
        setClawFilePlanList(sorted);
        const initial =
          sorted.find((item) => item.templateId === _curTemplateId) ||
          sorted[0];
        setActiveTemplate(initial);
        setClawPlanFailed(false);
      } else {
        setClawFilePlanList([]);
        setActiveTemplate(null);
        setClawPlanFailed(true);
      }
    } catch (err) {
      console.error("获取客户报告模板列表失败:", err);
      setClawFilePlanList([]);
      setActiveTemplate(null);
      setClawPlanFailed(true);
    } finally {
      setClawPlanLoading(false);
      clawPlanInitializedRef.current = true;
    }
  }, [custUid, templateId, userId, bbk, pageSource, platformSource, actvId, actvTyp]);

  useEffect(() => {
    fetchClawFilePlan();
  }, [fetchClawFilePlan]);

  // fetch 文件数据并创建 Blob URL 或动态渲染
  useEffect(() => {
    const loadGeneration = ++previewLoadGenerationRef.current;
    setTemplateResultKey("");
    let cancelled = false;
    const invalidateLoad = () => {
      cancelled = true;
      if (previewLoadGenerationRef.current === loadGeneration) {
        previewLoadGenerationRef.current += 1;
      }
    };

    // When custUid is present, wait for claw plan to initialize first
    if (custUid && !clawPlanInitializedRef.current) {
      return invalidateLoad;
    }

    if (open && fileType === "previewable" && fileUrl) {
      setLoading(true);
      setError(null);
      setBlobUrl(null);
      setMarkdownContent(null);
      setCanonicalHtml(null);
      setIsFileGenerating(false);

      // 清理之前的轮询定时器
      if (pollingTimerRef.current) {
        clearTimeout(pollingTimerRef.current);
        pollingTimerRef.current = null;
      }

      // 动态渲染逻辑
      if (isDynamicRender) {
        setDynamicRenderLoading(true);

        if (!effectiveResultId || !effectiveTemplateId) {
          setError("缺少必要的参数");
          setLoading(false);
          setDynamicRenderLoading(false);
          return invalidateLoad;
        }
        fetchDynamicRenderData(
          effectiveResultId,
          effectiveTemplateId,
          loadGeneration,
        );

        return invalidateLoad;
      }

      // 原有逻辑：直接加载文件
      fetch(fileUrl)
        .then(async (res) => {
          if (!res.ok) throw new Error("加载失败");

          if (isMarkdownFile) {
            const content = await res.text();
            if (!cancelled) setMarkdownContent(content);
            return;
          }

          const blob = await res.blob();
          const canonicalContent =
            enableAnnotations && isHtmlPreview ? await blob.text() : null;
          if (cancelled) return;
          if (canonicalContent !== null) {
            setCanonicalHtml(canonicalContent);
          }
          const contentType = getContentType(fileName);
          const newBlob = new Blob([blob], { type: contentType });
          const url = URL.createObjectURL(newBlob);
          setBlobUrl(url);
        })
        .catch(() => {
          if (!cancelled) setError("文件暂时无法预览");
        })
        .finally(() => {
          if (!cancelled) setLoading(false);
        });
    }

    return invalidateLoad;
  }, [
    open,
    fileType,
    fileUrl,
    fileName,
    custUid,
    enableAnnotations,
    isHtmlPreview,
    isMarkdownFile,
    isDynamicRender,
    renderTemplate,
    fetchDynamicRenderData,
    effectiveResultId,
    effectiveTemplateId,
    reportScopeKey,
  ]);

  // 清理 Blob URL
  useEffect(() => {
    return () => {
      if (blobUrl) {
        URL.revokeObjectURL(blobUrl);
      }
    };
  }, [blobUrl]);

  // 清理动态渲染的 HTML 内容
  useEffect(() => {
    return () => {
      setRenderedHtmlContent(null);
      setCanonicalHtml(null);
    };
  }, []);

  useEffect(() => {
    if (!open) {
      cleanupTrackers();
      cleanupCaptureClickRef.current?.();
      cleanupCaptureClickRef.current = null;
      setNestedPreview(null);
      setRenderedHtmlContent(null);
      setIsFileGenerating(false);
      setClawFilePlanList([]);
      setActiveTemplate(null);
      setClawPlanFailed(false);
      setClawPlanLoading(false);
      clawPlanInitializedRef.current = false;
      // 清理轮询定时器
      if (pollingTimerRef.current) {
        clearTimeout(pollingTimerRef.current);
        pollingTimerRef.current = null;
      }
    }
  }, [open]);

  useEffect(() => {
    return () => {
      cleanupTrackers();
      cleanupCaptureClickRef.current?.();
      cleanupCaptureClickRef.current = null;
      clawPlanInitializedRef.current = false;
      // 清理轮询定时器
      if (pollingTimerRef.current) {
        clearTimeout(pollingTimerRef.current);
        pollingTimerRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    // 记录满足该条件的模板加载时间，使用click的接口
    if (
      templateInfo?.templateFlag === "person-event" &&
      templateResult?.custUid &&
      templateResult?.custName &&
      !personEventsFlagRef.current
    ) {
      personEventsFlagRef.current = true;
      const payload = {
        file_url: fileUrl,
        file_name: fileName,
        button_id: "plan",
        button_name: "查看方案",
        button_text: "📋 查看方案",
        button_type: "plan",
        customer_id: templateResult?.custUid ?? null,
        customer_name: templateResult?.custName ?? null,
        customer_info: {
          customer_id: templateResult?.custUid || "",
          name: templateResult?.custName || "",
        },
        clicked_at: new Date().toISOString(),
        source_id: "RMASSIST",
        event_type: "button_click" as const,
        template_id: templateInfo?.templateId ?? null,
        result_id: effectiveResultId,
        page_source: pageSource || "default",
        platform_source: platformSource || "default",
        template_type: "sub" as const,
      };
      htmlPreviewEventsApi.recordClick(payload);
    }
  }, [
    templateResult,
    templateInfo,
    htmlPreviewEventsApi,
    effectiveResultId,
    pageSource,
    platformSource,
  ]);

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(fileUrl);
      message.success("链接已复制");
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      message.error("复制失败");
    }
  }, [fileUrl]);

  const handleDownload = useCallback(async () => {
    // 动态渲染类型的特殊下载逻辑
    if (isDynamicRender) {
      const downloadFunc = (htmlContent: string) => {
        const blob = new Blob([htmlContent], { type: "text/html" });
        const blobUrl = URL.createObjectURL(blob);

        const link = document.createElement("a");
        link.href = blobUrl;
        link.download = fileName.endsWith(".html")
          ? fileName
          : `${fileName}.html`;
        link.target = "_blank";
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);

        // 清理Blob URL
        setTimeout(() => URL.revokeObjectURL(blobUrl), 100);
      };
      try {
        if (renderedHtmlContent) {
          downloadFunc(renderedHtmlContent);
          return;
        }

        if (!effectiveTemplateId) {
          console.error("动态渲染链接缺少必要的参数: templateId");
          return;
        }

        const templateIdNum = parseInt(effectiveTemplateId, 10);

        // 静态模板：直接渲染模板内容，无需获取数据
        if (isStaticTemplate(templateIdNum)) {
          const renderedHtml = await renderStaticTemplate(templateIdNum);
          if (renderedHtml) {
            downloadFunc(renderedHtml);
          } else {
            console.error("静态模板渲染失败");
          }
          return;
        }
        if (!effectiveResultId) {
          console.error("动态渲染链接缺少必要的参数: resultId");
          return;
        }
        // 优先使用缓存数据，避免重复请求接口
        const renderData = (
          await dynamicRenderApi.getRecordData(
            effectiveResultId,
            effectiveTemplateId,
          )
        ).data;
        // 下载不是已确认展示，不借下载结果触发另一任务的自动标读。
        const renderedHtml = await renderTemplate(templateIdNum, renderData);

        if (renderedHtml) {
          // 将HTML内容转换为Blob进行下载
          downloadFunc(renderedHtml);
        } else {
          console.error("模板渲染失败");
        }
      } catch (error) {
        console.error("动态渲染下载失败:", error);
      }
    } else {
      // 普通文件的下载逻辑
      const link = document.createElement("a");
      link.href = fileUrl;
      link.download = fileName;
      link.target = "_blank";
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
    }
  }, [
    fileUrl,
    fileName,
    isDynamicRender,
    renderTemplate,
    renderStaticTemplate,
    isStaticTemplate,
    renderedHtmlContent,
    effectiveResultId,
    effectiveTemplateId,
  ]);

  const handleFullscreen = useCallback(() => {
    setFullscreen((prev) => !prev);
  }, []);

  const handleIframeLoad = useCallback(() => {
    setIframeLoadKey((key) => key + 1);
    reattachTrackersRef.current?.();
  }, []);

  const annotationContext = useHtmlAnnotations();
  const annotationEligible =
    enableAnnotations &&
    isHtmlPreview &&
    annotationContext.composerAvailable &&
    Boolean(canonicalHtml);
  const annotationController = useHtmlAnnotationController({
    iframeRef,
    enabled: annotationEligible,
    canonicalHtml,
    sourceKey: fileUrl,
    fileName,
    loadKey: iframeLoadKey,
  });
  const recordingEnabled = !trackingContext.disableEventRecording;

  const metaData = useMemo(
    () => ({
      cronTaskId:
        trackingContext.cronTaskId || (templateResult?.CRON_JOB_ID as string),
      cronTaskName:
        trackingContext.cronTaskName || templateInfo?.cron_task_name,
      fileUrl,
      fileName,
      listKey: trackingListKey,
      listName: trackingListName,
      defaultCustomerInfo,
      traceId: templateResult?.TRACE_ID as string,
      templateId: effectiveTemplateId,
      resultId: effectiveResultId,
      rootResultId,
      rootTemplateId,
      pageSource,
      platformSource,
      templateType,
    }),
    [
      fileName,
      fileUrl,
      trackingContext,
      trackingListKey,
      defaultCustomerInfo,
      trackingListName,
      templateInfo,
      templateResult,
      effectiveResultId,
      effectiveTemplateId,
      pageSource,
      platformSource,
      templateType,
    ],
  );

  const { cleanup: cleanupTrackers, reattach: reattachTrackers } =
    useIframeHtmlPreviewTracking(
      iframeRef,
      {
        metaData,
        load: recordingEnabled ? htmlPreviewEventsApi.recordClick : null,
        click: annotationController.annotationMode
          ? null
          : isHtmlPreview && enableClickTracking
          ? {
              reporter: recordingEnabled
                ? htmlPreviewEventsApi.recordClick
                : () => undefined,
              listSnapshotReporter:
                recordingEnabled && enableListSnapshotTracking
                  ? htmlPreviewEventsApi.recordListSnapshot
                  : undefined,
              onOpenNestedPreview: setNestedPreview,
              getTemplateName: (templateId: number) => {
                return templateList.current.find(
                  (t) => t.templateId === templateId,
                )?.templateName;
              },
            }
          : null,
        exposure:
          isHtmlPreview && recordingEnabled
            ? {
                reporter: htmlPreviewEventsApi.recordClick,
              }
            : null,
      },
      [isHtmlPreview, enableClickTracking, enableListSnapshotTracking],
      annotationController.annotationMode,
    );
  const reattachTrackersRef = useRef(reattachTrackers);
  reattachTrackersRef.current = reattachTrackers;

  useEffect(() => {
    if (presentation !== "drawer" || !open || fullscreen) return;
    return acquireSplitPreviewLayout();
  }, [fullscreen, open, presentation]);

  const previewHeight =
    presentation === "drawer" || presentation === "workspace"
      ? "100%"
      : fullscreen
      ? "90vh"
      : "500px";

  const renderPreviewContent = useMemo(() => {
    if (fileType === "previewable") {
      if (loading || isFileGenerating) {
        const tip = isFileGenerating
          ? "文件正在生成中，内容准备完成后，页面会自动展示最新预览"
          : dynamicRenderLoading
          ? "正在渲染报告..."
          : "加载中...";
        return <Spin tip={tip} />;
      }
      if (error) {
        return (
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              padding: "24px",
            }}
          >
            <div
              style={{
                color: "#8c8c8c",
                marginBottom: "16px",
                fontSize: "14px",
              }}
            >
              {error}
            </div>
            <IconButton icon={<SparkDownloadLine />} onClick={handleDownload}>
              下载文件查看
            </IconButton>
          </div>
        );
      }
      if (isMarkdownFile && markdownContent !== null) {
        return (
          <div
            style={{
              width: "100%",
              height: previewHeight,
              overflow: "auto",
              padding: "16px",
              boxSizing: "border-box",
              textAlign: "left",
            }}
          >
            <Markdown content={markdownContent} />
          </div>
        );
      }
      if (renderedHtmlContent) {
        return (
          <div
            className={styles.annotatablePreview}
            style={{ width: "100%", height: previewHeight }}
          >
            <iframe
              ref={iframeRef}
              srcDoc={renderedHtmlContent}
              style={{ width: "100%", height: "100%", border: "none" }}
              title="File Preview"
              onLoad={handleIframeLoad}
            />
            {annotationEligible && annotationController.annotationMode && (
              <div className={styles.annotationOverlay}>
                {annotationController.hoverRect && (
                  <span
                    className={styles.annotationHover}
                    style={annotationController.hoverRect}
                    aria-hidden="true"
                  />
                )}
                {annotationController.annotations.map((annotation, index) =>
                  annotation.rect ? (
                    <button
                      key={annotation.id}
                      type="button"
                      className={styles.annotationMarker}
                      style={{
                        left: annotation.rect.left,
                        top: annotation.rect.top,
                      }}
                      aria-label={`编辑批注 ${index + 1}`}
                      onClick={() =>
                        annotationController.editAnnotation(annotation)
                      }
                    >
                      {index + 1}
                    </button>
                  ) : null,
                )}
              </div>
            )}
          </div>
        );
      }
      if (blobUrl) {
        return (
          <div
            className={styles.annotatablePreview}
            style={{ width: "100%", height: previewHeight }}
          >
            <iframe
              ref={iframeRef}
              src={blobUrl}
              style={{ width: "100%", height: "100%", border: "none" }}
              title="File Preview"
              onLoad={handleIframeLoad}
            />
            {annotationEligible && annotationController.annotationMode && (
              <div className={styles.annotationOverlay}>
                {annotationController.hoverRect && (
                  <span
                    className={styles.annotationHover}
                    style={annotationController.hoverRect}
                    aria-hidden="true"
                  />
                )}
                {annotationController.annotations.map((annotation, index) =>
                  annotation.rect ? (
                    <button
                      key={annotation.id}
                      type="button"
                      className={styles.annotationMarker}
                      style={{
                        left: annotation.rect.left,
                        top: annotation.rect.top,
                      }}
                      aria-label={`编辑批注 ${index + 1}`}
                      onClick={() =>
                        annotationController.editAnnotation(annotation)
                      }
                    >
                      {index + 1}
                    </button>
                  ) : null,
                )}
              </div>
            )}
          </div>
        );
      }
      return null;
    }

    return (
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          padding: "24px",
          textAlign: "center",
        }}
      >
        <div style={{ marginBottom: "16px", color }}>{icon}</div>
        <div
          style={{
            fontSize: "16px",
            fontWeight: 500,
            marginBottom: "8px",
            maxWidth: "300px",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {fileName}
        </div>
        <div
          style={{ fontSize: "12px", color: "#8c8c8c", marginBottom: "16px" }}
        >
          该文件类型不支持预览
        </div>
        <IconButton icon={<SparkDownloadLine />} onClick={handleDownload}>
          下载文件
        </IconButton>
      </div>
    );
  }, [
    fileType,
    loading,
    error,
    isMarkdownFile,
    markdownContent,
    previewHeight,
    blobUrl,
    renderedHtmlContent,
    fileName,
    icon,
    color,
    handleDownload,
    handleIframeLoad,
    annotationController,
    annotationEligible,
  ]);

  const headerActions = useMemo(() => {
    const actions = [
      <Tooltip key="download" title="下载文件">
        <IconButton
          size="small"
          icon={<SparkDownloadLine />}
          onClick={handleDownload}
          bordered={false}
          aria-label="下载文件"
        />
      </Tooltip>,
    ];

    if (fileType === "previewable") {
      actions.unshift(
        <Tooltip key="fullscreen" title={fullscreen ? "退出全屏" : "全屏预览"}>
          <IconButton
            size="small"
            icon={<FullscreenOutlined />}
            onClick={handleFullscreen}
            bordered={false}
            aria-label={fullscreen ? "退出全屏" : "全屏预览"}
          />
        </Tooltip>,
      );
    }

    return actions;
  }, [
    fileType,
    handleCopy,
    handleDownload,
    handleFullscreen,
    copied,
    fullscreen,
  ]);

  const previewBody = (
    <>
      {custUid && clawPlanLoading && (
        <div className={styles.tabsLoadingWrapper}>
          <Spin size="small" />
        </div>
      )}
      {showClawPlanEmpty ? (
        <div className={styles.emptyWrapper}>
          <div className={styles.emptyIcon}>
            <svg
              className={styles.emptyIconSvg}
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
              <polyline points="7 10 12 15 17 10" />
              <line x1="12" y1="15" x2="12" y2="3" />
            </svg>
          </div>
          <div className={styles.emptyTitle}>未查询到个人报告数据</div>
          <div className={styles.emptyDesc}>
            当前暂无可用报告模板，请确认信息后重试
          </div>
        </div>
      ) : (
        renderPreviewContent
      )}
    </>
  );

  const annotationAction = annotationEligible ? (
    <Button
      size="small"
      type={annotationController.annotationMode ? "primary" : "default"}
      aria-label={annotationController.annotationMode ? "完成批注" : "添加批注"}
      onClick={() => {
        if (annotationController.annotationMode) {
          if (annotationController.completeAnnotations()) {
            message.success("批注已加入聊天输入框");
          } else {
            message.error("请先至少添加一条批注");
          }
          return;
        }
        annotationController.setAnnotationMode(true);
      }}
    >
      {annotationController.annotationMode
        ? `完成批注 (${annotationController.annotations.length})`
        : "添加批注"}
    </Button>
  ) : null;

  const annotationPanel = (
    <>
      {annotationEligible && annotationController.sourceConflict && (
        <div className={styles.annotationWarning} role="alert">
          页面源码已更新，原批注草稿已清除，请基于新页面重新批注。
        </div>
      )}
      {annotationEligible && annotationController.annotationMode && (
        <div
          className={styles.annotationPanel}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              event.stopPropagation();
              annotationController.exitAnnotationMode();
            }
          }}
        >
          <div className={styles.annotationInstruction}>
            选择页面元素并描述修改；批注不会改变当前页面。
          </div>
          {annotationController.unsupportedReason && (
            <div className={styles.annotationWarning} role="alert">
              {annotationController.unsupportedReason}
            </div>
          )}
          {annotationController.annotations.map((annotation, index) => (
            <div className={styles.annotationListItem} key={annotation.id}>
              <button
                type="button"
                onClick={() => annotationController.editAnnotation(annotation)}
              >
                {index + 1}. {annotation.comment}
              </button>
              <button
                type="button"
                aria-label={`删除批注 ${index + 1}`}
                onClick={() =>
                  annotationController.deleteAnnotation(annotation.id)
                }
              >
                删除
              </button>
            </div>
          ))}
          {annotationController.pendingTarget && (
            <div className={styles.annotationEditor}>
              <Input.TextArea
                autoFocus
                value={annotationController.comment}
                maxLength={2048}
                placeholder="描述这里需要怎样修改"
                aria-label="批注内容"
                onChange={(event) =>
                  annotationController.setComment(event.target.value)
                }
                onPressEnter={(event) => {
                  if (!event.shiftKey) {
                    event.preventDefault();
                    annotationController.saveComment();
                  }
                }}
              />
              <div className={styles.annotationEditorActions}>
                <Button
                  size="small"
                  onClick={annotationController.cancelComment}
                >
                  取消
                </Button>
                <Button
                  size="small"
                  type="primary"
                  disabled={!annotationController.comment.trim()}
                  onClick={annotationController.saveComment}
                >
                  保存批注
                </Button>
              </div>
            </div>
          )}
        </div>
      )}
    </>
  );

  const templateTabs =
    custUid && !clawPlanLoading && clawFilePlanList.length > 0 ? (
      <div className={styles.tabsWrapper}>
        <Tabs
          activeKey={activeTemplate?.key}
          onChange={(key) => {
            const next = clawFilePlanList.find((item) => item.key === key);
            if (next) setActiveTemplate(next);
          }}
          items={clawFilePlanList.map((item) => ({
            key: item.key,
            label: item.skillName,
          }))}
          size="small"
        />
      </div>
    ) : null;

  const replaceWithNestedPreview =
    nestedPreviewMode === "replace" && nestedPreview !== null;

  const nestedPreviewContent = nestedPreview ? (
    <FilePreviewModal
      open
      onClose={() => setNestedPreview(null)}
      fileUrl={nestedPreview.fileUrl}
      fileName={nestedPreview.fileName}
      enableClickTracking
      enableListSnapshotTracking={false}
      trackingListKey={nestedPreview.listKey}
      trackingListName={nestedPreview.listName}
      defaultCustomerInfo={nestedPreview.customerInfo}
      custUid={nestedPreview.custUid}
      rootTemplateId={
        templateInfo?.templateId ? String(templateInfo.templateId) : undefined
      }
      rootResultId={effectiveResultId}
      presentation={presentation}
      nestedPreviewMode={nestedPreviewMode}
      enableAnnotations={enableAnnotations}
      onBack={
        nestedPreviewMode === "replace"
          ? () => setNestedPreview(null)
          : undefined
      }
    />
  ) : null;

  if (replaceWithNestedPreview) return nestedPreviewContent;

  return (
    <>
      {presentation === "workspace" ? (
        <div className={styles.workspacePreview}>
          <header className={styles.workspacePreviewHeader}>
            {onBack && (
              <Button
                type="text"
                size="small"
                icon={<ArrowLeftOutlined />}
                onClick={onBack}
                aria-label="返回上一级预览"
              >
                返回
              </Button>
            )}
            <div className={styles.previewTitle} title={fileName}>
              {fileName}
            </div>
            <div className={styles.headerActions}>
              {annotationAction}
              {headerActions[headerActions.length - 1]}
            </div>
          </header>
          {templateTabs}
          <div className={styles.previewContent}>{previewBody}</div>
          {annotationPanel}
        </div>
      ) : presentation === "drawer" ? (
        <Drawer
          open={open}
          onClose={onClose}
          width={
            fullscreen
              ? "100vw"
              : "var(--copaw-file-preview-drawer-width, 42vw)"
          }
          rootClassName={styles.previewDrawerRoot}
          placement="right"
          mask={false}
          push={false}
          closable={false}
          title={
            <div className={styles.previewTitle} title={fileName}>
              {fileName}
            </div>
          }
          extra={
            <div className={styles.headerActions}>
              {headerActions}
              {annotationAction}
              <Tooltip title="关闭预览">
                <IconButton
                  size="small"
                  icon={<SparkFalseLine />}
                  bordered={false}
                  onClick={onClose}
                  aria-label="关闭预览"
                />
              </Tooltip>
            </div>
          }
          styles={{
            body: {
              display: "flex",
              flexDirection: "column",
              minHeight: 0,
              padding: 0,
              overflow: "hidden",
            },
          }}
        >
          {templateTabs}
          <div className={styles.previewContent}>{previewBody}</div>
          {annotationPanel}
        </Drawer>
      ) : (
        <Modal
          open={open}
          onCancel={onClose}
          footer={null}
          width={fullscreen ? 1368 : 800}
          className={styles.previewModalWrapper}
          centered
          closeIcon={
            <IconButton
              size="small"
              icon={<SparkFalseLine />}
              bordered={false}
            />
          }
          title={
            <div style={{ display: "flex", width: "100%", marginTop: "-6px" }}>
              {templateTabs}
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "12px",
                  marginRight: "32px",
                  marginLeft: "auto",
                }}
              >
                {headerActions}
              </div>
            </div>
          }
          styles={{ content: { padding: "16px 24px" } }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              minHeight: fullscreen ? "90vh" : "200px",
              flexDirection: "column",
            }}
          >
            {previewBody}
          </div>
        </Modal>
      )}
      {nestedPreviewContent}
    </>
  );
}

export default FilePreviewModal;
