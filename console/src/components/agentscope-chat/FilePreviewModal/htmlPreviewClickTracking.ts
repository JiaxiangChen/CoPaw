import type {
  HtmlTrackerPayloadType,
  HtmlPreviewTemplateType,
  HtmlPreviewListSnapshotPayload,
} from "@/api/types/htmlPreviewEvents";

export interface HtmlPreviewClickMetadata {
  cronTaskId?: string | null;
  cronTaskName?: string | null;
  fileUrl: string;
  fileName?: string | null;
  listKey?: string | null;
  listName?: string | null;
  defaultCustomerInfo?: Record<string, string> | null;
  traceId?: string;
  templateId?: string | null;
  resultId?: string | null;
  rootTemplateId?: string | null;
  rootResultId?: string | null;
  templateType?: HtmlPreviewTemplateType | null;
  pageSource?: string;
  platformSource?: string;
}

export type HtmlPreviewClickReporter = (
  payload: HtmlTrackerPayloadType,
) => Promise<unknown> | unknown;

export type HtmlPreviewListSnapshotReporter = (
  payload: HtmlPreviewListSnapshotPayload,
) => Promise<unknown> | unknown;

export interface NestedHtmlPreviewRequest {
  fileUrl: string;
  fileName: string;
  listKey: string;
  listName: string;
  customerInfo: Record<string, string> | null;
  custUid: string;
}

const CLICKABLE_SELECTOR = "button,a,[role='button'],[data-track-id]";
const NESTED_PREVIEW_SELECTOR = "a[data-preview-modal='true']";
const CUSTOMER_DATA_PREFIX = "customer";
const CUSTOMER_INFO_DATA_KEY = "customerInfo";

// 从 URL 中提取 resultId 和 templateId 的正则表达式
const RESULT_ID_REGEX = /[?&]resultId=([^&]+)/i;
const TEMPLATE_ID_REGEX = /[?&]templateId=([^&]+)/i;

function extractResultIdFromUrl(url: string): string | null {
  const match = url.match(RESULT_ID_REGEX);
  return match ? match[1] : null;
}

function extractTemplateIdFromUrl(url: string): string | null {
  const match = url.match(TEMPLATE_ID_REGEX);
  return match ? match[1] : null;
}
const CUSTOMER_NAME_HEADER_PATTERN = /^(kh姓名|kh名称|姓名)$/;
const CUSTOMER_INFO_ALLOWED_KEYS = new Set([
  "customer_id",
  "customer_name",
  "name",
  "kh姓名",
  "kh名称",
  "姓名",
]);

function normalizeText(value: string | null | undefined, maxLength: number) {
  const normalized = value?.replace(/\s+/g, " ").trim() || "";
  return normalized ? normalized.slice(0, maxLength) : null;
}

function normalizeKey(value: string) {
  return value
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .replace(/[\s-]+/g, "_")
    .toLowerCase();
}

function normalizeCustomerDatasetKey(key: string) {
  const rawKey = key.replace(CUSTOMER_DATA_PREFIX, "") || key;
  const normalizedKey = normalizeKey(rawKey);
  return normalizedKey === "id" ? "customer_id" : normalizedKey;
}

function getElementName(element: HTMLElement, buttonText: string | null) {
  return normalizeText(
    element.dataset.trackName ||
    element.getAttribute("aria-label") ||
    element.getAttribute("title") ||
    element.getAttribute("name") ||
    buttonText,
    255,
  );
}

function getClassFallbackId(
  element: HTMLElement,
  buttonText: string | null,
) {
  if (buttonText?.includes("查看方案")) {
    return "plan";
  }
  if (element.classList.contains("phone")) {
    return "phone";
  }
  if (element.classList.contains("link-btn")) {
    return "insight";
  }
  return null;
}

