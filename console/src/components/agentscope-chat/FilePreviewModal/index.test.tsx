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
import { HtmlAnnotationProvider } from "../HtmlAnnotations/context";
import HtmlAnnotationComposerSummary from "../HtmlAnnotations/ComposerSummary";
import { cronAccessKey, currentCronContext } from "@/api/cronTaskContext";

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
const markTaskReferenceReadMock = vi.hoisted(() => vi.fn());
vi.mock("@/api/modules/cronTaskActions", () => ({
  markTaskReferenceRead: markTaskReferenceReadMock,
}));

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
  Input: {
    TextArea: (props: Record<string, unknown>) => <textarea {...props} />,
  },
  message: {
    error: vi.fn(),
    success: vi.fn(),
    warning: vi.fn(),
  },
}));

vi.mock("@ant-design/icons", () => ({
  ArrowLeftOutlined: () => <span data-testid="back-icon" />,
  CloseOutlined: () => <span data-testid="annotation-close-icon" />,
  CommentOutlined: () => <span data-testid="annotation-comment-icon" />,
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
  markTaskReferenceReadMock.mockReset();
  markTaskReferenceReadMock.mockResolvedValue({});
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
  it("新任务报告真正渲染完成后才按会话来源标读", async () => {
    const origin = {
      owner: "scheduler" as const,
      context: currentCronContext(),
      accessKey: cronAccessKey(),
    };
    getRecordDataMock.mockResolvedValue({
      code: "200",
      data: { TRACE_ID: "trace", CRON_JOB_ID: "new-task" },
    });
    render(
      <HtmlPreviewTrackingProvider
        value={{ cronTaskId: "new-task", cronTaskOrigin: origin }}
      >
        <FilePreviewModal
          open
          onClose={() => {}}
          fileUrl="https://example.test/report[auto-preview].html?resultId=managed-result&templateId=1"
          fileName="report.html"
        />
      </HtmlPreviewTrackingProvider>,
    );
    await waitFor(() => expect(renderTemplateMock).toHaveBeenCalled());
    await waitFor(() =>
      expect(markTaskReferenceReadMock).toHaveBeenCalledWith(
        "new-task",
        origin,
        true,
        expect.any(String),
      ),
    );
    expect(markTaskReferenceReadMock).toHaveBeenCalledTimes(1);
  });
  it("reloads a changed HTML URL without letting an older response overwrite it", async () => {
    let resolveFirst!: (response: Response) => void;
    let resolveSecond!: (response: Response) => void;
    const firstResponse = new Promise<Response>((resolve) => {
      resolveFirst = resolve;
    });
    const secondResponse = new Promise<Response>((resolve) => {
      resolveSecond = resolve;
    });
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockReturnValueOnce(firstResponse)
      .mockReturnValueOnce(secondResponse);
    const createObjectURL = vi.fn<typeof URL.createObjectURL>(
      () => "blob:preview",
    );
    const originalCreateObjectURL = Object.getOwnPropertyDescriptor(
      URL,
      "createObjectURL",
    );
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: createObjectURL,
    });
    vi.stubGlobal("fetch", fetchMock);

    try {
      const { rerender } = render(
        <FilePreviewDrawer
          open
          onClose={vi.fn()}
          fileUrl="https://example.test/report-a.html"
          fileName="report.html"
        />,
      );
      await waitFor(() =>
        expect(fetchMock).toHaveBeenCalledWith(
          "https://example.test/report-a.html",
        ),
      );

      rerender(
        <FilePreviewDrawer
          open
          onClose={vi.fn()}
          fileUrl="https://example.test/report-b.html"
          fileName="report.html"
        />,
      );
      await waitFor(() =>
        expect(fetchMock).toHaveBeenCalledWith(
          "https://example.test/report-b.html",
        ),
      );

      await act(async () => {
        resolveSecond(
          new Response("<!doctype html><main>second</main>", { status: 200 }),
        );
      });
      await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1));

      await act(async () => {
        resolveFirst(
          new Response("<!doctype html><main>first</main>", { status: 200 }),
        );
      });
      await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1));
      const latestBlob = createObjectURL.mock.calls[
        createObjectURL.mock.calls.length - 1
      ]?.[0] as Blob;
      expect(await latestBlob.text()).toContain("second");
    } finally {
      vi.unstubAllGlobals();
      if (originalCreateObjectURL) {
        Object.defineProperty(URL, "createObjectURL", originalCreateObjectURL);
      } else {
        Reflect.deleteProperty(URL, "createObjectURL");
      }
    }
  });

  it("preserves the original bytes used by ordinary HTML previews", async () => {
    const sourceBytes = new Uint8Array([0x3c, 0x80, 0x3e]);
    const createObjectURL = vi.fn<typeof URL.createObjectURL>(
      () => "blob:preview",
    );
    const originalCreateObjectURL = Object.getOwnPropertyDescriptor(
      URL,
      "createObjectURL",
    );
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: createObjectURL,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(new Response(sourceBytes)),
    );

    try {
      render(
        <FilePreviewModal
          open
          onClose={vi.fn()}
          fileUrl="https://example.test/legacy.html"
          fileName="legacy.html"
        />,
      );

      await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1));
      const previewBlob = createObjectURL.mock.calls[0]?.[0] as Blob;
      expect(
        Array.from(new Uint8Array(await previewBlob.arrayBuffer())),
      ).toEqual(Array.from(sourceBytes));
    } finally {
      vi.unstubAllGlobals();
      if (originalCreateObjectURL) {
        Object.defineProperty(URL, "createObjectURL", originalCreateObjectURL);
      } else {
        Reflect.deleteProperty(URL, "createObjectURL");
      }
    }
  });

  it("does not decode canonical HTML for previews that did not enable annotations", async () => {
    const previewBlob = new Blob(["<!doctype html><main>report</main>"], {
      type: "text/html",
    });
    const readCanonicalText = vi.spyOn(previewBlob, "text");
    const createObjectURL = vi.fn<typeof URL.createObjectURL>(
      () => "blob:preview",
    );
    const originalCreateObjectURL = Object.getOwnPropertyDescriptor(
      URL,
      "createObjectURL",
    );
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: createObjectURL,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue({
        ok: true,
        blob: async () => previewBlob,
      } as Response),
    );

    try {
      render(
        <FilePreviewModal
          open
          onClose={vi.fn()}
          fileUrl="https://example.test/report.html"
          fileName="report.html"
        />,
      );

      await waitFor(() => expect(createObjectURL).toHaveBeenCalledOnce());
      expect(readCanonicalText).not.toHaveBeenCalled();
    } finally {
      vi.unstubAllGlobals();
      if (originalCreateObjectURL) {
        Object.defineProperty(URL, "createObjectURL", originalCreateObjectURL);
      } else {
        Reflect.deleteProperty(URL, "createObjectURL");
      }
    }
  });

  it("keeps the latest dynamic HTML when an older request resolves last", async () => {
    type DynamicResponse = {
      code: string;
      data: Record<string, unknown>;
    };
    let resolveFirst!: (response: DynamicResponse) => void;
    let resolveSecond!: (response: DynamicResponse) => void;
    const firstResponse = new Promise<DynamicResponse>((resolve) => {
      resolveFirst = resolve;
    });
    const secondResponse = new Promise<DynamicResponse>((resolve) => {
      resolveSecond = resolve;
    });
    getRecordDataMock
      .mockReturnValueOnce(firstResponse)
      .mockReturnValueOnce(secondResponse);
    renderTemplateMock.mockImplementation(
      async (_templateId: number, data: { marker: string }) =>
        `<html><body>${data.marker}</body></html>`,
    );

    const { rerender } = render(
      <FilePreviewDrawer
        open
        onClose={vi.fn()}
        fileUrl="https://example.test/report[auto-preview].html?resultId=result-a&templateId=1"
        fileName="report.html"
      />,
    );
    await waitFor(() =>
      expect(getRecordDataMock).toHaveBeenCalledWith("result-a", "1"),
    );

    rerender(
      <FilePreviewDrawer
        open
        onClose={vi.fn()}
        fileUrl="https://example.test/report[auto-preview].html?resultId=result-b&templateId=2"
        fileName="report.html"
      />,
    );
    await waitFor(() =>
      expect(getRecordDataMock).toHaveBeenCalledWith("result-b", "2"),
    );

    await act(async () => {
      resolveSecond({ code: "200", data: { marker: "second" } });
    });
    const iframe = await waitFor(() => {
      const current = document.querySelector("iframe") as HTMLIFrameElement;
      expect(current.srcdoc).toContain("second");
      return current;
    });

    await act(async () => {
      resolveFirst({ code: "200", data: { marker: "first" } });
    });
    expect(renderTemplateMock).toHaveBeenCalledTimes(1);
    expect(iframe.srcdoc).toContain("second");
    expect(iframe.srcdoc).not.toContain("first");
  });

  it("does not restart polling for an obsolete dynamic request", async () => {
    type DynamicResponse = {
      code: string;
      data: Record<string, unknown>;
    };
    let resolveFirst!: (response: DynamicResponse) => void;
    const firstResponse = new Promise<DynamicResponse>((resolve) => {
      resolveFirst = resolve;
    });
    getRecordDataMock
      .mockReturnValueOnce(firstResponse)
      .mockResolvedValueOnce({ code: "200", data: { marker: "second" } });
    renderTemplateMock.mockResolvedValue("<html><body>second</body></html>");

    const { rerender } = render(
      <FilePreviewDrawer
        open
        onClose={vi.fn()}
        fileUrl="https://example.test/report[auto-preview].html?resultId=result-a&templateId=1"
        fileName="report.html"
      />,
    );
    await waitFor(() =>
      expect(getRecordDataMock).toHaveBeenCalledWith("result-a", "1"),
    );

    rerender(
      <FilePreviewDrawer
        open
        onClose={vi.fn()}
        fileUrl="https://example.test/report[auto-preview].html?resultId=result-b&templateId=2"
        fileName="report.html"
      />,
    );
    await waitFor(() =>
      expect(
        document.querySelector("iframe")?.getAttribute("srcdoc"),
      ).toContain("second"),
    );

    vi.useFakeTimers();
    try {
      await act(async () => {
        resolveFirst({ code: "202", data: {} });
      });
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps the HTML drawer annotation-free with a writable composer", async () => {
    render(
      <HtmlAnnotationProvider activeChatKey="chat-1" composerAvailable>
        <FilePreviewDrawer
          open
          onClose={vi.fn()}
          fileUrl="https://example.test/report[auto-preview].html?resultId=result-1&templateId=1"
          fileName="report.html"
        />
      </HtmlAnnotationProvider>,
    );

    await waitFor(() => expect(renderTemplateMock).toHaveBeenCalled());
    expect(document.querySelector("iframe")).not.toBeNull();
    expect(screen.queryByRole("button", { name: "添加批注" })).toBeNull();
  });

  it("shows annotation for an explicitly enabled HTML workspace preview", async () => {
    render(
      <HtmlAnnotationProvider activeChatKey="chat-1" composerAvailable>
        <FilePreviewModal
          open
          onClose={vi.fn()}
          fileUrl="https://example.test/report[auto-preview].html?resultId=result-1&templateId=1"
          fileName="report.html"
          presentation="workspace"
          enableAnnotations
        />
        <HtmlAnnotationComposerSummary />
      </HtmlAnnotationProvider>,
    );

    fireEvent.click(await screen.findByRole("button", { name: "添加批注" }));
    await screen.findByText("选择页面元素并描述修改；批注不会改变当前页面。");
    const iframe = document.querySelector("iframe") as HTMLIFrameElement;
    iframe.contentDocument!.body.innerHTML =
      '<button id="workspace-target" type="button">打开详情</button>';
    fireEvent.click(
      iframe.contentDocument!.querySelector("#workspace-target")!,
    );
    const editor = await screen.findByRole("textbox", { name: "批注内容" });
    fireEvent.change(editor, { target: { value: "修改工作区按钮文字" } });
    fireEvent.click(screen.getByRole("button", { name: "保存批注" }));
    fireEvent.click(screen.getByRole("button", { name: /完成批注/ }));

    expect(await screen.findByText("1 条批注")).toBeVisible();
  });

  it("selects a DOM target without activating it and stages the saved comment", async () => {
    render(
      <HtmlAnnotationProvider activeChatKey="chat-1" composerAvailable>
        <FilePreviewModal
          presentation="drawer"
          enableAnnotations
          open
          onClose={vi.fn()}
          fileUrl="https://example.test/report[auto-preview].html?resultId=result-1&templateId=1"
          fileName="report.html"
        />
        <HtmlAnnotationComposerSummary />
      </HtmlAnnotationProvider>,
    );
    const addButton = await screen.findByRole("button", { name: "添加批注" });
    const iframe = document.querySelector("iframe") as HTMLIFrameElement;
    act(() => addButton.click());
    await screen.findByText("选择页面元素并描述修改；批注不会改变当前页面。");
    iframe.contentDocument!.body.innerHTML =
      '<button id="target" type="button">打开详情</button>';
    const target = iframe.contentDocument!.querySelector("#target")!;
    let activated = false;
    target.addEventListener("click", () => {
      activated = true;
    });

    await waitFor(() => {
      target.dispatchEvent(
        new MouseEvent("click", { bubbles: true, cancelable: true }),
      );
      expect(screen.getByRole("textbox", { name: "批注内容" })).toBeVisible();
    });
    expect(activated).toBe(false);
    const editor = screen.getByRole("textbox", { name: "批注内容" });
    act(() => {
      editor.dispatchEvent(
        new InputEvent("input", { bubbles: true, data: "修改按钮文字" }),
      );
    });
    // React's controlled textarea needs the testing-library change helper.
    fireEvent.change(editor, { target: { value: "修改按钮文字" } });
    fireEvent.click(screen.getByRole("button", { name: "保存批注" }));
    fireEvent.click(screen.getByRole("button", { name: /完成批注/ }));

    expect(await screen.findByText("1 条批注")).toBeVisible();
  });

  it("suppresses pre-click target handlers while selecting an annotation", async () => {
    render(
      <HtmlAnnotationProvider activeChatKey="chat-1" composerAvailable>
        <FilePreviewModal
          presentation="drawer"
          enableAnnotations
          open
          onClose={vi.fn()}
          fileUrl="https://example.test/report[auto-preview].html?resultId=result-1&templateId=1"
          fileName="report.html"
        />
      </HtmlAnnotationProvider>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "添加批注" }));
    const iframe = document.querySelector("iframe") as HTMLIFrameElement;
    iframe.contentDocument!.body.innerHTML =
      '<button id="target" type="button">打开详情</button>';
    const target = iframe.contentDocument!.querySelector("#target")!;
    const onMouseDown = vi.fn();
    target.addEventListener("mousedown", onMouseDown);

    target.dispatchEvent(
      new MouseEvent("mousedown", { bubbles: true, cancelable: true }),
    );

    expect(onMouseDown).not.toHaveBeenCalled();
  });

  it("rejects canvas content before resolving a meaningful ancestor", async () => {
    render(
      <HtmlAnnotationProvider activeChatKey="chat-1" composerAvailable>
        <FilePreviewModal
          presentation="drawer"
          enableAnnotations
          open
          onClose={vi.fn()}
          fileUrl="https://example.test/report[auto-preview].html?resultId=result-1&templateId=1"
          fileName="report.html"
        />
      </HtmlAnnotationProvider>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "添加批注" }));
    const iframe = document.querySelector("iframe") as HTMLIFrameElement;
    iframe.contentDocument!.body.innerHTML =
      '<main><section><canvas id="chart"></canvas></section></main>';

    fireEvent.click(iframe.contentDocument!.querySelector("#chart")!);

    expect(
      await screen.findByText(
        "该位置没有可寻址的 DOM 内容，无法创建可靠批注。",
      ),
    ).toBeVisible();
    expect(
      screen.queryByRole("textbox", { name: "批注内容" }),
    ).not.toBeInTheDocument();
  });

  it("does not record another preview view when annotation mode exits", async () => {
    render(
      <HtmlAnnotationProvider activeChatKey="chat-1" composerAvailable>
        <FilePreviewModal
          presentation="drawer"
          enableAnnotations
          open
          onClose={vi.fn()}
          fileUrl="https://example.test/report[auto-preview].html?resultId=result-1&templateId=1"
          fileName="report.html"
        />
      </HtmlAnnotationProvider>,
    );
    const addButton = await screen.findByRole("button", { name: "添加批注" });
    const previewViewCount = () =>
      recordClickMock.mock.calls.filter(
        ([payload]) => payload?.event_type === "preview_view",
      ).length;
    await waitFor(() => expect(previewViewCount()).toBeGreaterThan(0));
    const beforeToggle = previewViewCount();

    fireEvent.click(addButton);
    const iframe = document.querySelector("iframe") as HTMLIFrameElement;
    act(() => {
      iframe.contentDocument!.dispatchEvent(
        new KeyboardEvent("keydown", {
          key: "Escape",
          bubbles: true,
          cancelable: true,
        }),
      );
    });
    await screen.findByRole("button", { name: "添加批注" });

    expect(previewViewCount()).toBe(beforeToggle);
  });

  it("exits annotation mode when Escape is pressed in the comment editor", async () => {
    render(
      <HtmlAnnotationProvider activeChatKey="chat-1" composerAvailable>
        <FilePreviewModal
          presentation="drawer"
          enableAnnotations
          open
          onClose={vi.fn()}
          fileUrl="https://example.test/report[auto-preview].html?resultId=result-1&templateId=1"
          fileName="report.html"
        />
      </HtmlAnnotationProvider>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "添加批注" }));
    const iframe = document.querySelector("iframe") as HTMLIFrameElement;
    iframe.contentDocument!.body.innerHTML =
      '<button id="target" type="button">打开详情</button>';
    const target = iframe.contentDocument!.querySelector("#target")!;
    target.dispatchEvent(
      new MouseEvent("click", { bubbles: true, cancelable: true }),
    );
    const editor = await screen.findByRole("textbox", { name: "批注内容" });

    fireEvent.keyDown(editor, { key: "Escape" });

    expect(screen.getByRole("button", { name: "添加批注" })).toBeVisible();
    expect(screen.queryByRole("textbox", { name: "批注内容" })).toBeNull();
  });

  it("repositions a saved marker after iframe scrolling and DOM replacement", async () => {
    render(
      <HtmlAnnotationProvider activeChatKey="chat-1" composerAvailable>
        <FilePreviewModal
          presentation="drawer"
          enableAnnotations
          open
          onClose={vi.fn()}
          fileUrl="https://example.test/report[auto-preview].html?resultId=result-1&templateId=1"
          fileName="report.html"
        />
      </HtmlAnnotationProvider>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "添加批注" }));
    const iframe = document.querySelector("iframe") as HTMLIFrameElement;
    iframe.contentDocument!.body.innerHTML =
      '<button id="target" type="button">打开详情</button>';
    const target = iframe.contentDocument!.querySelector("#target")!;
    vi.spyOn(target, "getBoundingClientRect").mockReturnValue({
      left: 10,
      top: 20,
      width: 100,
      height: 30,
    } as DOMRect);

    await waitFor(() => {
      target.dispatchEvent(
        new MouseEvent("click", { bubbles: true, cancelable: true }),
      );
      expect(screen.getByRole("textbox", { name: "批注内容" })).toBeVisible();
    });
    fireEvent.change(screen.getByRole("textbox", { name: "批注内容" }), {
      target: { value: "修改按钮" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存批注" }));
    const marker = document.querySelector(
      'button[aria-label="编辑批注 1"]',
    ) as HTMLButtonElement;
    expect(marker).toBeTruthy();
    expect(marker).toHaveStyle({ left: "10px", top: "20px" });

    target.remove();
    const replacement = iframe.contentDocument!.createElement("button");
    replacement.id = "target";
    replacement.textContent = "打开详情";
    vi.spyOn(replacement, "getBoundingClientRect").mockReturnValue({
      left: 45,
      top: 80,
      width: 100,
      height: 30,
    } as DOMRect);
    iframe.contentDocument!.body.append(replacement);
    fireEvent.scroll(iframe.contentDocument!);

    await waitFor(() => {
      expect(marker).toHaveStyle({ left: "45px", top: "80px" });
    });
  });

  it("keeps interactive annotation markers exposed to assistive technology", async () => {
    render(
      <HtmlAnnotationProvider activeChatKey="chat-1" composerAvailable>
        <FilePreviewModal
          presentation="drawer"
          enableAnnotations
          open
          onClose={vi.fn()}
          fileUrl="https://example.test/report[auto-preview].html?resultId=result-1&templateId=1"
          fileName="report.html"
        />
      </HtmlAnnotationProvider>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "添加批注" }));
    const iframe = document.querySelector("iframe") as HTMLIFrameElement;
    iframe.contentDocument!.body.innerHTML =
      '<button id="target" type="button">打开详情</button>';
    const target = iframe.contentDocument!.querySelector("#target")!;

    await waitFor(() => {
      target.dispatchEvent(
        new MouseEvent("click", { bubbles: true, cancelable: true }),
      );
      expect(screen.getByRole("textbox", { name: "批注内容" })).toBeVisible();
    });
    fireEvent.change(screen.getByRole("textbox", { name: "批注内容" }), {
      target: { value: "修改按钮" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存批注" }));

    const marker = document.querySelector(
      'button[aria-label="编辑批注 1"]',
    ) as HTMLButtonElement;
    expect(marker).toBeTruthy();
    expect(marker.closest('[aria-hidden="true"]')).toBeNull();
  });

  it("keeps modal, non-HTML and unavailable-composer previews annotation free", async () => {
    const { rerender } = render(
      <HtmlAnnotationProvider activeChatKey="chat-1" composerAvailable={false}>
        <FilePreviewDrawer
          open
          onClose={vi.fn()}
          fileUrl="https://example.test/report[auto-preview].html?resultId=result-1&templateId=1"
          fileName="report.html"
        />
      </HtmlAnnotationProvider>,
    );
    await waitFor(() => expect(renderTemplateMock).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "添加批注" })).toBeNull();

    rerender(
      <HtmlAnnotationProvider activeChatKey="chat-1" composerAvailable>
        <FilePreviewModal
          open
          onClose={vi.fn()}
          fileUrl="https://example.test/report[auto-preview].html?resultId=result-1&templateId=1"
          fileName="report.html"
        />
      </HtmlAnnotationProvider>,
    );
    expect(screen.queryByRole("button", { name: "添加批注" })).toBeNull();

    rerender(
      <HtmlAnnotationProvider activeChatKey="chat-1" composerAvailable>
        <FilePreviewDrawer
          open
          onClose={vi.fn()}
          fileUrl="https://example.test/report.pdf"
          fileName="report.pdf"
        />
      </HtmlAnnotationProvider>,
    );
    expect(screen.queryByRole("button", { name: "添加批注" })).toBeNull();
  });
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
