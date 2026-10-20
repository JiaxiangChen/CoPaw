import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Input, Select, Space, Tag, Typography } from "antd";
import { Button, Modal, Switch, Table } from "@agentscope-ai/design";
import {
  schedulerCronApi,
  targetManagedContext,
} from "@/api/modules/schedulerCron";
import type {
  ManagedChild,
  ManagedCronContext,
  ManagedJob,
  ManagedSelectedChild,
} from "@/api/types/schedulerCron";
import {
  detailToJob,
  failureMessage,
  isBatchDispatchMode,
  RequestIdentity,
  selectedChildren,
} from "./helpers";
import { useManagedPanel } from "./useManagedPanel";
import {
  childBbkOptions,
  duplicateChildNames,
  filterChildren,
} from "./childListView";
import {
  createChildrenDeletion,
  deletionInProgress,
  runChildrenDeletion,
  type ChildrenDeletionPlan,
} from "./childrenDeletion";
import styles from "../CronJobs/components/BroadcastChildrenModal.module.less";

const idle = () => false;
const EMPTY_CHILDREN: ManagedChild[] = [];
const rowKey = (row: ManagedChild) =>
  JSON.stringify([row.tenant_id, row.source_id, row.agent_id, row.id]);
interface Props {
  context: ManagedCronContext;
  job: ManagedJob;
  onClose: () => void;
  onEdit: (child: ManagedJob) => void;
  onProgress?: (child: ManagedJob) => void;
  onChanged: () => void;
}

