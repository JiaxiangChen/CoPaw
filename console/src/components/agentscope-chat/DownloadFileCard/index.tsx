import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { message, Spin } from "antd";
import { SparkDownloadLine } from "@agentscope-ai/icons";
import FilePreviewModal from "../FilePreviewModal";
import FilePreviewDrawer from "../FilePreviewDrawer";
import { useFilePreviewPresentation } from "../FilePreviewPresentationContext";
import { emitChatWorkspaceFile } from "../FileWorkspaceEvents";
import {
  extractDecodedFileNameFromUrl,
  extractResultIdFromUrl,
  extractTemplateIdFromUrl,
  getFileIcon,
  getFileType,
  isAutoPreviewHtmlLink,
  isDynamicRenderHtmlLink,
  safeDecodeFileName,
} from "../FilePreviewModal/fileUtils";
import { useAutoPreviewHtml } from "../AutoPreviewHtmlContext";
import { useDynamicRender } from "../DynamicRenderContext";
import { dynamicRenderApi } from "@/api/modules/dynamicRender";

export interface DownloadFileCardProps {
  url: string;
  fileName?: string;
  autoPreview?: boolean;
  enableClickTracking?: boolean;
  className?: string;
  style?: React.CSSProperties;
  hideLoadBtn?: boolean;
}

const EMPTY = "\u00A0";

// 内联样式定义
const cardStyle: React.CSSProperties = {
  position: "relative",
  display: "flex",
  alignItems: "center",
  padding: "12px 16px",
  background: "#fff",
  border: "1px solid #d9d9d9",
  borderRadius: "8px",
  cursor: "pointer",
  transition: "all 0.3s",
  maxWidth: "280px",
  overflow: "hidden",
};

const iconStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  width: "24px",
  height: "24px",
  marginRight: "8px",
  flexShrink: 0,
};

const contentStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  display: "flex",
  flexDirection: "column",
  gap: "4px",
};

const nameStyle: React.CSSProperties = {
  fontSize: "14px",
  fontWeight: 500,
  color: "#262626",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const hintStyle: React.CSSProperties = {
  fontSize: "12px",
  color: "#8c8c8c",
};

const downloadBtnStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  width: "24px",
  height: "24px",
  background: "#1677ff",
  borderRadius: "4px",
  color: "#fff",
  cursor: "pointer",
  flexShrink: 0,
  marginLeft: "8px",
};

