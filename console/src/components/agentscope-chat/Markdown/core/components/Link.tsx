import { useProviderContext } from '@/components/agentscope-chat';
import DownloadFileCard from "../../../DownloadFileCard";
import {
  extractDecodedFileNameFromUrl,
  isAutoPreviewHtmlLink,
  safeDecodeFileName,
} from "../../../FilePreviewModal/fileUtils";
// import { Base64 } from 'js-base64'

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
  // 匹配常见文件扩展名
  const fileExts = [
    "png", "jpg", "jpeg", "gif", "bmp", "webp", "svg",
    "mp4", "webm", "mp3", "wav", "ogg",
    "pdf", "html", "htm",
    "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "md", "mdx", "txt", "json", "xml", "csv", "log", "yaml", "yml",
    "zip", "rar", "7z", "tar", "gz",
  ];
  const fileName = safeDecodeFileName(pathname.split("/").pop() || "");
  const extMatch = fileName.match(/\.([a-zA-Z0-9]+)$/);
  if (extMatch && extMatch[1].toLowerCase() === 'html' && !href.endsWith("html")) {
    return false
  }
  return extMatch && fileExts.includes(extMatch[1].toLowerCase());
}

// 从 URL 提取文件名
function extractFileName(href: string): string {
  return extractDecodedFileNameFromUrl(href, "文件");
}

export default function Link(props) {
  const getHrefId = (href: string) => {
    if (!href) {
      return ''
    }
    try {
      const url = new URL(href);
      const hash = url.hash;
      if (hash && hash.includes('/wpcontactpanelv2/')) {
        const params = new URLSearchParams(url?.search);
        const custUid = params.get('custUid');
        const bbkOrgId = params.get('bbkOrgId');
        if (!custUid || !bbkOrgId) return '';
        return `2DF_${bbkOrgId}_${custUid}`
      }
      if (url.hostname === 'cusinsightuixc.paasoa.cmbchina.cn') {
        const match = hash.match(/\/homepage\/([A-Za-z0-9+\/=]+)/);
        const base64String = match[1];
        let decoded;
        try {
          decoded = atob(base64String); // 注意：atob 仅支持标准 Base64
        } catch (e) {
          return '';
        }
        const params = new URLSearchParams(decoded);
        const custUid = params.get('custUid');
        const bbkOrgId = params.get('bbkOrgId');
        if (!custUid || !bbkOrgId) return '';
        return `2KH_${bbkOrgId}_${custUid}`
      }
    } catch (e) {
      return ""
    }
    return ""
  }
  if (props['data-footnote-ref'] === '') return <Sup {...props} />;
  if (props.children === '↩' && props['data-footnote-backref'] === '') return null;

  const encryptUrl = getHref(props?.href)
  const originHref = props?.href

  if (isFileLink(originHref)) {
    const fileName = extractFileName(originHref);
    return (
      <DownloadFileCard
        url={originHref}
        fileName={fileName}
        enableClickTracking={isAutoPreviewHtmlLink(originHref, fileName)}
        hideLoadBtn={true}
      />
    );
  }

  return <a id={getHrefId(props?.href)}  {...props} href={encryptUrl} onClick={(e) => {
    console.log('props?.href', props?.href)
    const url = new URL(originHref);
    console.log('url', url)
    e.preventDefault();
    const toParentMsg = url.searchParams.get('toParentMsg');
    console.log("toParentMsg", toParentMsg)
    if (toParentMsg) {
      // const newToParentMsg = JSON.parse(Base64.decode(toParentMsg.replace(/\s/g, "+")))
      const newToParentMsg = toParentMsg.replace(/\s/g, "+")
      // 解析为 JavaScript 对象
      if (window.parent !== window) {
        window.parent.postMessage(newToParentMsg, window.__postMsgSwe__ || '*');
      } else {
        window.open(originHref, '_blank', 'noopener,noreferrer');
      }
    } else {
      window.open(originHref, '_blank', 'noopener,noreferrer');
    }
  }
  } />;
}

function getHref(href: string) {
  if (!href) {
    return ''
  }
  try {
    const url = new URL(href);
    console.log('url', url.host)
    if (url.host === 'cusinsightuixc.paasoa.cmbchina.cn') {
      return encryptUrl(href)
    } else {
      return href
    }
  } catch (e) {
    return href
  }
}

function encryptUrl(url) {
  const key = "RM-CLAW-WEB";
  let encrypted = '';
  for (let i = 0; i < url.length; i++) {
    const charCode = url.charCodeAt(i);
    const keyChar = key[i % key.length].charCodeAt(0);
    encrypted += String.fromCharCode(charCode ^ keyChar);
  }
  return btoa(encrypted); // Base64 编码
}

// 🔓 解密函数
function decryptUrl(encrypted) {
  const decoded = atob(encrypted);
  const key = "RM-CLAW-WEB";
  let decrypted = '';
  for (let i = 0; i < decoded.length; i++) {
    const charCode = decoded.charCodeAt(i);
    const keyChar = key[i % key.length].charCodeAt(0);
    decrypted += String.fromCharCode(charCode ^ keyChar);
  }
  return decrypted;
}

// 手动设置电访和客户洞察url的a标签id
const getHrefId = (href: string) => {
  console.log('getHrefId1', href)
  if (!href) {
    return ''
  }
  try {
    const url = new URL(href);
    const hash = url.hash;
    if (hash && hash.includes('/wpcontactpanelv2/')) {
      const params = new URLSearchParams(url?.search);
      const custUid = params.get('custUid');
      const bbkOrgId = params.get('bbkOrgId');
      if (!custUid || !bbkOrgId) return '';
      return `2DF_${bbkOrgId}_${custUid}`
    }
    if (url.hostname === 'cusinsightuixc.paasoa.cmbchina.cn') {
      const match = hash.match(/\/homepage\/([A-Za-z0-9+\/=]+)/);
      const base64String = match[1];
      let decoded;
      try {
        decoded = atob(base64String); // 注意：atob 仅支持标准 Base64
      } catch (e) {
        return '';
      }
      const params = new URLSearchParams(decoded);
      const custUid = params.get('custUid');
      const bbkOrgId = params.get('bbkOrgId');
      if (!custUid || !bbkOrgId) return '';
      return `2KH_${bbkOrgId}_${custUid}`
    }
  } catch (e) {
    return ""
  }
  return ""
}

function Sup(props) {
  const { getPrefixCls } = useProviderContext();
  const prefixCls = getPrefixCls('markdown-footnote');
  const { href, ...rest } = props;

  return <a id={getHrefId(props?.href)} {...rest} className={prefixCls} onClick={() => {
    try {
      const [x, y, id,] = props.id.split('-');
      const url = document.querySelector(`#footnote-${id}`).querySelector('a').getAttribute('href');
      window.open(url, '_blank');
    } catch {
      return;
    }
  }} />
}
