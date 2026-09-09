import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HtmlPreviewTrackingProvider } from "../HtmlPreviewTrackingContext";
import FilePreviewDrawer from "../FilePreviewDrawer";
import FilePreviewModal from "./index";

type AttachHtmlPreviewClickTracker =
  typeof import("./htmlPreviewClickTracking").attachHtmlPreviewClickTracker;
type TrackerParams = Parameters<AttachHtmlPreviewClickTracker>[0];

const attachHtmlPreviewClickTrackerMock = vi.hoisted(() =>
  vi.fn(() => vi.fn()),
);
const recordClickMock = vi.hoisted(() => vi.fn());
const recordListSnapshotMock = vi.hoisted(() => vi.fn());
const getRecordDataMock = vi.hoisted(() => vi.fn());
const renderTemplateMock = vi.hoisted(() => vi.fn());
const renderStaticTemplateMock = vi.hoisted(() => vi.fn());
const isStaticTemplateMock = vi.hoisted(() => vi.fn(() => false));
const templateListMock = vi.hoisted(() => ({ current: [] }));

vi.mock("@/api/modules/htmlPreviewEvents", () => ({
  htmlPreviewEventsApi: {
    recordClick: recordClickMock,
    recordListSnapshot: recordListSnapshotMock,
  },
}));

vi.mock("@/api/modules/dynamicRender", () => ({
  dynamicRenderApi: {
    getRecordData: getRecordDataMock,
  },
}));

vi.mock("./htmlPreviewClickTracking", async (importOriginal) => {
  const actual = await importOriginal<
    typeof import("./htmlPreviewClickTracking")
  >();
  return {
    ...actual,
    attachHtmlPreviewClickTracker: attachHtmlPreviewClickTrackerMock,
  };
});

vi.mock("../DynamicRenderContext", () => ({
  useDynamicRender: () => ({
    renderTemplate: renderTemplateMock,
    renderStaticTemplate: renderStaticTemplateMock,
    isStaticTemplate: isStaticTemplateMock,
    templateList: templateListMock,
    isTemplateListLoaded: true,
  }),
}));

vi.mock("../Markdown", () => ({
  default: ({ content }: { content: string }) => <div>{content}</div>,
}));