function getButtonType(
  element: HTMLElement,
  buttonId: string | null,
  buttonName: string | null,
  buttonText: string | null,
) {
  const normalizedId = (buttonId || "").toLowerCase();
  if (
    normalizedId.includes("plan") ||
    buttonName?.includes("查看方案") ||
    buttonText?.includes("查看方案")
  ) {
    return "plan";
  }
  if (
    element.classList.contains("phone") ||
    normalizedId.includes("phone") ||
    buttonName?.includes("电访") ||
    buttonName?.includes("电话访问") ||
    buttonText?.includes("电访") ||
    buttonText?.includes("电话访问")
  ) {
    return "phone";
  }
  if (
    element.classList.contains("link-btn") ||
    normalizedId.includes("insight") ||
    buttonName?.includes("洞察") ||
    buttonText?.includes("洞察")
  ) {
    return "insight";
  }
  return "other";
}

function parseCustomerInfoJson(value: string | undefined) {
  if (!value) {
    return null;
  }
  try {
    const parsed = JSON.parse(value) as Record<string, unknown>;
    return Object.fromEntries(
      Object.entries(parsed)
        .map(([key, item]) => [
          normalizeKey(key),
          item == null ? null : normalizeText(String(item), 512),
        ])
        .filter(
          (entry): entry is [string, string] =>
            Boolean(entry[1]) && CUSTOMER_INFO_ALLOWED_KEYS.has(entry[0]),
        ),
    );
  } catch {
    return null;
  }
}

function getStructuredCustomerInfo(element: HTMLElement) {
  const candidate = element.closest<HTMLElement>(
    `[data-customer-info],tr,[data-customer-name]`,
  );
  if (!candidate) {
    return null;
  }

  const jsonInfo = parseCustomerInfoJson(candidate.dataset[CUSTOMER_INFO_DATA_KEY]);
  if (jsonInfo && Object.keys(jsonInfo).length > 0) {
    return jsonInfo;
  }

  const entries = Object.entries(candidate.dataset)
    .filter(
      ([key, value]) =>
        key !== CUSTOMER_INFO_DATA_KEY &&
        key.startsWith(CUSTOMER_DATA_PREFIX) &&
        CUSTOMER_INFO_ALLOWED_KEYS.has(normalizeCustomerDatasetKey(key)) &&
        Boolean(normalizeText(value, 512)),
    )
    .map(([key, value]) => [
      normalizeCustomerDatasetKey(key),
      normalizeText(value, 512),
    ])
    .filter((entry): entry is [string, string] => Boolean(entry[1]));

  return entries.length > 0 ? Object.fromEntries(entries) : null;
}

function getTableHeaders(row: HTMLTableRowElement) {
  const table = row.closest("table");
  const headerCells = table
    ? Array.from(table.querySelectorAll("thead th"))
    : [];
  const fallbackHeaderCells =
    headerCells.length > 0
      ? headerCells
      : Array.from(table?.querySelectorAll("tr:first-child th") || []);

  return fallbackHeaderCells.map((cell, index) => {
    const text = normalizeText(cell.textContent, 64);
    return text || `column_${index + 1}`;
  });
}

function getFallbackCustomerInfoFromRow(element: HTMLElement) {
  const row = element.closest("tr");
  if (!row || row.tagName !== "TR") {
    return null;
  }

  const headers = getTableHeaders(row);
  const cells = Array.from(row.children).filter((cell) =>
    ["TD", "TH"].includes(cell.tagName),
  );
  const entries = cells
    .map((cell, index) => {
      const header = headers[index] || `column_${index + 1}`;
      if (!CUSTOMER_NAME_HEADER_PATTERN.test(header)) {
        return null;
      }
      const value = normalizeText(cell.textContent, 512);
      return value ? [header, value] : null;
    })
    .filter((entry): entry is [string, string] => Boolean(entry));

  return entries.length > 0 ? Object.fromEntries(entries) : null;
}

function getCustomerInfo(element: HTMLElement) {
  return (
    getStructuredCustomerInfo(element) ||
    getFallbackCustomerInfoFromRow(element)
  );
}

function getListKey(metadata: HtmlPreviewClickMetadata) {
  return metadata.listKey || metadata.fileUrl;
}

