import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { App, Modal } from "antd";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import FileColumn from "./FileColumn";
import FileManager from "./index";
import FileDetail from "./FileDetail";
import type { FileManagerDirectoryListing } from "@/api/modules/chat";

const { deleteDirectory, listDirectory, readFile } = vi.hoisted(() => ({
  deleteDirectory: vi.fn(),
  listDirectory: vi.fn(),
  readFile: vi.fn(),
}));

class ResizeObserverMock {
  observe() {}
  disconnect() {}
  unobserve() {}
}

vi.mock("@/api/modules/chat", () => ({
  chatApi: {
    fileManager: {
      listDirectory,
      readFile,
      saveText: vi.fn(),
      upload: vi.fn(),
      downloadUrl: vi.fn(() => "/download"),
      downloadFile: vi.fn(),
      archive: vi.fn(),
      deleteDirectory,
      restore: vi.fn(),
      purge: vi.fn(),
    },
  },
}));

vi.mock("@/components/agentscope-chat/FilePreviewModal/fileUtils", () => ({
  getFileIcon: () => ({ icon: <span>file</span>, color: "#1677ff" }),
  getContentType: () => "text/plain",
}));

vi.mock("@/components/agentscope-chat/Markdown", () => ({
  default: () => <div>Markdown</div>,
}));

vi.mock("@/components/agentscope-chat/FilePreviewModal", () => {
  function MockFilePreviewModal(props: {
    fileName: string;
    nestedPreviewMode?: string;
  }) {
    const [nested, setNested] = useState(false);
    return (
      <div
        data-testid="session-file-preview"
        data-nested-preview-mode={props.nestedPreviewMode}
      >
        {nested ? "二级预览" : props.fileName}
        <button type="button" onClick={() => setNested(true)}>
          模拟打开详情
        </button>
      </div>
    );
  }

  return { default: MockFilePreviewModal };
});

const rootPage = {
  root: "working" as const,
  path: "",
  items: [
    {
      name: "docs",
      path: "docs",
      kind: "directory" as const,
      capabilities: {
        browse: true,
        read: true,
        upload: true,
        edit: true,
        download: true,
        archive: true,
      },
    },
  ],
  next_cursor: null,
  has_child_directory: true,
  first_child_directory: "docs",
  capabilities: {
    browse: true,
    read: true,
    upload: true,
    edit: true,
    download: true,
    archive: true,
  },
};

function requestError(status: number, detail: string) {
  const error = new Error(detail) as Error & { status?: number };
  error.status = status;
  return error;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((complete) => {
    resolve = complete;
  });
  return { promise, resolve };
}

function scrollToBottom(element: HTMLElement) {
  Object.defineProperties(element, {
    clientHeight: { configurable: true, value: 100 },
    scrollHeight: { configurable: true, value: 200 },
    scrollTop: { configurable: true, value: 100 },
  });
  fireEvent.scroll(element);
}

