/**
 * 注意 ！！！此文件需要单独对比合并
 */
import type { ComponentPropsWithoutRef, MouseEvent } from "react";
import { useProviderContext } from "@/components/agentscope-chat";
import DownloadFileCard from "../../../DownloadFileCard";
import {
  extractDecodedFileNameFromUrl,
  isAutoPreviewHtmlLink,
  safeDecodeFileName,
} from "../../../FilePreviewModal/fileUtils";
// import { Base64 } from 'js-base64'

interface LinkProps extends ComponentPropsWithoutRef<"a"> {
  "data-footnote-ref"?: string;
  "data-footnote-backref"?: string;
}

const CUSTOMER_INSIGHT_HOST = "xxxx.yyyy.zz.cn";

const FILE_EXTENSIONS = new Set([
  "png",
  "jpg",
  "jpeg",
  "gif",
  "bmp",
  "webp",
  "svg",
  "mp4",
  "webm",
  "mp3",
  "wav",
  "ogg",
  "pdf",
  "html",
  "htm",
  "doc",
  "docx",
  "xls",
  "xlsx",
  "ppt",
  "pptx",
  "md",
  "mdx",
  "txt",
  "json",
  "xml",
  "csv",
  "log",
  "yaml",
  "yml",
  "zip",
  "rar",
  "7z",
  "tar",
  "gz",
]);

// 判断是否为文件链接
function isFileLink(href?: string): boolean {
  if (!href) return false;

  let urlObj: URL;
  try {
    urlObj = new URL(href, window.location.origin);
  } catch {
    return false;
  }
  const pathname = urlObj.pathname;
  if (isAutoPreviewHtmlLink(href)) return true;
  const fileName = safeDecodeFileName(pathname.split("/").pop() || "");
  const extMatch = fileName.match(/\.([a-zA-Z0-9]+)$/);
  if (
    extMatch &&
    extMatch[1].toLowerCase() === "html" &&
    !href.endsWith("html")
  ) {
    return false;
  }
  return Boolean(extMatch && FILE_EXTENSIONS.has(extMatch[1].toLowerCase()));
}

// 从 URL 提取文件名
function extractFileName(href: string): string {
  return extractDecodedFileNameFromUrl(href, "文件");
}

export default function Link(props: LinkProps) {
  if (props["data-footnote-ref"] === "") return <Sup {...props} />;
  if (props.children === "↩" && props["data-footnote-backref"] === "")
    return null;

  const originHref = props.href;

  if (isFileLink(originHref)) {
    const fileName = extractFileName(originHref);
    return (
      <DownloadFileCard
        url={originHref}
        fileName={fileName}
        enableClickTracking={isAutoPreviewHtmlLink(originHref, fileName)}
        hideLoadBtn
      />
    );
  }

  const handleClick = (event: MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    if (!originHref) return;

    let url: URL;
    try {
      url = new URL(originHref, window.location.href);
    } catch {
      return;
    }

    const toParentMsg = url.searchParams.get("toParentMsg");
    const jumpUrlKey = url.searchParams.get("jumpUrlKey");
    const targetOrigin = window.__postMsgSwe__ || "*";

    if (window.parent !== window) {
      if (toParentMsg) {
        // const newToParentMsg = JSON.parse(Base64.decode(toParentMsg.replace(/\s/g, "+")))
        // 查询参数中的未转义 + 会被解析为空格，还原后交给父应用处理。
        window.parent.postMessage(
          toParentMsg.replace(/\s/g, "+"),
          targetOrigin,
        );
        return;
      }
      if (jumpUrlKey !== null) {
        window.parent.postMessage(
          { type: "CLAW_SEND_URL_KEY", payload: { jumpUrlKey } },
          targetOrigin,
        );
        return;
      }
    }

    window.open(originHref, "_blank", "noopener,noreferrer");
  };

  return (
    <a
      id={getHrefId(originHref)}
      {...props}
      href={getHref(originHref)}
      onClick={handleClick}
    />
  );
}

function getHref(href?: string): string {
  if (!href) {
    return "";
  }
  try {
    const url = new URL(href);
    if (url.host === CUSTOMER_INSIGHT_HOST) {
      return encryptUrl(href);
    } else {
      return href;
    }
  } catch {
    return href;
  }
}

function encryptUrl(url: string): string {
  // 需要替换
  const key = "RM-XXXXX";
  let encrypted = "";
  for (let i = 0; i < url.length; i++) {
    const charCode = url.charCodeAt(i);
    const keyChar = key[i % key.length].charCodeAt(0);
    encrypted += String.fromCharCode(charCode ^ keyChar);
  }
  return btoa(encrypted); // Base64 编码
}

// 手动设置电访和客户洞察url的a标签id
function getHrefId(href?: string): string {
  if (!href) {
    return "";
  }
  try {
    const url = new URL(href);
    const hash = url.hash;
    if (hash && hash.includes("/wpcontactpanelv2/")) {
      const params = url.searchParams;
      const custUid = params.get("custUid");
      const bbkOrgId = params.get("bbkOrgId");
      if (!custUid || !bbkOrgId) return "";
      return `2DF_${bbkOrgId}_${custUid}`;
    }
    if (url.hostname === CUSTOMER_INSIGHT_HOST) {
      const match = hash.match(/\/homepage\/([A-Za-z0-9+/=]+)/);
      if (!match) return "";
      const base64String = match[1];
      let decoded;
      try {
        decoded = atob(base64String); // 注意：atob 仅支持标准 Base64
      } catch {
        return "";
      }
      const params = new URLSearchParams(decoded);
      const custUid = params.get("custUid");
      const bbkOrgId = params.get("bbkOrgId");
      if (!custUid || !bbkOrgId) return "";
      return `2KH_${bbkOrgId}_${custUid}`;
    }
  } catch {
    return "";
  }
  return "";
}

function Sup(props: LinkProps) {
  const { getPrefixCls } = useProviderContext();
  const prefixCls = getPrefixCls("markdown-footnote");
  const { href, ...rest } = props;

  return (
    <a
      id={getHrefId(href)}
      {...rest}
      className={prefixCls}
      onClick={() => {
        try {
          const id = props.id?.split("-")[2];
          if (!id) return;
          const url = document
            .querySelector(`#footnote-${id}`)
            ?.querySelector("a")
            ?.getAttribute("href");
          if (url) window.open(url, "_blank", "noopener,noreferrer");
        } catch {
          return;
        }
      }}
    />
  );
}