function DownloadFileCard(props: DownloadFileCardProps) {
  const {
    url,
    fileName: propFileName,
    autoPreview,
    enableClickTracking = false,
    className,
    style,
    hideLoadBtn,
  } = props;
  const [previewOpen, setPreviewOpen] = useState(false);
  const autoPreviewOpenedRef = useRef(false);
  const { enabled: pageAutoPreviewEnabled, register: registerAutoPreview } =
    useAutoPreviewHtml();
  const [isDownloadingGenerating, setIsDownloadingGenerating] = useState(false);
  const pollingTimerRef = useRef<NodeJS.Timeout | null>(null);
  const { renderTemplate } = useDynamicRender();
  const previewPresentation = useFilePreviewPresentation();

  // Extract filename from URL if not provided
  const fileName = useMemo(() => {
    if (propFileName) return safeDecodeFileName(propFileName);
    return extractDecodedFileNameFromUrl(url, "未知文件");
  }, [url, propFileName]);

  const { icon } = useMemo(() => getFileIcon(fileName), [fileName]);

  // Split filename for display
  const [namePrefix, nameSuffix] = useMemo(() => {
    const match = fileName.match(/^(.*)\.[^.]+$/);
    return match ? [match[1], fileName.slice(match[1].length)] : [fileName, ""];
  }, [fileName]);

  const fileType = useMemo(() => getFileType(fileName), [fileName]);
  // 动态渲染类型也支持自动预览
  const isDynamicRender = useMemo(() => isDynamicRenderHtmlLink(url), [url]);
  const shouldAutoPreview = useMemo(
    () =>
      autoPreview ??
      (pageAutoPreviewEnabled &&
        (isAutoPreviewHtmlLink(url, fileName) || isDynamicRender)),
    [autoPreview, pageAutoPreviewEnabled, url, fileName, isDynamicRender],
  );
  const isAutoPreviewHtml = useMemo(
    () => isAutoPreviewHtmlLink(url, fileName),
    [fileName, url],
  );
  const shouldEnableClickTracking =
    enableClickTracking || isAutoPreviewHtml || isDynamicRender;

  const openWorkspacePreview = useCallback(() => {
    emitChatWorkspaceFile({
      action: "open",
      fileName,
      fileUrl: url,
      enableClickTracking: shouldEnableClickTracking,
    });
  }, [fileName, shouldEnableClickTracking, url]);

  useEffect(() => {
    if (previewPresentation !== "workspace") return;
    emitChatWorkspaceFile({
      action: "register",
      fileName,
      fileUrl: url,
      enableClickTracking: shouldEnableClickTracking,
    });
  }, [fileName, previewPresentation, shouldEnableClickTracking, url]);

  useEffect(() => {
    if (
      !shouldAutoPreview ||
      autoPreviewOpenedRef.current ||
      fileType !== "previewable"
    ) {
      return;
    }

    if (autoPreview === undefined && pageAutoPreviewEnabled) {
      return registerAutoPreview({
        url,
        open: () => {
          autoPreviewOpenedRef.current = true;
          if (previewPresentation === "workspace") {
            openWorkspacePreview();
          } else {
            setPreviewOpen(true);
          }
        },
      });
    }

    autoPreviewOpenedRef.current = true;
    if (previewPresentation === "workspace") {
      openWorkspacePreview();
    } else {
      setPreviewOpen(true);
    }
  }, [
    autoPreview,
    fileType,
    openWorkspacePreview,
    pageAutoPreviewEnabled,
    previewPresentation,
    registerAutoPreview,
    shouldAutoPreview,
    url,
  ]);

  const handlePreview = () => {
    if (previewPresentation === "workspace") {
      openWorkspacePreview();
      return;
    }
    setPreviewOpen(true);
  };

  // 轮询获取动态渲染数据直到成功
  const pollForData = async (
    resultId: string,
    templateId: string,
    onSuccess: (res: Record<string, unknown>) => Promise<void>,
  ): Promise<void> => {
    const res = await dynamicRenderApi.getRecordData(resultId, templateId);

    if (res.code === "200") {
      await onSuccess(res.data as Record<string, unknown>);
      return;
    }

    // 文件正在生成中，显示提示并继续轮询
    setIsDownloadingGenerating(true);
    message.loading({
      content: "文件正在生成中，内容准备完成后，会自动下载",
      key: "fileGenerating",
      duration: 0,
    });

    // 10秒后继续轮询
    pollingTimerRef.current = setTimeout(() => {
      pollForData(resultId, templateId, onSuccess);
    }, 10000);
  };

  const handleDownload = async (e: React.MouseEvent) => {
    e.stopPropagation(); // 阻止事件冒泡，避免打开弹窗

    // 清理之前的轮询定时器
    if (pollingTimerRef.current) {
      clearTimeout(pollingTimerRef.current);
      pollingTimerRef.current = null;
    }

    // 动态渲染类型的特殊下载逻辑
    if (isDynamicRender) {
      try {
        const resultId = extractResultIdFromUrl(url);
        const templateId = extractTemplateIdFromUrl(url);

        if (!resultId || !templateId) {
          console.error("动态渲染链接缺少必要的参数");
          return;
        }

        // 执行下载的函数
        const performDownload = async (res: Record<string, unknown>) => {
          const templateIdNum = parseInt(templateId, 10);
          const renderedHtml = await renderTemplate(templateIdNum, res);

          if (renderedHtml) {
            // 将HTML内容转换为Blob进行下载
            const blob = new Blob([renderedHtml], { type: "text/html" });
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
            setIsDownloadingGenerating(false);
          } else {
            console.error("模板渲染失败");
            message.error("模板渲染失败");
          }
        };

        // 使用轮询函数获取数据并下载
        await pollForData(resultId, templateId, async (res) => {
          message.success({
            content: "文件已生成，正在下载...",
            key: "fileGenerating",
          });
          await performDownload(res);
        });
      } catch (error) {
        console.error("动态渲染下载失败:", error);
        message.error("文件下载失败");
        setIsDownloadingGenerating(false);
      }
    } else {
      // 普通文件的下载逻辑
      const link = document.createElement("a");
      link.href = url;
      link.download = fileName;
      link.target = "_blank";
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
    }
  };

  // 合并样式
  const mergedCardStyle = {
    ...cardStyle,
    borderColor: "#d9d9d9",
    ...style,
  };

  const mergedHintStyle = {
    ...hintStyle,
    color: fileType === "previewable" ? "#1677ff" : "#8c8c8c",
  };

  // 组件卸载时清理轮询定时器
  useEffect(() => {
    return () => {
      if (pollingTimerRef.current) {
        clearTimeout(pollingTimerRef.current);
        pollingTimerRef.current = null;
      }
    };
  }, []);

  const hintText = fileType === "previewable" ? "点击预览" : "不支持预览";
  const PreviewComponent =
    previewPresentation === "drawer" ? FilePreviewDrawer : FilePreviewModal;

  return (
    <>
      <div
        className={className}
        style={mergedCardStyle}
        onClick={handlePreview}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            handlePreview();
          }
        }}
      >
        <div style={iconStyle}>{icon}</div>
        <div style={contentStyle}>
          <div style={nameStyle}>
            {namePrefix || EMPTY}
            {nameSuffix}
          </div>
          <div style={mergedHintStyle}>{hintText}</div>
        </div>
        {/* 直接下载按钮 */}
        {!hideLoadBtn && (
          <div style={downloadBtnStyle} onClick={handleDownload} title="下载">
            <SparkDownloadLine style={{ fontSize: "14px" }} />
          </div>
        )}
      </div>
      {previewPresentation !== "workspace" && (
        <PreviewComponent
          open={previewOpen}
          onClose={() => setPreviewOpen(false)}
          fileUrl={url}
          fileName={fileName}
          enableClickTracking={shouldEnableClickTracking}
        />
      )}
    </>
  );
}

export default DownloadFileCard;