/** 批操作经父任务授权入口逐项处理，跨用户读取仍保留真实操作者。 */
export function ManagedChildrenModal({
  context,
  job,
  onClose,
  onEdit,
  onProgress,
  onChanged,
}: Props) {
  const [includeDeleted, setIncludeDeleted] = useState(false);
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [branch, setBranch] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(8);
  const [selected, setSelected] = useState<React.Key[]>([]);
  const [busy, setBusy] = useState(false);
  const [runResult, setRunResult] = useState<string | null>(null);
  const deletion = useRef<ChildrenDeletionPlan | null>(null);
  const writeLock = useRef(false);
  const confirmation = useRef<ReturnType<typeof Modal.confirm> | null>(null);
  useEffect(() => () => confirmation.current?.destroy(), []);
  const runRequest = useRef(new RequestIdentity());
  const controlRequests = useRef(new Map<string, RequestIdentity>());
  const load = useCallback(
    () => schedulerCronApi.childrenAll(context, job.id, includeDeleted),
    [context, job.id, includeDeleted],
  );
  const panel = useManagedPanel(load, idle);
  const ignoredDeletion = useRef<string | null>(null);
  const loadDeletion = useCallback(async () => {
    const value = await schedulerCronApi.currentChildrenDeletion(
      context,
      job.id,
    );
    return {
      task:
        value.task?.operation_id === ignoredDeletion.current
          ? null
          : value.task ?? null,
    };
  }, [context, job.id]);
  const deletionPanel = useManagedPanel(loadDeletion, (value) =>
    Boolean(
      value.task &&
        value.task.operation_status !== "failed" &&
        (deletionInProgress(value.task) || value.task.pending_count > 0),
    ),
  );
  const result = deletionPanel.value?.task ?? null;
  const deleteLocked = Boolean(deletion.current || result);
  const deleting = Boolean(result && deletionInProgress(result));
  const deletedCount = result?.deleted_count ?? 0;
  const deletionProgressKey = `${result?.operation_id ?? ""}:${deletedCount}`;
  const notifiedProgress = useRef("");
  const reloadChildren = panel.reload;
  useEffect(() => {
    if (deletedCount > 0 && notifiedProgress.current !== deletionProgressKey) {
      notifiedProgress.current = deletionProgressKey;
      void reloadChildren();
      onChanged();
    }
  }, [deletedCount, deletionProgressKey, reloadChildren, onChanged]);
  const children = panel.value ?? EMPTY_CHILDREN;
  const filtered = useMemo(
    () => filterChildren(children, search, branch),
    [children, search, branch],
  );
  const branches = useMemo(() => childBbkOptions(children), [children]);
  const duplicateNames = useMemo(
    () => duplicateChildNames(children),
    [children],
  );
  const chosen = children.filter((child) => selected.includes(rowKey(child)));
  const toggle = async (child: ManagedChild) => {
    if (!panel.alive.current || busy || !child.management_version) return;
    setBusy(true);
    const key = rowKey(child);
    const identity = controlRequests.current.get(key) ?? new RequestIdentity();
    controlRequests.current.set(key, identity);
    try {
      const body = {
        version: child.management_version,
        enabled: Boolean(child.task?.is_paused) || !child.enabled,
      };
      const value = await schedulerCronApi.setEnabled(
        targetManagedContext(context, child),
        child.id,
        body.enabled,
        body.version,
        identity.for(body),
      );
      identity.reset();
      if (panel.alive.current) {
        setRunResult(
          `操作 ${value.operation.operation_id}：${value.operation.status}`,
        );
        await panel.reload();
        onChanged();
      }
    } catch (failure) {
      if (panel.alive.current) panel.setError(failureMessage(failure));
    } finally {
      if (panel.alive.current) setBusy(false);
    }
  };
  const act = async (remove: boolean, fixed?: ManagedSelectedChild[]) => {
    if (!panel.alive.current || busy || writeLock.current) return;
    writeLock.current = true;
    setBusy(true);
    try {
      const items = fixed ?? selectedChildren(chosen);
      if (remove) {
        if (result?.operation_id) {
          const value = await schedulerCronApi.retryChildrenDeletion(
            context,
            job.id,
            result.operation_id,
          );
          if (panel.alive.current) deletionPanel.replaceValue({ task: value });
        } else {
          deletion.current ??= createChildrenDeletion(items, Boolean(fixed));
          await runChildrenDeletion(
            context,
            job.id,
            deletion.current,
            (value) => {
              if (panel.alive.current)
                deletionPanel.replaceValue({ task: value });
            },
            () => panel.alive.current,
          );
        }
      } else {
        const value = await schedulerCronApi.childrenRun(
          context,
          job.id,
          items,
          runRequest.current.for(items),
        );
        if (panel.alive.current)
          setRunResult(
            `已处理 ${value.results.length} 项，失败 ${
              value.failed_count
            } 项；${value.results
              .map(
                (item) =>
                  item.error_code || item.execution?.status || item.status,
              )
              .join("、")}`,
          );
        runRequest.current.reset();
      }
      if (panel.alive.current) {
        await panel.reload();
        onChanged();
      }
    } catch (failure) {
      if (panel.alive.current) panel.setError(failureMessage(failure));
    } finally {
      writeLock.current = false;
      if (panel.alive.current) setBusy(false);
    }
  };
  const confirmDeleteAll = async () => {
    if (!panel.alive.current || busy || writeLock.current || deleteLocked)
      return;
    writeLock.current = true;
    setBusy(true);
    try {
      const all = await schedulerCronApi.childrenAll(context, job.id, false);
      if (!panel.alive.current) return;
      const items = selectedChildren(all.filter((child) => !child.deleted));
      if (!items.length) throw new Error("没有可删除的子任务，请刷新列表");
      confirmation.current = Modal.confirm({
        title: `删除全部 ${items.length} 个子任务？`,
        content: `将删除父任务「${job.name}」下本次读取的全部未删除子任务，不受搜索、机构筛选或分页影响。不删除父任务；未开始的执行将跳过，正在执行的任务不取消。`,
        okText: "确认删除全部",
        cancelText: "取消",
        okButtonProps: { danger: true },
        onOk: () => act(true, items),
      });
    } catch (failure) {
      if (panel.alive.current) panel.setError(failureMessage(failure));
    } finally {
      writeLock.current = false;
      if (panel.alive.current) setBusy(false);
    }
  };
  const inspect = async (child: ManagedChild, progress = false) => {
    if (!panel.alive.current || busy) return;
    setBusy(true);
    try {
      const detail = await schedulerCronApi.detail(
        targetManagedContext(context, child),
        child.id,
        progress,
      );
      if (panel.alive.current) {
        if (progress) onProgress?.(detailToJob(detail));
        else onEdit(detailToJob(detail));
      }
    } catch (failure) {
      if (panel.alive.current) panel.setError(failureMessage(failure));
    } finally {
      if (panel.alive.current) setBusy(false);
    }
  };
  return (
    <Modal
      open
      title={`子任务管理 · ${job.name}`}
      width={1280}
      style={{ maxWidth: "calc(100vw - 48px)" }}
      onCancel={onClose}
      footer={null}
    >
      <Space
        direction="vertical"
        style={{ width: "100%" }}
        className={styles.content}
      >
        <Alert
          type="info"
          message="删除异步受理，关闭页面后继续处理；已开始的执行继续，失败清理可在单项进度中重试。"
        />
        {panel.error && <Alert type="error" showIcon message={panel.error} />}
        {deletionPanel.error && (
          <Alert type="error" showIcon message={deletionPanel.error} />
        )}
        {duplicateNames.length > 0 && (
          <Alert
            type="warning"
            showIcon
            message="存在同名用户，请以 UID 区分"
            description={duplicateNames.join("、")}
          />
        )}
        <Space wrap>
          <Switch
            checked={includeDeleted}
            disabled={busy || deleteLocked}
            onChange={(value) => {
              setIncludeDeleted(value);
              setSelected([]);
              setPage(1);
            }}
          />
          <span>包含已删除</span>
          <Button
            onClick={() => {
              void panel.reload();
              void deletionPanel.reload();
            }}
            loading={panel.loading || deletionPanel.loading}
          >
            刷新
          </Button>
          <Button
            disabled={
              busy || !chosen.length || chosen.length > 1000 || deleteLocked
            }
            onClick={() => void act(false)}
          >
            执行选中任务
          </Button>
          <Button
            danger
            disabled={
              busy ||
              deleting ||
              deletionPanel.loading ||
              Boolean(deletionPanel.error) ||
              Boolean(result && result.operation_status !== "failed") ||
              chosen.length > 1000 ||
              (!chosen.length && !deleteLocked)
            }
            onClick={() =>
              Modal.confirm({
                title: deleteLocked
                  ? "继续原批量删除？"
                  : `删除选中的 ${chosen.length} 个任务？`,
                content: "未开始的执行将跳过，正在执行的任务不取消。",
                onOk: () => act(true),
              })
            }
          >
            {deleteLocked ? "重试原批量删除" : "删除选中任务"}
          </Button>
          <Button
            danger
            disabled={
              busy ||
              panel.loading ||
              Boolean(panel.error) ||
              deletionPanel.loading ||
              Boolean(deletionPanel.error) ||
              deleteLocked ||
              !children.some((child) => !child.deleted)
            }
            onClick={() => void confirmDeleteAll()}
          >
            删除全部子任务
          </Button>
          {result?.operation_status === "completed" &&
            result.pending_count === 0 && (
              <Button
                onClick={() => {
                  deletion.current = null;
                  ignoredDeletion.current = result.operation_id;
                  deletionPanel.replaceValue({ task: null });
                  setSelected([]);
                }}
              >
                结束本次操作
              </Button>
            )}
        </Space>
        <Space wrap>
          <Input.Search
            aria-label="搜索子任务用户"
            allowClear
            enterButton="搜索"
            placeholder="搜索用户姓名或 UID"
            style={{ width: 320, maxWidth: "100%" }}
            value={searchInput}
            disabled={busy || deleteLocked}
            onChange={(event) => setSearchInput(event.target.value)}
            onSearch={(value) => {
              setSearch(value);
              setSelected([]);
              setPage(1);
            }}
          />
          <Select
            aria-label="筛选子任务机构"
            allowClear
            showSearch
            optionFilterProp="label"
            placeholder="筛选机构"
            options={branches}
            value={branch || undefined}
            style={{ width: 180, maxWidth: "100%" }}
            disabled={busy || deleteLocked}
            onChange={(value) => {
              setBranch(value || "");
              setSelected([]);
              setPage(1);
            }}
          />
        </Space>
        {chosen.length > 1000 && (
          <Alert type="warning" message="一次批量操作最多选择 1000 个任务。" />
        )}
        {runResult && <Alert type="info" message={runResult} />}
        {result && (
          <>
            <Alert
              type={
                result.operation_status === "failed" || result.failed_count > 0
                  ? "warning"
                  : "info"
              }
              showIcon
              message={`异步删除 ${result.operation_id}：${
                deleting
                  ? "后台排队或处理中，关闭页面不影响执行"
                  : result.operation_status === "failed"
                  ? "处理中断，请重试原批量删除"
                  : result.pending_count > 0
                  ? "删除已处理，等待在途执行结束后清理"
                  : "处理结束"
              }`}
            />
            <Typography.Text>
              删除 {result.deleted_count} / 完成 {result.completed_count} /
              待处理 {result.pending_count} / 失败 {result.failed_count}
            </Typography.Text>
            {result.total_count > result.results.length && (
              <Typography.Text type="secondary">
                尚有 {result.total_count - result.results.length}{" "}
                项未返回结果，按待确认处理。
              </Typography.Text>
            )}
            <Table
              size="small"
              rowKey={(item) =>
                `${item.tenant_id}:${item.agent_id}:${item.job_id}`
              }
              dataSource={result.results}
              pagination={{ pageSize: 10 }}
              columns={[
                { title: "用户", dataIndex: "tenant_id" },
                { title: "任务", dataIndex: "job_id" },
                { title: "状态", dataIndex: "status" },
                {
                  title: "已逻辑删除",
                  render: (_, item) =>
                    item.logical_delete_applied ? "是" : "否",
                },
                { title: "操作 ID", dataIndex: "operation_id" },
                { title: "错误", dataIndex: "error_code" },
              ]}
            />
          </>
        )}
        <Table
          size="small"
          rowKey={rowKey}
          loading={panel.loading}
          dataSource={filtered}
          scroll={{ x: 1700, y: "calc(100vh - 380px)" }}
          pagination={{
            current: page,
            pageSize,
            showSizeChanger: true,
            pageSizeOptions: ["8", "20", "50", "100"],
            showTotal: (total) => `共 ${total} 条`,
            onChange: (nextPage, nextSize) => {
              setPage(nextPage);
              setPageSize(nextSize);
            },
          }}
          rowSelection={{
            selectedRowKeys: selected,
            onChange: setSelected,
            getCheckboxProps: (row) => ({
              disabled: row.deleted || busy || Boolean(deletion.current),
            }),
          }}
          columns={[
            {
              title: "用户",
              width: 220,
              render: (_, row) =>
                row.tenant_name ? (
                  <Space direction="vertical" size={0}>
                    <Typography.Text strong>{row.tenant_name}</Typography.Text>
                    <Typography.Text type="secondary" code>
                      {row.tenant_id}
                    </Typography.Text>
                  </Space>
                ) : (
                  <Typography.Text>{row.tenant_id}</Typography.Text>
                ),
            },
            {
              title: "机构",
              width: 120,
              render: (_, row) => row.bbk_id?.trim() || "-",
            },
            { title: "Agent", dataIndex: "agent_id", width: 100 },
            { title: "任务", dataIndex: "name", width: 180 },
            { title: "子任务ID", dataIndex: "id", width: 220 },
            {
              title: "状态",
              render: (_, row) =>
                row.deleted
                  ? "已删除"
                  : row.task?.is_running
                  ? "运行中"
                  : row.enabled && !row.task?.is_paused
                  ? "已启用"
                  : "已停用",
            },
            {
              title: "排程",
              width: 160,
              render: (_, row) => (
                <span>
                  {isBatchDispatchMode(row.dispatch_state) && (
                    <Tag color="processing">批调度</Tag>
                  )}
                  <span>{row.schedule?.cron}</span>
                </span>
              ),
            },
            {
              title: "时区",
              width: 140,
              render: (_, row) => row.schedule?.timezone || "-",
            },
            {
              title: "提前分钟",
              width: 100,
              render: (_, row) => row.offset_minutes ?? 0,
            },
            {
              title: "最近状态",
              width: 120,
              render: (_, row) => row.state?.last_status || "-",
            },
            {
              title: "操作",
              width: 240,
              render: (_, row) => (
                <Space>
                  <Button
                    type="link"
                    disabled={row.deleted || busy || !row.management_version}
                    aria-label={`${
                      row.enabled && !row.task?.is_paused ? "停用" : "启用"
                    } ${row.tenant_id} ${row.name}`}
                    onClick={() => void toggle(row)}
                  >
                    {row.enabled && !row.task?.is_paused ? "停用" : "启用"}
                  </Button>
                  <Button
                    type="link"
                    disabled={row.deleted || busy}
                    onClick={() => void inspect(row)}
                  >
                    编辑
                  </Button>
                  {onProgress && (
                    <Button
                      type="link"
                      disabled={busy}
                      onClick={() => void inspect(row, true)}
                    >
                      进度
                    </Button>
                  )}
                </Space>
              ),
            },
          ]}
        />
      </Space>
    </Modal>
  );
}
