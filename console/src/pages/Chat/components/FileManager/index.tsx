import {
  App,
  Breadcrumb,
  Button,
  Drawer,
  Empty,
  Input,
  Modal,
  Tabs,
  Tooltip,
} from "antd";
import {
  CloseOutlined,
  DeleteOutlined,
  DownloadOutlined,
  FolderOpenOutlined,
  FullscreenExitOutlined,
  FullscreenOutlined,
  HomeOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  MessageOutlined,
  SearchOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  chatApi,
  type FileManagerDirectoryListing,
  type FileManagerItem,
  type FileManagerRoot,
  type FileManagerTextPreview,
} from "@/api/modules/chat";
import FileColumn from "./FileColumn";
import FileDetail, { type FileDetailHandle } from "./FileDetail";
import FilePreviewModal from "@/components/agentscope-chat/FilePreviewModal";
import {
  CHAT_WORKSPACE_FILE_EVENT,
  type ChatWorkspaceFile,
  type ChatWorkspaceFileEventDetail,
} from "@/components/agentscope-chat/FileWorkspaceEvents";
import styles from "./index.module.less";

type Columns = [
  FileManagerDirectoryListing | null,
  FileManagerDirectoryListing | null,
  FileManagerDirectoryListing | null,
];
type Selected = [string | null, string | null, string | null];
type ColumnQueries = [string, string, string];
type PendingAction = (() => void) | null;
type WorkspaceTab = "workspace" | "session";
type FileManagerApi = typeof chatApi.fileManager;

interface FileManagerProps {
  fileManagerApi?: FileManagerApi;
}

const virtualAnchorPrefix = "__file_manager_anchor__:";

const shortcutRoots: Array<{
  root: FileManagerRoot;
  label: string;
  icon: React.ReactNode;
}> = [
  { root: "working", label: "工作目录", icon: <FolderOpenOutlined /> },
  { root: "source_scope", label: "根目录", icon: <FolderOpenOutlined /> },
  { root: "upload", label: "上传目录", icon: <UploadOutlined /> },
  { root: "download", label: "下载目录", icon: <DownloadOutlined /> },
  { root: "conversation", label: "对话目录", icon: <MessageOutlined /> },
  { root: "recycle", label: "回收站", icon: <DeleteOutlined /> },
];

function uploadDisabledReason(root: FileManagerRoot) {
  if (root === "conversation") return "对话目录仅供浏览，不能上传文件。";
  if (root === "recycle") return "回收站只支持还原或永久删除，不能上传文件。";
  return "";
}

function directoryAnchor(
  root: FileManagerRoot,
  path: string,
  capabilities: FileManagerDirectoryListing["capabilities"],
): FileManagerDirectoryListing {
  const label =
    path.split("/").filter(Boolean).slice(-1)[0] ||
    shortcutRoots.find((item) => item.root === root)?.label ||
    "工作目录";
  const anchorPath = `${virtualAnchorPrefix}${path}`;
  return {
    root,
    path: anchorPath,
    items: [
      {
        name: label,
        path: anchorPath,
        kind: "directory",
        capabilities: { ...capabilities, archive: false },
      },
    ],
    next_cursor: null,
    has_child_directory: true,
    first_child_directory: null,
    capabilities,
  };
}

function isDirectoryAnchor(directory: FileManagerDirectoryListing | null) {
  return Boolean(directory?.path.startsWith(virtualAnchorPrefix));
}

function parentPath(path: string) {
  const parts = path.split("/").filter(Boolean);
  parts.pop();
  return parts.join("/");
}

function breadcrumbItems(
  root: FileManagerRoot,
  path: string,
  onNavigate: (path: string) => void,
) {
  const rootLabel =
    shortcutRoots.find((item) => item.root === root)?.label || "工作目录";
  const parts = path ? path.split("/").filter(Boolean) : [];
  return [
    {
      title: (
        <button
          type="button"
          className={styles.crumbButton}
          onClick={() => onNavigate("")}
        >
          <HomeOutlined /> {rootLabel}
        </button>
      ),
    },
    ...parts.map((part, index) => {
      const target = parts.slice(0, index + 1).join("/");
      return {
        title: (
          <button
            type="button"
            className={styles.crumbButton}
            onClick={() => onNavigate(target)}
          >
            {part}
          </button>
        ),
      };
    }),
  ];
}