vi.mock("antd", () => ({
  Button: ({
    children,
    onClick,
    "aria-label": ariaLabel,
  }: {
    children?: ReactNode;
    onClick?: () => void;
    "aria-label"?: string;
  }) => (
    <button type="button" onClick={onClick} aria-label={ariaLabel}>
      {children}
    </button>
  ),
  Modal: ({
    open,
    children,
    title,
  }: {
    open: boolean;
    children: ReactNode;
    title?: ReactNode;
  }) =>
    open ? (
      <div data-testid="preview-modal">
        {title}
        {children}
      </div>
    ) : null,
  Drawer: ({
    open,
    children,
    title,
    extra,
    mask,
    placement,
  }: {
    open: boolean;
    children: ReactNode;
    title?: ReactNode;
    extra?: ReactNode;
    mask?: boolean;
    placement?: string;
  }) =>
    open ? (
      <aside
        data-testid="preview-drawer"
        data-mask={String(mask)}
        data-placement={placement}
      >
        {title}
        {extra}
        {children}
      </aside>
    ) : null,
  Spin: ({ tip }: { tip?: string }) => <div>{tip || "loading"}</div>,
  Tooltip: ({ children }: { children: ReactNode }) => <>{children}</>,
  message: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

vi.mock("@ant-design/icons", () => ({
  ArrowLeftOutlined: () => <span data-testid="back-icon" />,
  FullscreenOutlined: () => <span data-testid="fullscreen-icon" />,
}));

vi.mock("@agentscope-ai/icons", () => ({
  SparkDownloadLine: () => <span data-testid="download-icon" />,
  SparkFalseLine: () => <span data-testid="close-icon" />,
}));

vi.mock("@agentscope-ai/design", () => ({
  IconButton: ({
    children,
    onClick,
  }: {
    children?: ReactNode;
    onClick?: () => void;
  }) => <button onClick={onClick}>{children}</button>,
}));

function getLatestTrackerParams(): TrackerParams {
  const calls = attachHtmlPreviewClickTrackerMock.mock.calls;
  const latestCall = calls[calls.length - 1] as unknown as
    | [TrackerParams]
    | undefined;
  expect(latestCall).toBeDefined();
  return latestCall![0];
}

beforeEach(() => {
  attachHtmlPreviewClickTrackerMock.mockClear();
  recordClickMock.mockClear();
  recordListSnapshotMock.mockClear();
  getRecordDataMock.mockReset();
  renderTemplateMock.mockReset();
  renderStaticTemplateMock.mockReset();
  isStaticTemplateMock.mockReset();
  getRecordDataMock.mockResolvedValue({ code: "200", data: { ok: true } });
  renderTemplateMock.mockResolvedValue("<html><body>preview</body></html>");
  renderStaticTemplateMock.mockResolvedValue(
    "<html><body>static preview</body></html>",
  );
  isStaticTemplateMock.mockReturnValue(false);
});

afterEach(() => {
  cleanup();
});

describe("FilePreviewModal HTML preview recording", () => {
  it("renders a non-blocking right-side preview drawer with the file name", () => {
    render(
      <FilePreviewDrawer
        open
        onClose={vi.fn()}
        fileUrl="https://example.test/report.zip"
        fileName="季度经营分析报告.zip"
      />,
    );

    const drawer = screen.getByTestId("preview-drawer");
    expect(drawer).toHaveAttribute("data-mask", "false");
    expect(drawer).toHaveAttribute("data-placement", "right");
    expect(drawer).toHaveTextContent("季度经营分析报告.zip");
    expect(document.documentElement).toHaveClass(
      "copaw-file-preview-drawer-open",
    );
  });

  it("keeps the shared preview modal as the default presentation", () => {
    render(
      <FilePreviewModal
        open
        onClose={vi.fn()}
        fileUrl="https://example.test/report.html"
        fileName="定时任务报告.html"
      />,
    );

    expect(screen.getByTestId("preview-modal")).toBeInTheDocument();
    expect(screen.queryByTestId("preview-drawer")).not.toBeInTheDocument();
    expect(document.documentElement).not.toHaveClass(
      "copaw-file-preview-drawer-open",
    );
  });

  it("records normal task auto-preview clicks and list snapshots", async () => {
    render(
      <HtmlPreviewTrackingProvider
        value={{ cronTaskId: "task-1", cronTaskName: "到期客户任务" }}
      >
        <FilePreviewModal
          open
          onClose={vi.fn()}
          fileUrl="https://example.test/report[auto-preview].html?resultId=result-1&templateId=1"
          fileName="report[auto-preview].html"
          enableClickTracking
        />
      </HtmlPreviewTrackingProvider>,
    );

    await waitFor(() => {
      const node = document.querySelector("iframe");
      expect(node).toBeTruthy();
      return node as HTMLIFrameElement;
    });

    await waitFor(() => {
      expect(attachHtmlPreviewClickTrackerMock).toHaveBeenCalled();
    });
    const trackerParams = getLatestTrackerParams();
    expect(trackerParams.metadata).toMatchObject({
      cronTaskId: "task-1",
      cronTaskName: "到期客户任务",
      fileUrl:
        "https://example.test/report[auto-preview].html?resultId=result-1&templateId=1",
      fileName: "report[auto-preview].html",
    });
    expect(trackerParams.reporter).toBe(recordClickMock);
    expect(trackerParams.listSnapshotReporter).toBe(recordListSnapshotMock);
  });

  it("suppresses recording in read-only replay while keeping nested preview routing", async () => {
    render(
      <HtmlPreviewTrackingProvider value={{ disableEventRecording: true }}>
        <FilePreviewModal
          open
          onClose={vi.fn()}
          fileUrl="https://example.test/report[auto-preview].html?resultId=result-1&templateId=1"
          fileName="report[auto-preview].html"
          enableClickTracking
        />
      </HtmlPreviewTrackingProvider>,
    );

    await waitFor(() => {
      const node = document.querySelector("iframe");
      expect(node).toBeTruthy();
      return node as HTMLIFrameElement;
    });

    await waitFor(() => {
      expect(attachHtmlPreviewClickTrackerMock).toHaveBeenCalled();
    });
    const trackerParams = getLatestTrackerParams();
    expect(trackerParams.reporter).not.toBe(recordClickMock);
    expect(trackerParams.listSnapshotReporter).toBeUndefined();

    trackerParams.reporter({
      file_url:
        "https://example.test/report[auto-preview].html?resultId=result-1&templateId=1",
      button_id: "plan",
      button_name: "查看方案",
      button_text: "查看方案",
      clicked_at: new Date().toISOString(),
    });
    expect(recordClickMock).not.toHaveBeenCalled();
    expect(recordListSnapshotMock).not.toHaveBeenCalled();

    const baseAttachCount = attachHtmlPreviewClickTrackerMock.mock.calls.length;

    await act(async () => {
      trackerParams.onOpenNestedPreview({
        fileUrl:
          "https://example.test/nested-plan.html?resultId=result-2&templateId=2",
        fileName: "nested-plan.html",
        listKey:
          "https://example.test/report[auto-preview].html?resultId=result-1&templateId=1",
        listName: "report[auto-preview].html",
        customerInfo: { customer_id: "CUST-001", name: "张三" },
        custUid: "CUST-001",
      });
    });

    await waitFor(() => {
      expect(getRecordDataMock).toHaveBeenCalledWith("result-2", "2");
    });

    await waitFor(() => {
      const nodes = document.querySelectorAll("iframe");
      expect(nodes).toHaveLength(2);
      return nodes;
    });

    await waitFor(() => {
      expect(
        attachHtmlPreviewClickTrackerMock.mock.calls.length,
      ).toBeGreaterThan(baseAttachCount);
    });
    const nestedTrackerParams = getLatestTrackerParams();
    expect(nestedTrackerParams.reporter).not.toBe(recordClickMock);
    expect(nestedTrackerParams.listSnapshotReporter).toBeUndefined();
    expect(nestedTrackerParams.metadata).toMatchObject({
      fileUrl:
        "https://example.test/nested-plan.html?resultId=result-2&templateId=2",
      fileName: "nested-plan.html",
      listKey:
        "https://example.test/report[auto-preview].html?resultId=result-1&templateId=1",
      listName: "report[auto-preview].html",
      defaultCustomerInfo: { customer_id: "CUST-001", name: "张三" },
    });
  });

  it("replaces the workspace preview for nested links without changing the default stack mode", async () => {
    render(
      <FilePreviewModal
        open
        onClose={vi.fn()}
        fileUrl="https://example.test/report[auto-preview].html?resultId=result-1&templateId=1"
        fileName="report[auto-preview].html"
        enableClickTracking
        presentation="workspace"
        nestedPreviewMode="replace"
      />,
    );

    await waitFor(() => {
      expect(document.querySelectorAll("iframe")).toHaveLength(1);
    });
    const trackerParams = getLatestTrackerParams();

    await act(async () => {
      trackerParams.onOpenNestedPreview({
        fileUrl:
          "https://example.test/nested-plan.html?resultId=result-2&templateId=2",
        fileName: "nested-plan.html",
        listKey:
          "https://example.test/report[auto-preview].html?resultId=result-1&templateId=1",
        listName: "report[auto-preview].html",
        customerInfo: null,
        custUid: "",
      });
    });

    await waitFor(() => {
      expect(screen.getByTitle("nested-plan.html")).toBeInTheDocument();
      expect(
        screen.queryByTitle("report[auto-preview].html"),
      ).not.toBeInTheDocument();
      expect(document.querySelectorAll("iframe")).toHaveLength(1);
    });

    fireEvent.click(screen.getByRole("button", { name: "返回上一级预览" }));

    await waitFor(() => {
      expect(
        screen.getByTitle("report[auto-preview].html"),
      ).toBeInTheDocument();
      expect(screen.queryByTitle("nested-plan.html")).not.toBeInTheDocument();
      expect(document.querySelectorAll("iframe")).toHaveLength(1);
    });
  });

  it("keeps nested previews stacked when no replacement mode is requested", async () => {
    render(
      <FilePreviewModal
        open
        onClose={vi.fn()}
        fileUrl="https://example.test/report[auto-preview].html?resultId=result-1&templateId=1"
        fileName="report[auto-preview].html"
        enableClickTracking
      />,
    );

    await waitFor(() => {
      expect(document.querySelectorAll("iframe")).toHaveLength(1);
    });
    const trackerParams = getLatestTrackerParams();

    await act(async () => {
      trackerParams.onOpenNestedPreview({
        fileUrl:
          "https://example.test/nested-plan.html?resultId=result-2&templateId=2",
        fileName: "nested-plan.html",
        listKey:
          "https://example.test/report[auto-preview].html?resultId=result-1&templateId=1",
        listName: "report[auto-preview].html",
        customerInfo: null,
        custUid: "",
      });
    });

    await waitFor(() => {
      expect(screen.getAllByTestId("preview-modal")).toHaveLength(2);
      expect(document.querySelectorAll("iframe")).toHaveLength(2);
    });
  });

  it("suppresses iframe opt-out recording while preserving task metadata", async () => {
    render(
      <HtmlPreviewTrackingProvider
        value={{
          cronTaskId: "task-1",
          cronTaskName: "到期客户任务",
          disableEventRecording: true,
        }}
      >
        <FilePreviewModal
          open
          onClose={vi.fn()}
          fileUrl="https://example.test/report[auto-preview].html?resultId=result-1&templateId=1"
          fileName="report[auto-preview].html"
          enableClickTracking
        />
      </HtmlPreviewTrackingProvider>,
    );

    await waitFor(() => {
      const node = document.querySelector("iframe");
      expect(node).toBeTruthy();
      return node as HTMLIFrameElement;
    });

    await waitFor(() => {
      expect(attachHtmlPreviewClickTrackerMock).toHaveBeenCalled();
    });

    const trackerParams = getLatestTrackerParams();
    expect(trackerParams.metadata).toMatchObject({
      cronTaskId: "task-1",
      cronTaskName: "到期客户任务",
    });
    expect(trackerParams.reporter).not.toBe(recordClickMock);
    expect(trackerParams.listSnapshotReporter).toBeUndefined();

    trackerParams.reporter({
      file_url:
        "https://example.test/report[auto-preview].html?resultId=result-1&templateId=1",
      button_id: "plan",
      button_name: "查看方案",
      button_text: "查看方案",
      clicked_at: new Date().toISOString(),
    });
    expect(recordClickMock).not.toHaveBeenCalled();
    expect(recordListSnapshotMock).not.toHaveBeenCalled();
  });
});