function getListName(metadata: HtmlPreviewClickMetadata) {
  return metadata.listName || metadata.fileName || metadata.fileUrl;
}

function getCustomerIdentity(customerInfo: Record<string, string> | null) {
  const info = customerInfo || {};
  return {
    customerId: info.customer_id || null,
    customerName:
      info.name ||
      info.customer_name ||
      info["kh姓名"] ||
      info["kh名称"] ||
      info["姓名"] ||
      null,
  };
}

function getFileNameFromUrl(url: string) {
  try {
    const urlObj = new URL(url, window.location.origin);
    const fileName = urlObj.pathname.split("/").pop();
    if (!fileName) {
      return "preview.html";
    }
    try {
      return decodeURIComponent(fileName);
    } catch {
      return fileName;
    }
  } catch {
    return url.split(/[?#]/)[0].split("/").pop() || "preview.html";
  }
}

// 检测链接是否为动态渲染链接（包含 resultId 和 templateId）
function isDynamicRenderLink(link: HTMLAnchorElement): boolean {
  const href = link.getAttribute("href") || link.href;
  return !!(extractResultIdFromUrl(href) && extractTemplateIdFromUrl(href));
}

function resolveNestedPreviewUrl(
  link: HTMLAnchorElement,
  metadata: HtmlPreviewClickMetadata,
) {
  const rawHref = link.getAttribute("href") || link.href;
  try {
    return new URL(rawHref, metadata.fileUrl).toString();
  } catch {
    return link.href || rawHref;
  }
}

export function buildHtmlPreviewClickPayload(
  element: HTMLElement,
  metadata: HtmlPreviewClickMetadata,
  clickedAt: Date = new Date(),
): HtmlTrackerPayloadType | null {
  const buttonText = normalizeText(element.textContent, 512);
  const buttonId = normalizeText(
    element.dataset.trackId ||
    element.id ||
    element.getAttribute("name") ||
    getClassFallbackId(element, buttonText) ||
    buttonText,
    255,
  );
  const buttonName = getElementName(element, buttonText);
  const customerInfo =
    getCustomerInfo(element) || metadata.defaultCustomerInfo || null;
  const customerIdentity = getCustomerIdentity(customerInfo);

  if (!buttonId && !buttonName && !buttonText) {
    return null;
  }
  const template_type: HtmlPreviewTemplateType = metadata.rootTemplateId ? 'sub' : 'main';
  return {
    cron_task_id: metadata.cronTaskId || null,
    cron_task_name: metadata.cronTaskName || null,
    file_url: metadata.fileUrl,
    file_name: metadata.fileName || null,
    list_key: getListKey(metadata),
    list_name: getListName(metadata),
    button_id: buttonId,
    button_name: buttonName,
    button_text: buttonText,
    button_type: getButtonType(element, buttonId, buttonName, buttonText),
    customer_id: customerIdentity.customerId,
    customer_name: customerIdentity.customerName,
    customer_info: customerInfo,
    clicked_at: clickedAt.toISOString(),
    trace_id: metadata.traceId || null,
    template_id: metadata.templateId ? parseInt(metadata.templateId, 10) : null,
    result_id: metadata.resultId || null,
    event_type: "button_click",
    template_type: metadata.templateType || template_type,
    page_source: metadata.pageSource || 'default',
    platform_source: metadata.platformSource || 'default',
  };
}

export function buildHtmlPreviewListSnapshotPayload(
  doc: Document,
  metadata: HtmlPreviewClickMetadata,
  snapshotAt: Date = new Date(),
): HtmlPreviewListSnapshotPayload | null {
  const HTMLElementCtor = doc.defaultView?.HTMLElement || HTMLElement;

  const rows = Array.from(doc.querySelectorAll("tr"));
  const customers = rows
    .map((row) => {
      if (!(row instanceof HTMLElementCtor)) {
        return null;
      }
      if (
        row.closest("thead") ||
        (row.querySelector("th") && !row.querySelector("td"))
      ) {
        return null;
      }
      const customerInfo = getCustomerInfo(row);
      const { customerId, customerName } = getCustomerIdentity(customerInfo);
      if (!customerId && !customerName) {
        return null;
      }
      return {
        customer_id: customerId,
        customer_name: customerName || "未知客户",
        extra_info: customerInfo,
      };
    })
    .filter(
      (
        item,
      ): item is {
        customer_id: string | null;
        customer_name: string;
        extra_info: Record<string, string> | null;
      } => Boolean(item),
    );

  if (customers.length === 0) {
    return null;
  }

  return {
    cron_task_id: metadata.cronTaskId || null,
    cron_task_name: metadata.cronTaskName || null,
    list_key: getListKey(metadata),
    list_name: getListName(metadata),
    file_url: metadata.fileUrl,
    file_name: metadata.fileName || null,
    customers,
    snapshot_at: snapshotAt.toISOString(),
  };
}

export function attachHtmlPreviewClickTracker(params: {
  iframe: HTMLIFrameElement;
  metadata: HtmlPreviewClickMetadata;
  reporter: HtmlPreviewClickReporter;
  listSnapshotReporter?: HtmlPreviewListSnapshotReporter;
  onOpenNestedPreview?: (preview: NestedHtmlPreviewRequest) => void;
  getTemplateName?: (templateId: number) => string | undefined;
}): () => void {
  const doc = params.iframe.contentDocument;
  const view = doc?.defaultView;
  if (!doc || !view) {
    return () => { };
  }

  // 屏蔽「随手改」SnapEdit 可视化编辑器页面（snap-editor/snapHtmlEditor.ts 生成，含 #shell/#blockBar 外壳）的点击埋点
  if (
    doc.querySelector("#blockBar") ||
    doc.querySelector("#shell") ||
    doc.querySelector("#aiPanel")
  ) {
    return () => { };
  }

  if (params.listSnapshotReporter) {
    const snapshotPayload = buildHtmlPreviewListSnapshotPayload(
      doc,
      params.metadata,
    );
    if (snapshotPayload) {
      try {
        void Promise.resolve(
          params.listSnapshotReporter(snapshotPayload),
        ).catch((error) => {
          console.warn("Failed to record HTML preview list snapshot:", error);
        });
      } catch (error) {
        console.warn("Failed to record HTML preview list snapshot:", error);
      }
    }
  }

  const handleClick = (event: MouseEvent) => {
    const target = event.target;
    if (!(target instanceof view.Element)) {
      return;
    }

    const element = target.closest(CLICKABLE_SELECTOR);
    if (!(element instanceof view.HTMLElement)) {
      return;
    }

    // 导航栏点击事件不记录
    if (element.closest(".nav-item")) {
      return;
    }

    const payload = buildHtmlPreviewClickPayload(
      element,
      params.metadata,
    );
    if (!payload) {
      return;
    }

    try {
      void Promise.resolve(params.reporter(payload)).catch((error) => {
        console.warn("Failed to record HTML preview click:", error);
      });
    } catch (error) {
      console.warn("Failed to record HTML preview click:", error);
    }

    // 处理嵌套预览 - 支持两种方式：
    // 1. 带 data-preview-modal="true" 属性的链接
    // 2. URL 中包含 resultId 和 templateId 的动态渲染链接
    const nestedPreviewLink = element.closest(NESTED_PREVIEW_SELECTOR);
    const isDynamicRender = element.closest("a") && isDynamicRenderLink(element.closest("a")!);

    if (
      params.onOpenNestedPreview &&
      ((nestedPreviewLink instanceof view.HTMLAnchorElement &&
        nestedPreviewLink.href) ||
        isDynamicRender)
    ) {
      let nestedPreviewUrl: string;
      let previewFileName: string;
      let customerInfo: Record<string, string> | null;

      if (isDynamicRender) {
        // 动态渲染链接：从 href 中提取 URL 并获取模板名称
        const anchorElement = element.closest("a")!;
        const href = anchorElement.getAttribute("href") || anchorElement.href;
        const templateId = extractTemplateIdFromUrl(href);
        nestedPreviewUrl = href;
        previewFileName = href.split("?")[0]?.split("/").pop() || "preview.html";

        // 如果提供了 getTemplateName 回调，使用它获取模板名称
        if (templateId && params.getTemplateName) {
          previewFileName = params.getTemplateName(parseInt(templateId, 10)) || previewFileName;
        }
        // 查找
        customerInfo = getCustomerInfo(element);
        console.log('customerInfo', customerInfo);
      } else {
        // data-preview-modal 属性链接
        nestedPreviewUrl = resolveNestedPreviewUrl(
          nestedPreviewLink as HTMLAnchorElement,
          params.metadata,
        );
        previewFileName = getFileNameFromUrl(nestedPreviewUrl);
      }

      event.preventDefault();
      params.onOpenNestedPreview({
        fileUrl: nestedPreviewUrl,
        fileName: previewFileName,
        listKey: payload.list_key || getListKey(params.metadata),
        listName: payload.list_name || getListName(params.metadata),
        customerInfo: payload.customer_info || null,
        custUid: customerInfo?.customer_id || null,
      });
    }
  };

  doc.addEventListener("click", handleClick, true);

  return () => doc.removeEventListener("click", handleClick, true);
}

// === Exposure Tracking (曝光埋点) ===

const VISIBLE_MONITOR_SELECTOR = '.js-view-monitor';
const EXPOSURE_THRESHOLD = 0.5;
const VIEWPORT_COVERAGE_THRESHOLD = 0.8;
const EXPOSURE_DURATION_MS = 2000;

export interface ExposureTrackerParams {
  iframe: HTMLIFrameElement;
  metadata: HtmlPreviewClickMetadata;
  reporter?: (payload: HtmlTrackerPayloadType) => void;
}
export interface HtmlPreviewExposureMetadata extends HtmlPreviewClickMetadata {
  sectionId?: string;
  sectionName?: string;
}
export function buildHtmlPreviewExposurePayload(
  metadata: HtmlPreviewExposureMetadata,
  clickedAt: Date = new Date(),
): HtmlTrackerPayloadType | null {
  // const buttonText = normalizeText(metadata.sectionName, 512);
  const customerInfo = metadata.defaultCustomerInfo || null;
  const customerIdentity = getCustomerIdentity(customerInfo);

  if (!metadata.sectionName) {
    return null;
  }
  const template_type: HtmlPreviewTemplateType = metadata.rootTemplateId ? 'sub' : 'main';
  return {
    cron_task_id: metadata.cronTaskId || null,
    cron_task_name: metadata.cronTaskName || null,
    file_url: metadata.fileUrl,
    file_name: metadata.fileName || null,
    list_key: getListKey(metadata),
    list_name: getListName(metadata),
    customer_id: customerIdentity.customerId,
    customer_name: customerIdentity.customerName,
    customer_info: customerInfo,
    clicked_at: clickedAt.toISOString(),
    trace_id: metadata.traceId || null,
    event_type: 'module_exposure',
    template_id: metadata.templateId ? parseInt(metadata.templateId, 10) : null,
    result_id: metadata.resultId || null,
    event_target_id: metadata.sectionId || null,
    event_target_name: metadata.sectionName || null,
    template_type: metadata.templateType || template_type,
    page_source: metadata.pageSource || 'default',
    platform_source: metadata.platformSource || 'default',
  };
}

export function attachHtmlPreviewExposureTracker(
  params: ExposureTrackerParams
): () => void {
  const { iframe, metadata, reporter } = params;
  const doc = iframe.contentDocument;
  if (!doc) return () => { };
  const elements = doc.querySelectorAll(VISIBLE_MONITOR_SELECTOR);
  if (!elements.length) {
    return () => { };
  }

  const elementStates = new Map<Element, {
    timer: ReturnType<typeof setTimeout> | null;
    reported: boolean;
  }>();

  const moduleExposureMap = new Map<string, boolean>();

  const viewportHeight = (doc.defaultView?.innerHeight || window.innerHeight);

  const isEffectivelyExposed = (entry: IntersectionObserverEntry): boolean => {
    if (!entry.isIntersecting) return false;
    if (entry.intersectionRatio >= EXPOSURE_THRESHOLD) return true;
    const visibleHeight = entry.intersectionRect.height;
    return visibleHeight / viewportHeight >= VIEWPORT_COVERAGE_THRESHOLD;
  };

  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        const el = entry.target as HTMLElement;
        const state = elementStates.get(el);
        const logData = parseDataLog(el);
        if (!state || (logData?.sectionId && moduleExposureMap.get(logData.sectionId)) || !logData.sectionId) return;

        if (isEffectivelyExposed(entry)) {
          if (!state.reported) {
            if(state.timer){
              clearTimeout(state.timer);
            }
            state.timer = setTimeout(() => {
              const payload: HtmlTrackerPayloadType = buildHtmlPreviewExposurePayload({ ...metadata, ...logData });
              reporter?.(payload);
              moduleExposureMap.set(logData.sectionId, true);
              state.reported = true;
              state.timer = null;
            }, EXPOSURE_DURATION_MS);
          }
        } else if (!entry.isIntersecting) {
          if (state.timer) {
            clearTimeout(state.timer);
            state.timer = null;
          }
        }
      });
    },
    { threshold: [0, 0.1, 0.2, 0.3, 0.4, EXPOSURE_THRESHOLD] }
  );


  if (elements.length > 0) {
    elements.forEach((el) => {
      elementStates.set(el, { timer: null, reported: false });
      observer.observe(el);
    });
  }

  return () => {
    console.log('[Exposure] Stop tracking');
    observer.disconnect();
    if (elementStates) {
      elementStates.forEach((state) => {
        if (state.timer) clearTimeout(state.timer);
      });
    }
    elementStates.clear();
    moduleExposureMap.clear();
  };
}