function requestError(error: unknown) {
  return error instanceof Error ? error.message : "请求失败，请重试";
}

type RequestError = Error & { status?: number };

function isListingConflict(error: unknown): error is RequestError {
  return error instanceof Error && (error as RequestError).status === 409;
}

export default function FileManager({
  fileManagerApi = chatApi.fileManager,
}: FileManagerProps) {
  const { message } = App.useApp();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const detailRef = useRef<FileDetailHandle>(null);
  const loadingPageKeys = useRef<Set<string>>(new Set());
  const navigationVersionRef = useRef(0);
  const [open, setOpen] = useState(false);
  const [root, setRoot] = useState<FileManagerRoot>("working");
  const [columns, setColumns] = useState<Columns>([null, null, null]);
  const [columnQueries, setColumnQueries] = useState<ColumnQueries>([
    "",
    "",
    "",
  ]);
  const [selected, setSelected] = useState<Selected>([null, null, null]);
  const [detail, setDetail] = useState<FileManagerItem | null>(null);
  const [preview, setPreview] = useState<FileManagerTextPreview | null>(null);
  const [binaryPreviewUrl, setBinaryPreviewUrl] = useState<string | null>(null);
  const [loadingColumns, setLoadingColumns] = useState<boolean[]>([
    false,
    false,
    false,
  ]);
  const [columnErrors, setColumnErrors] = useState<Array<string | null>>([
    null,
    null,
    null,
  ]);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [dirty, setDirty] = useState(false);
  const [pendingAction, setPendingAction] = useState<PendingAction>(null);
  const [guardOpen, setGuardOpen] = useState(false);
  const [activeTab, setActiveTab] = useState<WorkspaceTab>("workspace");
  const [sessionFiles, setSessionFiles] = useState<ChatWorkspaceFile[]>([]);
  const [sessionPreview, setSessionPreview] =
    useState<ChatWorkspaceFile | null>(null);
  const [sessionListCollapsed, setSessionListCollapsed] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);

  const currentDirectory = columns[1] || columns[0];
  const uploadReason = uploadDisabledReason(root);
  const canDeleteDirectories = root !== "conversation" && root !== "recycle";

  const revokeBinaryPreview = useCallback(() => {
    setBinaryPreviewUrl((url) => {
      if (url) URL.revokeObjectURL(url);
      return null;
    });
  }, []);

  useEffect(
    () => () => {
      if (binaryPreviewUrl) URL.revokeObjectURL(binaryPreviewUrl);
    },
    [binaryPreviewUrl],
  );

  useEffect(() => {
    const handleWorkspaceFile = (event: Event) => {
      const detail = (event as CustomEvent<ChatWorkspaceFileEventDetail>)
        .detail;
      if (!detail?.fileUrl || !detail.fileName) return;
      const file = {
        fileName: detail.fileName,
        fileUrl: detail.fileUrl,
        enableClickTracking: detail.enableClickTracking,
      };
      setSessionFiles((previous) => {
        const withoutCurrent = previous.filter(
          (item) => item.fileUrl !== file.fileUrl,
        );
        return [...withoutCurrent, file];
      });
      if (detail.action === "open") {
        setSessionPreview(file);
        setSessionListCollapsed(true);
        setFullscreen(false);
        setActiveTab("session");
        setOpen(true);
      }
    };
    window.addEventListener(CHAT_WORKSPACE_FILE_EVENT, handleWorkspaceFile);
    return () =>
      window.removeEventListener(
        CHAT_WORKSPACE_FILE_EVENT,
        handleWorkspaceFile,
      );
  }, []);

  const setColumnLoading = useCallback((index: number, value: boolean) => {
    setLoadingColumns((previous) =>
      previous.map((item, position) => (position === index ? value : item)),
    );
  }, []);
  const setColumnError = useCallback((index: number, value: string | null) => {
    setColumnErrors((previous) =>
      previous.map((item, position) => (position === index ? value : item)),
    );
  }, []);

  const loadDirectory = useCallback(
    async (
      targetRoot: FileManagerRoot,
      path: string,
      cursor?: string | null,
      query?: string,
    ) => {
      return fileManagerApi.listDirectory({
        root: targetRoot,
        path,
        cursor,
        query,
      });
    },
    [fileManagerApi],
  );

  const loadInitial = useCallback(
    async (targetRoot: FileManagerRoot) => {
      setLoadingColumns([true, true, true]);
      setColumnErrors([null, null, null]);
      setDetail(null);
      setPreview(null);
      revokeBinaryPreview();
      setDetailError(null);
      setColumnQueries(["", "", ""]);
      setSearch("");
      try {
        const rootPage = await loadDirectory(targetRoot, "");
        const firstFolder = rootPage.items.find(
          (item) => item.kind === "directory",
        );
        const right = firstFolder
          ? await loadDirectory(targetRoot, firstFolder.path)
          : null;
        setColumns([
          directoryAnchor(targetRoot, "", rootPage.capabilities),
          rootPage,
          right,
        ]);
        setSelected([
          `${virtualAnchorPrefix}`,
          firstFolder?.path || null,
          null,
        ]);
      } catch (error) {
        setColumnErrors([requestError(error), null, null]);
        setColumns([null, null, null]);
      } finally {
        setLoadingColumns([false, false, false]);
      }
    },
    [loadDirectory, revokeBinaryPreview],
  );

  useEffect(() => {
    if (open) void loadInitial(root);
  }, [loadInitial, open, root]);

  useEffect(() => {
    if (!open) return;
    document.documentElement.classList.add("copaw-file-manager-drawer-open");
    return () => {
      document.documentElement.classList.remove(
        "copaw-file-manager-drawer-open",
      );
    };
  }, [open]);

  useEffect(() => {
    if (!open || !fullscreen) return;
    document.documentElement.classList.add(
      "copaw-file-manager-preview-fullscreen",
    );
    return () => {
      document.documentElement.classList.remove(
        "copaw-file-manager-preview-fullscreen",
      );
    };
  }, [fullscreen, open]);

  const executeOrGuard = useCallback(
    (action: () => void) => {
      if (!dirty) {
        action();
        return;
      }
      setPendingAction(() => action);
      setGuardOpen(true);
    },
    [dirty],
  );

  const closeFileManager = useCallback(() => {
    executeOrGuard(() => {
      setFullscreen(false);
      setOpen(false);
    });
  }, [executeOrGuard]);

  const readEntry = useCallback(
    async (entry: FileManagerItem) => {
      revokeBinaryPreview();
      if (entry.kind !== "file" || !entry.capabilities.read) {
        setPreview(null);
        setDetailError(entry.kind === "symlink" ? "符号链接不能读取。" : null);
        return;
      }
      const ext = entry.name.split(".").pop()?.toLowerCase();
      const isBinaryPreview = [
        "png",
        "jpg",
        "jpeg",
        "gif",
        "webp",
        "svg",
        "mp4",
        "webm",
        "mp3",
        "wav",
        "ogg",
        "pdf",
      ].includes(ext || "");
      setDetailLoading(true);
      setDetailError(null);
      try {
        if (isBinaryPreview) {
          const { blob } = await fileManagerApi.downloadFile({
            root,
            path: entry.path,
          });
          setBinaryPreviewUrl(URL.createObjectURL(blob));
          setPreview(null);
        } else {
          setPreview(
            await fileManagerApi.readFile({ root, path: entry.path }),
          );
        }
      } catch (error) {
        setPreview(null);
        setDetailError(requestError(error));
      } finally {
        setDetailLoading(false);
      }
    },
    [fileManagerApi, revokeBinaryPreview, root],
  );

  const selectEntry = useCallback(
    async (index: 0 | 1 | 2, entry: FileManagerItem) => {
      const perform = async () => {
        setDetail(null);
        setPreview(null);
        revokeBinaryPreview();
        setDetailError(null);
        if (entry.kind === "directory") {
          if (index === 0 && isDirectoryAnchor(columns[0])) return;
          const destinationIndex = index === 0 ? 2 : Math.min(index + 1, 2);
          setColumnLoading(destinationIndex, true);
          setColumnError(destinationIndex, null);
          try {
            const page = await loadDirectory(root, entry.path);
            if (index === 2) {
              setColumns((previous) => [previous[1], previous[2], page]);
              setColumnQueries((previous) => [previous[1], previous[2], ""]);
              setSelected((previous) => [previous[1], entry.path, null]);
            } else if (index === 1) {
              setColumns((previous) => [previous[0], previous[1], page]);
              setColumnQueries((previous) => [previous[0], previous[1], ""]);
              setSelected((previous) => [previous[0], entry.path, null]);
            } else {
              const previousLeft = columns[0];
              if (!previousLeft) return;
              const ancestor = previousLeft.path
                ? await loadDirectory(root, parentPath(previousLeft.path))
                : directoryAnchor(root, "", previousLeft.capabilities);
              setColumns([ancestor, previousLeft, page]);
              setColumnQueries((previous) => ["", previous[0], ""]);
              setSelected([
                previousLeft.path || `${virtualAnchorPrefix}`,
                entry.path,
                null,
              ]);
            }
          } catch (error) {
            setColumnError(destinationIndex, requestError(error));
          } finally {
            setColumnLoading(destinationIndex, false);
          }
          return;
        }
        if (index === 2) {
          setColumns((previous) => [previous[1], previous[2], null]);
          setColumnQueries((previous) => [previous[1], previous[2], ""]);
          setSelected((previous) => [previous[1], entry.path, null]);
        } else {
          setSelected(
            (previous) =>
              previous.map((path, position) =>
                position === index
                  ? entry.path
                  : position > index
                  ? null
                  : path,
              ) as Selected,
          );
        }
        setDetail(entry);
        await readEntry(entry);
      };
      executeOrGuard(() => {
        void perform();
      });
    },
    [
      columns,
      executeOrGuard,
      loadDirectory,
      readEntry,
      revokeBinaryPreview,
      root,
      setColumnError,
      setColumnLoading,
    ],
  );

  const loadMore = useCallback(
    async (index: 0 | 1 | 2) => {
      const page = columns[index];
      const cursor = page?.next_cursor;
      const query = columnQueries[index] || undefined;
      if (!page || !cursor || loadingColumns[index]) return;
      const pageKey = JSON.stringify([index, root, page.path, query, cursor]);
      if (loadingPageKeys.current.has(pageKey)) return;
      loadingPageKeys.current.add(pageKey);
      const navigationVersion = navigationVersionRef.current;
      const isCurrentNavigation = () =>
        navigationVersionRef.current === navigationVersion;
      setColumnLoading(index, true);
      try {
        const next = await loadDirectory(root, page.path, cursor, query);
        if (!isCurrentNavigation()) return;
        setColumns(
          (previous) =>
            previous.map((item, position) =>
              position === index &&
              item &&
              item.root === page.root &&
              item.path === page.path &&
              item.next_cursor === cursor
                ? { ...next, items: [...item.items, ...next.items] }
                : item,
            ) as Columns,
        );
      } catch (error) {
        if (!isCurrentNavigation()) return;
        if (isListingConflict(error)) {
          try {
            const refreshed = await loadDirectory(root, page.path, null, query);
            if (!isCurrentNavigation()) return;
            setColumns(
              (previous) =>
                previous.map((item, position) =>
                  position === index &&
                  item &&
                  item.root === page.root &&
                  item.path === page.path &&
                  item.next_cursor === cursor
                    ? refreshed
                    : item,
                ) as Columns,
            );
            setColumnError(index, null);
            message.success?.("目录已更新，已重新加载");
          } catch (refreshError) {
            if (!isCurrentNavigation()) return;
            setColumnError(index, requestError(refreshError));
          }
          return;
        }
        setColumnError(index, requestError(error));
      } finally {
        loadingPageKeys.current.delete(pageKey);
        if (isCurrentNavigation()) setColumnLoading(index, false);
      }
    },
    [
      columnQueries,
      columns,
      loadDirectory,
      loadingColumns,
      message,
      root,
      setColumnError,
      setColumnLoading,
    ],
  );

  const retryColumn = useCallback(
    async (index: 0 | 1 | 2) => {
      const page = columns[index];
      if (!page) {
        await loadInitial(root);
        return;
      }
      setColumnLoading(index, true);
      setColumnError(index, null);
      try {
        const refreshed = await loadDirectory(
          root,
          page.path,
          null,
          columnQueries[index] || undefined,
        );
        setColumns(
          (previous) =>
            previous.map((item, position) =>
              position === index ? refreshed : item,
            ) as Columns,
        );
      } catch (error) {
        setColumnError(index, requestError(error));
      } finally {
        setColumnLoading(index, false);
      }
    },
    [
      columnQueries,
      columns,
      loadDirectory,
      loadInitial,
      root,
      setColumnError,
      setColumnLoading,
    ],
  );

  const submitSearch = useCallback(async () => {
    const targetIndex = columns[1] ? 1 : 0;
    const target = columns[targetIndex];
    if (!target) return;
    setColumnLoading(targetIndex, true);
    try {
      const query = search.trim();
      const results = await loadDirectory(
        root,
        target.path,
        null,
        query || undefined,
      );
      setColumns(
        (previous) =>
          previous.map((item, index) =>
            index === targetIndex ? results : item,
          ) as Columns,
      );
      setColumnQueries(
        (previous) =>
          previous.map((value, index) =>
            index === targetIndex ? query : value,
          ) as ColumnQueries,
      );
    } catch (error) {
      setColumnError(targetIndex, requestError(error));
    } finally {
      setColumnLoading(targetIndex, false);
    }
  }, [columns, loadDirectory, root, search, setColumnError, setColumnLoading]);

  const switchRoot = useCallback(
    (nextRoot: FileManagerRoot) => {
      executeOrGuard(() => {
        if (nextRoot !== root) navigationVersionRef.current += 1;
        setRoot(nextRoot);
        setSearch("");
      });
    },
    [executeOrGuard, root],
  );

  const anchorPath = useCallback(
    async (path: string) => {
      if (!path) {
        await loadInitial(root);
        return;
      }
      setLoadingColumns([true, true, true]);
      setColumnErrors([null, null, null]);
      try {
        const middle = await loadDirectory(root, path);
        const firstChild = middle.items.find(
          (item) => item.kind === "directory",
        );
        const right = firstChild
          ? await loadDirectory(root, firstChild.path)
          : null;
        setColumns([
          directoryAnchor(root, path, middle.capabilities),
          middle,
          right,
        ]);
        setColumnQueries(["", "", ""]);
        setSearch("");
        setSelected([
          `${virtualAnchorPrefix}${path}`,
          firstChild?.path || null,
          null,
        ]);
        setDetail(null);
        setPreview(null);
        revokeBinaryPreview();
      } catch (error) {
        setColumnErrors([null, requestError(error), null]);
      } finally {
        setLoadingColumns([false, false, false]);
      }
    },
    [loadDirectory, loadInitial, revokeBinaryPreview, root],
  );

  const confirmDeleteDirectory = useCallback(
    (entry: FileManagerItem) => {
      executeOrGuard(() => {
        Modal.confirm({
          title: "永久删除目录？",
          content: (
            <>
              <strong>{entry.path}</strong>
              <br />
              目录及其全部内容将被永久删除，无法恢复。
            </>
          ),
          okText: "永久删除",
          okButtonProps: { danger: true },
          cancelText: "取消",
          onOk: async () => {
            try {
              await fileManagerApi.deleteDirectory({
                root,
                path: entry.path,
              });
              await anchorPath(parentPath(entry.path));
              message.success("目录已永久删除");
            } catch (error) {
              message.error(`删除失败：${requestError(error)}`);
              throw error;
            }
          },
        });
      });
    },
    [anchorPath, executeOrGuard, fileManagerApi, message, root],
  );

  const saveText = useCallback(
    async (content: string, revision: string) => {
      if (!detail) return;
      try {
        const saved = await fileManagerApi.saveText({
          root,
          path: detail.path,
          content,
          revision,
        });
        setPreview(saved);
        message.success("已保存修改");
        return saved;
      } catch (error) {
        message.error("保存失败：内容已保留，可处理冲突后重试。");
        throw error;
      }
    },
    [detail, fileManagerApi, message, root],
  );

  const refreshCurrentDirectory = useCallback(async () => {
    const targetIndex = columns[1] ? 1 : 0;
    const target = columns[targetIndex];
    if (!target) return;
    const refreshed = await loadDirectory(
      root,
      target.path,
      null,
      columnQueries[targetIndex] || undefined,
    );
    setColumns(
      (previous) =>
        previous.map((page, index) =>
          index === targetIndex ? refreshed : page,
        ) as Columns,
    );
  }, [columnQueries, columns, loadDirectory, root]);

  const upload = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file || !currentDirectory) return;
    try {
      await fileManagerApi.upload(
        { root, path: currentDirectory.path },
        file,
      );
      await refreshCurrentDirectory();
      message.success("上传完成");
    } catch (error) {
      message.error(`上传失败：${requestError(error)}`);
    } finally {
      event.target.value = "";
    }
  };

  const doDownload = useCallback(async () => {
    if (!detail) return;
    try {
      const { blob, filename } = await fileManagerApi.downloadFile({
        root,
        path: detail.path,
      });
      const href = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = href;
      anchor.download = filename || detail.name;
      anchor.click();
      URL.revokeObjectURL(href);
    } catch (error) {
      message.error(`下载失败：${requestError(error)}`);
    }
  }, [detail, fileManagerApi, message, root]);

  const mutate = useCallback(
    (kind: "archive" | "restore" | "purge") => {
      if (!detail) return;
      const labels = {
        archive: "将文件移到回收站",
        restore: "还原此文件",
        purge: "永久删除此文件",
      };
      const performMutation = async () => {
        if (kind === "archive")
          await fileManagerApi.archive({ root, path: detail.path });
        if (kind === "restore" && detail.archive_item_id)
          await fileManagerApi.restore(detail.archive_item_id);
        if (kind === "purge" && detail.archive_item_id)
          await fileManagerApi.purge(detail.archive_item_id);
        setDetail(null);
        setPreview(null);
        await refreshCurrentDirectory();
        message.success("操作完成");
      };
      Modal.confirm({
        title: labels[kind],
        content:
          kind === "purge"
            ? "永久删除后无法恢复。下一步会再次确认原始路径。"
            : "此操作会立即刷新当前目录。",
        okText: kind === "purge" ? "继续" : "确认",
        okButtonProps: kind === "purge" ? { danger: true } : undefined,
        cancelText: "取消",
        onOk: async () => {
          if (kind !== "purge") return performMutation();
          Modal.confirm({
            title: "确认永久删除",
            content: (
              <>
                此文件的原始路径为：
                <strong>{detail.original_path || detail.path}</strong>
                。永久删除后无法恢复。
              </>
            ),
            okText: "永久删除",
            okButtonProps: { danger: true },
            cancelText: "取消",
            onOk: performMutation,
          });
        },
      });
    },
    [detail, fileManagerApi, message, refreshCurrentDirectory, root],
  );

  const breadcrumbPath = detail
    ? detail.path.split("/").slice(0, -1).join("/")
    : currentDirectory?.path || "";
  const canUpload =
    !uploadReason && Boolean(currentDirectory?.capabilities.upload);

  return (
    <>
      <Tooltip title="文件管理器">
        <Button
          type="text"
          icon={<FolderOpenOutlined />}
          aria-label="文件管理器"
          onClick={() => setOpen(true)}
        />
      </Tooltip>
      <Drawer
        open={open}
        width={
          fullscreen
            ? "100vw"
            : "var(--copaw-file-manager-drawer-width, clamp(760px, 58vw, 920px))"
        }
        placement="right"
        mask={false}
        push={false}
        closable={false}
        rootClassName={styles.drawerRoot}
        onClose={closeFileManager}
        styles={{
          body: {
            display: "flex",
            minHeight: 0,
            padding: 0,
          },
        }}
        aria-label="文件管理器"
      >
        <div className={styles.manager}>
          <Tabs
            className={styles.workspaceTabs}
            activeKey={activeTab}
            onChange={(key) => {
              const nextTab = key as WorkspaceTab;
              setActiveTab(nextTab);
              if (nextTab === "session") {
                setSessionListCollapsed(false);
              }
            }}
            tabBarExtraContent={
              <div className={styles.drawerActions}>
                <Tooltip title={fullscreen ? "退出全屏" : "全屏查看"}>
                  <Button
                    type="text"
                    className={styles.fullscreenButton}
                    aria-label={fullscreen ? "退出全屏" : "全屏查看文件管理器"}
                    icon={
                      fullscreen ? (
                        <FullscreenExitOutlined />
                      ) : (
                        <FullscreenOutlined />
                      )
                    }
                    onClick={() => setFullscreen((current) => !current)}
                  />
                </Tooltip>
                {activeTab === "session" && sessionFiles.length > 0 && (
                  <Tooltip
                    title={
                      sessionListCollapsed
                        ? "展开会话文件列表"
                        : "收起会话文件列表"
                    }
                  >
                    <Button
                      type="text"
                      className={styles.sessionListToggle}
                      aria-label={
                        sessionListCollapsed
                          ? "展开会话文件列表"
                          : "收起会话文件列表"
                      }
                      icon={
                        sessionListCollapsed ? (
                          <MenuUnfoldOutlined />
                        ) : (
                          <MenuFoldOutlined />
                        )
                      }
                      onClick={() =>
                        setSessionListCollapsed((collapsed) => !collapsed)
                      }
                    />
                  </Tooltip>
                )}
                <Button
                  type="text"
                  className={styles.closeButton}
                  aria-label="关闭文件管理器"
                  icon={<CloseOutlined />}
                  onClick={closeFileManager}
                />
              </div>
            }
            items={[
              {
                key: "workspace",
                label: (
                  <span className={styles.tabLabel}>
                    <FolderOpenOutlined aria-hidden="true" />
                    <span>工作区文件</span>
                  </span>
                ),
              },
              {
                key: "session",
                label: (
                  <span
                    className={styles.tabLabel}
                    aria-label={`当前会话文件${sessionFiles.length > 0 ? ` ${sessionFiles.length}` : ""}`}
                  >
                    <MessageOutlined aria-hidden="true" />
                    <span>当前会话文件</span>
                    {sessionFiles.length > 0 && (
                      <span className={styles.tabCount}>
                        {sessionFiles.length}
                      </span>
                    )}
                  </span>
                ),
              },
            ]}
          />
          {activeTab === "workspace" ? (
            <>
              <header className={styles.topBar}>
                <Breadcrumb
                  className={styles.breadcrumb}
                  items={breadcrumbItems(root, breadcrumbPath, (path) =>
                    executeOrGuard(() => {
                      void anchorPath(path);
                    }),
                  )}
                />
                <div className={styles.headerActions}>
                  <Input.Search
                    allowClear
                    placeholder={`在 ${
                      currentDirectory?.path || "目录"
                    } 中搜索`}
                    value={search}
                    onChange={(event) => setSearch(event.target.value)}
                    onSearch={() =>
                      executeOrGuard(() => {
                        void submitSearch();
                      })
                    }
                    prefix={<SearchOutlined />}
                    aria-label="搜索当前目录"
                  />
                  <Tooltip title={uploadReason || "上传到中间栏目当前目录"}>
                    <span>
                      <Button
                        type="primary"
                        icon={<UploadOutlined />}
                        disabled={!canUpload}
                        onClick={() => fileInputRef.current?.click()}
                      >
                        上传
                      </Button>
                    </span>
                  </Tooltip>
                </div>
                <input
                  ref={fileInputRef}
                  className={styles.fileInput}
                  type="file"
                  onChange={(event) => void upload(event)}
                />
              </header>
              {uploadReason && (
                <div className={styles.rootNotice}>{uploadReason}</div>
              )}
              <nav className={styles.shortcutBar} aria-label="文件目录快捷方式">
                {shortcutRoots.map((shortcut) => (
                  <Button
                    key={shortcut.root}
                    className={styles.shortcut}
                    icon={shortcut.icon}
                    aria-label={shortcut.label}
                    aria-pressed={root === shortcut.root}
                    onClick={() => switchRoot(shortcut.root)}
                  >
                    {shortcut.label}
                  </Button>
                ))}
              </nav>
              <main className={styles.columns}>
                {[0, 1].map((index) => (
                  <FileColumn
                    key={index}
                    column={(index + 1) as 1 | 2}
                    directory={columns[index as 0 | 1]}
                    selectedPath={selected[index as 0 | 1]}
                    loading={loadingColumns[index]}
                    error={columnErrors[index]}
                    onRetry={() => void retryColumn(index as 0 | 1)}
                    onSelect={(entry) =>
                      void selectEntry(index as 0 | 1, entry)
                    }
                    onDeleteDirectory={
                      canDeleteDirectories ? confirmDeleteDirectory : undefined
                    }
                    onLoadMore={() => void loadMore(index as 0 | 1)}
                  />
                ))}
                <div className={styles.detailColumn}>
                  {columns[2] && !detail ? (
                    <FileColumn
                      column={3}
                      directory={columns[2]}
                      selectedPath={selected[2]}
                      loading={loadingColumns[2]}
                      error={columnErrors[2]}
                      onRetry={() => void retryColumn(2)}
                      onSelect={(entry) => void selectEntry(2, entry)}
                      onDeleteDirectory={
                        canDeleteDirectories
                          ? confirmDeleteDirectory
                          : undefined
                      }
                      onLoadMore={() => void loadMore(2)}
                    />
                  ) : (
                    <FileDetail
                      ref={detailRef}
                      entry={detail}
                      preview={preview}
                      binaryPreviewUrl={binaryPreviewUrl}
                      loading={detailLoading}
                      error={detailError}
                      editable={root !== "conversation" && root !== "recycle"}
                      onDownload={() => void doDownload()}
                      onEditStateChange={setDirty}
                      onSave={saveText}
                      onArchive={() => mutate("archive")}
                      onRestore={() => mutate("restore")}
                      onPurge={() => mutate("purge")}
                    />
                  )}
                </div>
              </main>
            </>
          ) : (
            <main
              className={`${styles.sessionContent} ${
                sessionListCollapsed ? styles.sessionContentCollapsed : ""
              }`}
            >
              {!sessionListCollapsed && (
                <section
                  className={styles.sessionFileList}
                  aria-label="当前会话文件"
                >
                  {sessionFiles.length === 0 ? (
                    <div className={styles.sessionEmpty}>
                      <Empty description="当前会话还没有可预览的文件" />
                    </div>
                  ) : (
                    sessionFiles.map((file) => (
                      <button
                        key={file.fileUrl}
                        type="button"
                        className={styles.sessionFile}
                        aria-pressed={sessionPreview?.fileUrl === file.fileUrl}
                        onClick={() => {
                          setSessionPreview(file);
                        }}
                      >
                        <span className={styles.sessionFileName}>
                          {file.fileName}
                        </span>
                        <span className={styles.sessionFileHint}>点击预览</span>
                      </button>
                    ))
                  )}
                </section>
              )}
              <section
                className={styles.sessionPreview}
                aria-label="会话文件预览"
              >
                {sessionPreview ? (
                  <FilePreviewModal
                    key={sessionPreview.fileUrl}
                    open
                    onClose={() => {
                      setSessionPreview(null);
                    }}
                    fileUrl={sessionPreview.fileUrl}
                    fileName={sessionPreview.fileName}
                    enableClickTracking={sessionPreview.enableClickTracking}
                    presentation="workspace"
                    nestedPreviewMode="replace"
                  />
                ) : (
                  <Empty description="选择一个会话文件以预览" />
                )}
              </section>
            </main>
          )}
        </div>
      </Drawer>
      <Modal
        open={guardOpen}
        title="保存未完成的修改？"
        okText="保存"
        cancelText="取消"
        onCancel={() => {
          setGuardOpen(false);
          setPendingAction(null);
        }}
        onOk={async () => {
          const saved = await detailRef.current?.saveDraft();
          if (saved) {
            setDirty(false);
            setGuardOpen(false);
            const action = pendingAction;
            setPendingAction(null);
            action?.();
          }
        }}
        footer={(_, { OkBtn, CancelBtn }) => (
          <>
            <Button
              onClick={() => {
                setDirty(false);
                setGuardOpen(false);
                const action = pendingAction;
                setPendingAction(null);
                action?.();
              }}
            >
              放弃修改
            </Button>
            <CancelBtn />
            <OkBtn />
          </>
        )}
      >
        当前文件有未保存的修改。可返回编辑器保存，或放弃修改后继续。
      </Modal>
    </>
  );
}
