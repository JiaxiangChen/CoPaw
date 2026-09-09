import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Button, Drawer, message, Modal, Tooltip, Spin, Tabs } from "antd";
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
  } = props;
  const iframeState = useIframeStore((state) => state);
  const { userId, bbk } = iframeState;
  const [copied, setCopied] = useState(false);
  const [fullscreen, setFullscreen] = useState(presentation === "modal");
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [markdownContent, setMarkdownContent] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nestedPreview, setNestedPreview] =
    useState<NestedHtmlPreviewRequest | null>(null);
  const [dynamicRenderLoading, setDynamicRenderLoading] = useState(false);
  const [isFileGenerating, setIsFileGenerating] = useState(false);
  const pollingTimerRef = useRef<NodeJS.Timeout | null>(null);
  // 存储动态渲染的 HTML 内容（直接渲染到 div 时使用）
  const [renderedHtmlContent, setRenderedHtmlContent] = useState<string | null>(
    null,
  );
  const [templateResult, setTemplateResult] = useState<RecordDataResponse>();
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

  // 获取动态渲染数据的函数（带轮询逻辑）
  // 对于静态模板（templateFlag === 'no_query'），跳过数据获取，直接渲染模板内容
  const fetchDynamicRenderData = useCallback(
    async (resultId: string, templateId: string) => {
      try {
        const templateIdNum = parseInt(templateId, 10);

        // 静态模板（templateFlag === 'no_query'）：无需调用 /api/template/result 获取数据
        // 直接渲染模板内容，模板内容加载不受数据获取逻辑阻塞
        if (isStaticTemplate(templateIdNum)) {
          const renderedHtml = await renderStaticTemplate(templateIdNum);
          if (renderedHtml) {
            setRenderedHtmlContent(renderedHtml);
          } else {
            setError("静态模板渲染失败");
          }
          return;
        }

        // 非静态模板：需要先获取数据再渲染
        const res = await dynamicRenderApi.getRecordData(resultId, templateId);
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
            fetchDynamicRenderData(resultId, templateId);
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
        if (renderedHtml) {
          setTemplateResult({
            TRACE_ID,
            CRON_JOB_ID,
            custUid,
            custName,
            ...data,
          });
          setRenderedHtmlContent(renderedHtml);
        } else {
          setError("模板渲染失败");
        }
      } catch (err) {
        console.error("获取数据失败:", err);
        setError("数据加载失败");
        setIsFileGenerating(false);
      } finally {
        setLoading(false);
        setDynamicRenderLoading(false);
      }
    },
    [
      renderTemplate,
      renderStaticTemplate,
      isStaticTemplate,
      isTemplateListLoaded,
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
  }, [custUid, templateId, userId, bbk]);

  useEffect(() => {
    fetchClawFilePlan();
  }, [fetchClawFilePlan]);

  // fetch 文件数据并创建 Blob URL 或动态渲染
  useEffect(() => {
    // When custUid is present, wait for claw plan to initialize first
    if (custUid && !clawPlanInitializedRef.current) {
      return;
    }

    if (open && fileType === "previewable" && fileUrl) {
      setLoading(true);
      setError(null);
      setBlobUrl(null);
      setMarkdownContent(null);
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
          return;
        }
        fetchDynamicRenderData(effectiveResultId, effectiveTemplateId);

        return;
      }

      // 原有逻辑：直接加载文件
      fetch(fileUrl)
        .then(async (res) => {
          if (!res.ok) throw new Error("加载失败");

          if (isMarkdownFile) {
            setMarkdownContent(await res.text());
            return;
          }

          const blob = await res.blob();
          const contentType = getContentType(fileName);
          const newBlob = new Blob([blob], { type: contentType });
          const url = URL.createObjectURL(newBlob);
          setBlobUrl(url);
        })
        .catch(() => {
          setError("文件暂时无法预览");
        })
        .finally(() => {
          setLoading(false);
        });
    }
  }, [
    open,
    fileType,
    fileName,
    isMarkdownFile,
    isDynamicRender,
    renderTemplate,
    fetchDynamicRenderData,
    effectiveResultId,
    effectiveTemplateId,
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
      };
      htmlPreviewEventsApi.recordClick(payload);
    }
  }, [templateResult, templateInfo, htmlPreviewEventsApi, effectiveResultId]);

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
        const { TRACE_ID, CRON_JOB_ID, custUid, custName, ...data } =
          renderData as RecordDataResponse;
        setTemplateResult({
          TRACE_ID,
          CRON_JOB_ID,
          custUid,
          custName,
          ...data,
        });
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
    reattachTrackersRef.current?.();
  }, []);

  const shouldRecordEvents = !trackingContext.disableEventRecording;

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
    ],
  );

  const { cleanup: cleanupTrackers, reattach: reattachTrackers } =
    useIframeHtmlPreviewTracking(
      iframeRef,
      {
        metaData,
        load: htmlPreviewEventsApi.recordClick,
        click:
          isHtmlPreview && enableClickTracking
            ? {
              reporter: shouldRecordEvents
                ? htmlPreviewEventsApi.recordClick
                : () => undefined,
              listSnapshotReporter:
                shouldRecordEvents && enableListSnapshotTracking
                  ? htmlPreviewEventsApi.recordListSnapshot
                  : undefined,
              onOpenNestedPreview: setNestedPreview,
              getTemplateName: (templateId: number) => {
                return templateList.current.find(
                  (t) => t.templateId === templateId
                )?.templateName;
              },
            }
            : null,
        exposure: isHtmlPreview
            ? {
                reporter: htmlPreviewEventsApi.recordClick,
            }
            : null,
      },
      [isHtmlPreview, enableClickTracking, enableListSnapshotTracking],
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
          <div style={{ width: "100%", height: previewHeight }}>
            <iframe
              ref={iframeRef}
              srcDoc={renderedHtmlContent}
              style={{ width: "100%", height: "100%", border: "none" }}
              title="File Preview"
              onLoad={handleIframeLoad}
            />
          </div>
        );
      }
      if (blobUrl) {
        return (
          <div style={{ width: "100%", height: previewHeight }}>
            <iframe
              ref={iframeRef}
              src={blobUrl}
              style={{ width: "100%", height: "100%", border: "none" }}
              title="File Preview"
              onLoad={handleIframeLoad}
            />
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
              {headerActions[headerActions.length - 1]}
            </div>
          </header>
          {templateTabs}
          <div className={styles.previewContent}>{previewBody}</div>
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