function parseDataLog(el: Element): Record<string, string> {
  const sectionId = el.getAttribute('data-target-id');
  const sectionName = el.getAttribute('data-target-name');
  if (!sectionId || !sectionName) return {};
  try {
    return { sectionId, sectionName };
  } catch {
    return {};
  }
}
export function buildHtmlPreviewLoadPayload(
  metadata: HtmlPreviewExposureMetadata,
  clickedAt: Date = new Date(),
): HtmlTrackerPayloadType | null {
  const customerInfo = metadata.defaultCustomerInfo || null;
  const customerIdentity = getCustomerIdentity(customerInfo);
  const template_type: HtmlPreviewTemplateType = metadata.rootTemplateId ? 'sub' : 'main';

  return {
    cron_task_id: metadata.cronTaskId || null,
    cron_task_name: metadata.cronTaskName || null,
    file_url: metadata.fileUrl,
    file_name: metadata.fileName || null,
    list_key: getListKey(metadata),
    list_name: getListName(metadata),
    customer_id: customerIdentity.customerId,
    customer_name: customerIdentity.customerName,
    customer_info: customerInfo,
    clicked_at: clickedAt.toISOString(),
    trace_id: metadata.traceId || null,
    event_type: 'preview_view',
    template_id: metadata.templateId ? parseInt(metadata.templateId, 10) : null,
    result_id: metadata.resultId || null,
    template_type: metadata.templateType || template_type,
    page_source: metadata.pageSource || 'default',
    platform_source: metadata.platformSource || 'default',
  };
}

export function attachHtmlPreviewLoadTracker(
  params: ExposureTrackerParams
) {
  const { iframe, metadata, reporter } = params;
  const doc = iframe.contentDocument;
  if (!doc) return;

  const payload: HtmlTrackerPayloadType = buildHtmlPreviewLoadPayload(metadata);
  reporter?.(payload);
}