describe("FileManager", () => {
  afterEach(() => {
    Modal.destroyAll();
    cleanup();
    document.querySelectorAll(".ant-modal-root").forEach((element) => {
      element.remove();
    });
    document.querySelector("[data-chat-shell]")?.remove();
    document.querySelector("[data-chat-messages-area]")?.remove();
    vi.unstubAllGlobals();
  });
  beforeEach(() => {
    vi.stubGlobal("ResizeObserver", ResizeObserverMock);
    listDirectory.mockReset();
    listDirectory.mockResolvedValue(rootPage);
    deleteDirectory.mockReset();
    readFile.mockReset();
  });

  it("opens a right-side drawer with shortcut toolbar and three column roles", async () => {
    render(
      <App>
        <FileManager />
      </App>,
    );

    fireEvent.click(screen.getByRole("button", { name: "文件管理器" }));

    expect(
      await screen.findByRole("dialog", { name: "文件管理器" }),
    ).toBeInTheDocument();
    expect(document.documentElement).toHaveClass(
      "copaw-file-manager-drawer-open",
    );
    const shortcuts = screen.getByRole("navigation", {
      name: "文件目录快捷方式",
    });
    expect(
      within(shortcuts).getByRole("button", { name: "工作目录" }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(
      within(shortcuts).getByRole("button", { name: "根目录" }),
    ).toHaveAttribute("aria-pressed", "false");
    expect(
      within(shortcuts)
        .getAllByRole("button")
        .map((button) => button.textContent?.trim()),
    ).toEqual([
      "工作目录",
      "根目录",
      "上传目录",
      "下载目录",
      "对话目录",
      "回收站",
    ]);
    expect(screen.getByLabelText("文件列表第 1 栏")).toBeInTheDocument();
    expect(screen.getByLabelText("文件列表第 2 栏")).toBeInTheDocument();
    expect(screen.getByLabelText("文件列表第 3 栏")).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: "全屏查看文件管理器" }),
    );
    expect(document.documentElement).toHaveClass(
      "copaw-file-manager-preview-fullscreen",
    );
    fireEvent.click(screen.getByRole("button", { name: "退出全屏" }));
    expect(document.documentElement).not.toHaveClass(
      "copaw-file-manager-preview-fullscreen",
    );

    fireEvent.click(screen.getByRole("button", { name: "关闭文件管理器" }));

    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "文件管理器" }),
      ).not.toBeInTheDocument(),
    );
    expect(document.documentElement).not.toHaveClass(
      "copaw-file-manager-drawer-open",
    );
  });

  it("opens a registered session file in the same file workspace", async () => {
    render(
      <App>
        <FileManager />
      </App>,
    );

    window.dispatchEvent(
      new CustomEvent("copaw:chat-workspace-file", {
        detail: {
          action: "open",
          fileName: "投资简报.html",
          fileUrl: "/files/report.html",
          enableClickTracking: true,
        },
      }),
    );

    expect(
      await screen.findByRole("dialog", { name: "文件管理器" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: "当前会话文件 1" }),
    ).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByLabelText("当前会话文件")).not.toBeInTheDocument();
    expect(screen.getByTestId("session-file-preview"))
      .toHaveTextContent("投资简报.html");
    expect(screen.getByTestId("session-file-preview")).toHaveAttribute(
      "data-nested-preview-mode",
      "replace",
    );
    fireEvent.click(screen.getByRole("button", { name: "模拟打开详情" }));
    expect(screen.getByTestId("session-file-preview")).toHaveTextContent(
      "二级预览",
    );

    window.dispatchEvent(
      new CustomEvent("copaw:chat-workspace-file", {
        detail: {
          action: "register",
          fileName: "会议纪要.md",
          fileUrl: "/files/meeting.md",
          enableClickTracking: false,
        },
      }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "展开会话文件列表" }),
    );
    expect(screen.getByLabelText("当前会话文件")).toHaveTextContent(
      "投资简报.html",
    );
    fireEvent.click(screen.getByRole("button", { name: /会议纪要\.md/ }));
    expect(screen.getByTestId("session-file-preview")).toHaveTextContent(
      "会议纪要.md",
    );
    expect(screen.getByTestId("session-file-preview")).not.toHaveTextContent(
      "二级预览",
    );
    fireEvent.click(
      screen.getByRole("button", { name: "收起会话文件列表" }),
    );
    expect(screen.queryByLabelText("当前会话文件")).not.toBeInTheDocument();
  });

  it("permanently deletes a directory after confirmation without opening it", async () => {
    deleteDirectory.mockResolvedValue(undefined);
    render(
      <App>
        <FileManager />
      </App>,
    );

    fireEvent.click(screen.getByRole("button", { name: "文件管理器" }));
    fireEvent.click(
      await within(screen.getByLabelText("文件列表第 2 栏")).findByRole(
        "button",
        { name: "永久删除目录 docs" },
      ),
    );

    expect(
      await screen.findByText("目录及其全部内容将被永久删除，无法恢复。"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "永久删除" }));

    await waitFor(() =>
      expect(deleteDirectory).toHaveBeenCalledWith({
        root: "working",
        path: "docs",
      }),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "永久删除目录？" }),
      ).not.toBeInTheDocument(),
    );
    expect(listDirectory).toHaveBeenCalledTimes(4);
  });

  it("loads the tenant source scope from the root shortcut", async () => {
    listDirectory.mockImplementation(
      ({ root, path }: { root: string; path: string }) =>
        Promise.resolve(
          root === "source_scope" && path === ""
            ? { ...rootPage, root: "source_scope", items: [] }
            : rootPage,
        ),
    );
    render(<FileManager />);

    fireEvent.click(screen.getByRole("button", { name: "文件管理器" }));
    fireEvent.click(await screen.findByRole("button", { name: "根目录" }));

    await waitFor(() =>
      expect(listDirectory).toHaveBeenLastCalledWith(
        expect.objectContaining({ root: "source_scope", path: "" }),
      ),
    );
  });

  it("refreshes a column from its first page after a pagination conflict", async () => {
    const pagedRoot = {
      ...rootPage,
      items: [
        ...rootPage.items,
        {
          name: "old.txt",
          path: "old.txt",
          kind: "file" as const,
          capabilities: rootPage.capabilities,
        },
      ],
      next_cursor: "cursor-1",
    };
    const docsPage = { ...rootPage, path: "docs", items: [] };
    const refreshedRoot = {
      ...rootPage,
      items: [
        {
          name: "fresh.txt",
          path: "fresh.txt",
          kind: "file" as const,
          capabilities: rootPage.capabilities,
        },
      ],
      next_cursor: "cursor-new",
    };
    listDirectory.mockImplementation(
      ({ path, cursor }: { path: string; cursor?: string }) => {
        if (path === "" && cursor === "cursor-1") {
          return Promise.reject(
            requestError(409, "Directory listing changed; refresh and retry"),
          );
        }
        if (path === "" && listDirectory.mock.calls.length > 2) {
          return Promise.resolve(refreshedRoot);
        }
        return Promise.resolve(path === "docs" ? docsPage : pagedRoot);
      },
    );
    render(
      <App>
        <FileManager />
      </App>,
    );

    fireEvent.click(screen.getByRole("button", { name: "文件管理器" }));
    const middle = await screen.findByLabelText("文件列表第 2 栏");
    scrollToBottom(middle);

    await waitFor(() =>
      expect(listDirectory).toHaveBeenNthCalledWith(
        3,
        expect.objectContaining({ path: "", cursor: "cursor-1" }),
      ),
    );
    await waitFor(() =>
      expect(listDirectory).toHaveBeenNthCalledWith(
        4,
        expect.objectContaining({ path: "", cursor: null }),
      ),
    );
    expect(
      await within(middle).findByRole("button", { name: "fresh.txt" }),
    ).toBeInTheDocument();
    expect(
      within(middle).queryByRole("button", { name: "old.txt" }),
    ).not.toBeInTheDocument();
  });

  it("keeps the error panel for a non-conflict pagination failure", async () => {
    const pagedRoot = { ...rootPage, next_cursor: "cursor-1" };
    listDirectory.mockImplementation(
      ({ path, cursor }: { path: string; cursor?: string }) => {
        if (path === "" && cursor === "cursor-1") {
          return Promise.reject(requestError(500, "server unavailable"));
        }
        return Promise.resolve(
          path === "docs"
            ? { ...rootPage, path: "docs", items: [] }
            : pagedRoot,
        );
      },
    );
    render(<FileManager />);

    fireEvent.click(screen.getByRole("button", { name: "文件管理器" }));
    const middle = await screen.findByLabelText("文件列表第 2 栏");
    scrollToBottom(middle);

    expect(
      await within(middle).findByText("server unavailable"),
    ).toBeInTheDocument();
    expect(listDirectory).toHaveBeenCalledTimes(3);
  });

  it("does not apply a recovery response while the new root is loading", async () => {
    const recovery = deferred<typeof rootPage>();
    const sourceScopeRoot = deferred<FileManagerDirectoryListing>();
    const pagedRoot = { ...rootPage, next_cursor: "cursor-1" };
    let workingRootRequests = 0;
    listDirectory.mockImplementation(
      ({
        root,
        path,
        cursor,
      }: {
        root: string;
        path: string;
        cursor?: string;
      }) => {
        if (root === "working" && path === "" && cursor === "cursor-1") {
          return Promise.reject(
            requestError(409, "Directory listing changed; refresh and retry"),
          );
        }
        if (root === "working" && path === "") {
          workingRootRequests += 1;
          return workingRootRequests === 1
            ? Promise.resolve(pagedRoot)
            : recovery.promise;
        }
        if (root === "source_scope") return sourceScopeRoot.promise;
        return Promise.resolve(
          path === "docs"
            ? { ...rootPage, path: "docs", items: [] }
            : pagedRoot,
        );
      },
    );
    render(<FileManager />);

    fireEvent.click(screen.getByRole("button", { name: "文件管理器" }));
    const middle = await screen.findByLabelText("文件列表第 2 栏");
    scrollToBottom(middle);
    await waitFor(() => expect(listDirectory).toHaveBeenCalledTimes(4));

    fireEvent.click(screen.getByRole("button", { name: "根目录" }));
    await waitFor(() =>
      expect(listDirectory).toHaveBeenCalledWith(
        expect.objectContaining({ root: "source_scope", path: "" }),
      ),
    );
    await act(async () => {
      recovery.resolve({
        ...rootPage,
        items: [{ ...rootPage.items[0], name: "stale.txt", path: "stale.txt" }],
      });
      await recovery.promise;
    });

    expect(
      within(middle).queryByRole("button", { name: "stale.txt" }),
    ).not.toBeInTheDocument();
    await act(async () => {
      sourceScopeRoot.resolve({ ...rootPage, root: "source_scope", items: [] });
      await sourceScopeRoot.promise;
    });
  });

  it("does not issue duplicate cursor requests for rapid bottom scroll events", async () => {
    const nextPage = deferred<typeof rootPage>();
    const pagedRoot = { ...rootPage, next_cursor: "cursor-1" };
    listDirectory.mockImplementation(
      ({ path, cursor }: { path: string; cursor?: string }) => {
        if (path === "" && cursor === "cursor-1") return nextPage.promise;
        return Promise.resolve(
          path === "docs"
            ? { ...rootPage, path: "docs", items: [] }
            : pagedRoot,
        );
      },
    );
    render(<FileManager />);

    fireEvent.click(screen.getByRole("button", { name: "文件管理器" }));
    const middle = await screen.findByLabelText("文件列表第 2 栏");
    scrollToBottom(middle);
    scrollToBottom(middle);

    expect(
      listDirectory.mock.calls.filter(
        ([params]) => params.cursor === "cursor-1",
      ),
    ).toHaveLength(1);
    nextPage.resolve({ ...rootPage, next_cursor: null });
  });

  it("anchors a shortcut root in the left column before listing its contents in the middle column", async () => {
    const docsPage = {
      ...rootPage,
      path: "docs",
      items: [
        {
          name: "guides",
          path: "docs/guides",
          kind: "directory" as const,
          capabilities: rootPage.capabilities,
        },
      ],
    };
    listDirectory.mockImplementation(({ path }: { path: string }) =>
      Promise.resolve(path === "docs" ? docsPage : rootPage),
    );
    render(<FileManager />);

    fireEvent.click(screen.getByRole("button", { name: "文件管理器" }));

    const left = await screen.findByLabelText("文件列表第 1 栏");
    const middle = screen.getByLabelText("文件列表第 2 栏");
    const right = screen.getByLabelText("文件列表第 3 栏");
    expect(
      within(left).getByRole("button", { name: "工作目录" }),
    ).toBeInTheDocument();
    expect(
      within(middle).getByRole("button", { name: "docs" }),
    ).toBeInTheDocument();
    expect(
      within(right).getByRole("button", { name: "guides" }),
    ).toBeInTheDocument();
  });

  it("does not offer permanent deletion for the virtual root anchor", async () => {
    render(<FileManager />);

    fireEvent.click(screen.getByRole("button", { name: "文件管理器" }));

    expect(
      within(await screen.findByLabelText("文件列表第 1 栏")).queryByRole(
        "button",
        { name: "永久删除目录 工作目录" },
      ),
    ).not.toBeInTheDocument();
  });

  it("backfills the shortcut anchor when a folder is selected from the left column", async () => {
    const rootWithProjects = {
      ...rootPage,
      items: [
        {
          name: "docs",
          path: "docs",
          kind: "directory" as const,
          capabilities: rootPage.capabilities,
        },
        {
          name: "projects",
          path: "projects",
          kind: "directory" as const,
          capabilities: rootPage.capabilities,
        },
      ],
    };
    const docsPage = {
      ...rootPage,
      path: "docs",
      items: [
        {
          name: "guides",
          path: "docs/guides",
          kind: "directory" as const,
          capabilities: rootPage.capabilities,
        },
      ],
    };
    const guidesPage = {
      ...rootPage,
      path: "docs/guides",
      items: [
        {
          name: "chapter.md",
          path: "docs/guides/chapter.md",
          kind: "file" as const,
          capabilities: rootPage.capabilities,
        },
      ],
    };
    const projectsPage = {
      ...rootPage,
      path: "projects",
      items: [
        {
          name: "plan.md",
          path: "projects/plan.md",
          kind: "file" as const,
          capabilities: rootPage.capabilities,
        },
      ],
    };
    listDirectory.mockImplementation(({ path }: { path: string }) =>
      Promise.resolve(
        {
          "": rootWithProjects,
          docs: docsPage,
          "docs/guides": guidesPage,
          projects: projectsPage,
        }[path] || rootWithProjects,
      ),
    );
    render(<FileManager />);

    fireEvent.click(screen.getByRole("button", { name: "文件管理器" }));
    const right = await screen.findByLabelText("文件列表第 3 栏");
    fireEvent.click(
      await within(right).findByRole("button", { name: "guides" }),
    );
    fireEvent.click(
      await within(screen.getByLabelText("文件列表第 1 栏")).findByRole(
        "button",
        { name: "projects" },
      ),
    );

    expect(
      await within(screen.getByLabelText("文件列表第 1 栏")).findByRole(
        "button",
        { name: "工作目录" },
      ),
    ).toBeInTheDocument();
    expect(
      await within(screen.getByLabelText("文件列表第 2 栏")).findByRole(
        "button",
        { name: "projects" },
      ),
    ).toBeInTheDocument();
    expect(
      await within(screen.getByLabelText("文件列表第 3 栏")).findByRole(
        "button",
        { name: "plan.md" },
      ),
    ).toBeInTheDocument();
  });

  it("moves the right directory into the middle column before loading its child", async () => {
    const docsPage = {
      ...rootPage,
      path: "docs",
      items: [
        {
          name: "guides",
          path: "docs/guides",
          kind: "directory" as const,
          capabilities: rootPage.capabilities,
        },
      ],
    };
    const guidesPage = {
      ...rootPage,
      path: "docs/guides",
      items: [
        {
          name: "chapter.md",
          path: "docs/guides/chapter.md",
          kind: "file" as const,
          capabilities: rootPage.capabilities,
        },
      ],
    };
    listDirectory.mockImplementation(({ path }: { path: string }) =>
      Promise.resolve(
        path === "docs"
          ? docsPage
          : path === "docs/guides"
          ? guidesPage
          : rootPage,
      ),
    );
    render(<FileManager />);

    fireEvent.click(screen.getByRole("button", { name: "文件管理器" }));
    const right = await screen.findByLabelText("文件列表第 3 栏");
    fireEvent.click(
      await within(right).findByRole("button", { name: "guides" }),
    );

    expect(
      await within(screen.getByLabelText("文件列表第 1 栏")).findByRole(
        "button",
        { name: "docs" },
      ),
    ).toBeInTheDocument();
    expect(
      await within(screen.getByLabelText("文件列表第 2 栏")).findByRole(
        "button",
        { name: "guides" },
      ),
    ).toBeInTheDocument();
    expect(
      await within(screen.getByLabelText("文件列表第 3 栏")).findByRole(
        "button",
        { name: "chapter.md" },
      ),
    ).toBeInTheDocument();
  });

  it("explains why uploads are unavailable in conversation and recycle roots", async () => {
    render(<FileManager />);
    fireEvent.click(screen.getByRole("button", { name: "文件管理器" }));
    await screen.findByRole("dialog", { name: "文件管理器" });

    fireEvent.click(screen.getByRole("button", { name: "对话目录" }));
    expect(
      await screen.findByText("对话目录仅供浏览，不能上传文件。"),
    ).toBeInTheDocument();
  });

  it("keeps the available 1 MB text visible while marking a truncated preview read-only", () => {
    render(
      <FileDetail
        entry={{
          name: "large.txt",
          path: "large.txt",
          kind: "file",
          capabilities: rootPage.capabilities,
        }}
        preview={{
          path: "large.txt",
          size_bytes: 2_000_000,
          is_text: true,
          content: "first megabyte",
          is_truncated: true,
          editable: false,
          revision: "r1",
        }}
        editable
        onDownload={() => undefined}
        onSave={async () => undefined}
        onArchive={() => undefined}
        onRestore={() => undefined}
        onPurge={() => undefined}
      />,
    );

    expect(screen.getByText("仅预览前 1 MB 内容")).toBeInTheDocument();
    expect(screen.getByText("first megabyte")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "编辑" }),
    ).not.toBeInTheDocument();
  });

  it("does not render the selected file as a navigable breadcrumb", async () => {
    const docsPage = {
      ...rootPage,
      path: "docs",
      items: [
        {
          name: "note.txt",
          path: "docs/note.txt",
          kind: "file" as const,
          capabilities: rootPage.capabilities,
        },
      ],
    };
    listDirectory.mockImplementation(({ path }: { path: string }) =>
      Promise.resolve(path === "" ? rootPage : docsPage),
    );
    readFile.mockResolvedValue({
      path: "docs/note.txt",
      size_bytes: 5,
      is_text: true,
      content: "hello",
      is_truncated: false,
      editable: true,
      revision: "r1",
    });
    render(<FileManager />);
    fireEvent.click(screen.getByRole("button", { name: "文件管理器" }));
    await screen.findByRole("button", { name: "note.txt" });

    fireEvent.click(screen.getByRole("button", { name: "note.txt" }));
    await screen.findByRole("region", { name: "文件详情" });
    expect(screen.getAllByRole("button", { name: "note.txt" })).toHaveLength(1);
  });

  it("keeps directory columns compact with an item count and no disclosure arrow", () => {
    render(
      <FileColumn
        column={1}
        directory={rootPage}
        selectedPath={null}
        onSelect={() => undefined}
      />,
    );

    expect(screen.getByText("1 项")).toBeInTheDocument();
    expect(screen.queryByText("工作区")).not.toBeInTheDocument();
    expect(screen.queryByText("›")).not.toBeInTheDocument();
  });

  it("keeps file size out of directory rows", () => {
    render(
      <FileColumn
        column={1}
        directory={{
          ...rootPage,
          items: [
            {
              name: "report.txt",
              path: "report.txt",
              kind: "file",
              size_bytes: 1536,
              modified_at: "2026-07-29T00:00:00Z",
              capabilities: rootPage.capabilities,
            },
          ],
        }}
        selectedPath={null}
        onSelect={() => undefined}
      />,
    );

    expect(screen.getByText("7/29/2026", { exact: false })).toBeInTheDocument();
    expect(screen.queryByText("1.5 KB")).not.toBeInTheDocument();
  });

  it("shows file size in details and abandons the draft by leaving edit mode", () => {
    render(
      <FileDetail
        entry={{
          name: "note.txt",
          path: "note.txt",
          kind: "file",
          size_bytes: 1536,
          capabilities: rootPage.capabilities,
        }}
        preview={{
          path: "note.txt",
          size_bytes: 1536,
          is_text: true,
          content: "original",
          is_truncated: false,
          editable: true,
          revision: "r1",
        }}
        editable
        onDownload={() => undefined}
        onSave={async () => undefined}
        onArchive={() => undefined}
        onRestore={() => undefined}
        onPurge={() => undefined}
      />,
    );

    fireEvent.click(screen.getByRole("tab", { name: "详情" }));
    expect(
      screen.getByRole("row", { name: "大小 1.5 KB" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "预览" }));
    fireEvent.click(screen.getByRole("button", { name: /编辑/ }));
    fireEvent.change(screen.getByLabelText("文件内容"), {
      target: { value: "draft" },
    });
    fireEvent.click(screen.getByRole("button", { name: /放弃修改/ }));

    expect(screen.queryByLabelText("文件内容")).not.toBeInTheDocument();
    expect(screen.getByText("original")).toBeInTheDocument();
  });
});
