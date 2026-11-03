import type { ReactNode } from "react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  EditOutlined,
  HistoryOutlined,
  MoreOutlined,
  UserOutlined,
  DownloadOutlined,
} from "@ant-design/icons";
import { Button, Dropdown, message, Modal, Spin, Table, Tag, Tooltip, Typography, type MenuProps } from "antd";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Send, Undo2, Trash2, Archive, Users, PhoneCall, Tag as TagIcon, GitBranch, Calendar, CheckCircle, BarChart3 } from "lucide-react";
import { marketApi, MarketSkillDetail } from "../../api/modules/market";
import type { FileContentResponse } from "../../api/modules/mySkills";
import { VersionHistoryModal } from "./Skills/VersionHistoryModal";
import styles from "./SkillDetailDrawer.module.less";

const { Text, Title } = Typography;
const DEFAULT_USAGE_PAGE_SIZE = 5;

interface SkillDetailDrawerProps {
  open: boolean;
  skill: MarketSkillDetail | null;
  onClose: () => void;
  isManager?: boolean;
  onDistribute?: () => void;
  onLookupOwners?: () => void;
  onRecall?: () => void;
  onUnpublish?: () => void;
  onDelete?: () => void;
  sourceId?: string;
  onRefresh?: () => void;
  categoryName?: string;
  onEdit?: () => void;
}

const FRONTMATTER_PATTERN = /^---\r?\n[\s\S]*?\r?\n---[ \t]*(?:\r?\n|$)/;

// 顶栏样式 - 固定在顶部
const HEADER_STYLE = {
  position: "sticky",
  top: 0,
  zIndex: 10,
  padding: "12px 20px",
  backgroundColor: "#fff",
  borderBottom: "1px solid #f0f0f0",
} as const;

// 元数据项样式 - 淡色小字
const META_ITEM_STYLE = {
  display: "inline-flex",
  alignItems: "center",
  gap: 4,
  fontSize: 12,
  color: "#8c8c8c",
} as const;

// 中文名样式 - 稍大
const CHINESE_NAME_STYLE = {
  fontSize: 14,
  fontWeight: 500,
  color: "#1a1a1a",
} as const;

// 技能名样式 - 小号
const SKILL_NAME_STYLE = {
  fontSize: 12,
  color: "#8c8c8c",
} as const;

// 下载按钮样式 - 深灰蓝（柔和色调）
const DOWNLOAD_BUTTON_STYLE = {
  height: 32,
  padding: "0 12px",
  borderRadius: 6,
  fontSize: 13,
  fontWeight: 500,
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  border: "1px solid #8b9caa",
  backgroundColor: "#f4f6f8",
  color: "#5b6b7c",
} as const;

// 版本历史按钮样式 - 深灰紫（柔和色调）
const HISTORY_BUTTON_STYLE = {
  height: 32,
  padding: "0 12px",
  borderRadius: 6,
  fontSize: 13,
  fontWeight: 500,
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  border: "1px solid #9b8baa",
  backgroundColor: "#f6f4f8",
  color: "#6b5b7a",
} as const;

// 主要按钮样式（分发）- 蓝色实心（保持突出）
const PRIMARY_BUTTON_STYLE = {
  height: 32,
  padding: "0 14px",
  borderRadius: 6,
  fontSize: 13,
  fontWeight: 600,
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  backgroundColor: "#1890ff",
  color: "#fff",
  border: "none",
} as const;

// 用户查询按钮样式 - 深灰绿（柔和色调）
const USER_BUTTON_STYLE = {
  height: 32,
  padding: "0 12px",
  borderRadius: 6,
  fontSize: 13,
  fontWeight: 500,
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  border: "1px solid #8a9b8a",
  backgroundColor: "#f4f8f5",
  color: "#4a7c59",
} as const;

// 更多按钮样式（下拉）- 灰色图标
const MORE_BUTTON_STYLE = {
  height: 32,
  width: 32,
  padding: 0,
  borderRadius: 6,
  fontSize: 14,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  border: "1px solid #d9d9d9",
  backgroundColor: "#fff",
  color: "#8c8c8c",
} as const;

// 统计徽章样式
const STAT_TAG_STYLE = {
  margin: 0,
  borderRadius: 6,
  paddingInline: 8,
  paddingBlock: 2,
  fontSize: 12,
  display: "inline-flex",
  alignItems: "center",
  gap: 4,
} as const;

function formatDate(value: string | null): string {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleDateString("zh-CN");
}

function formatMetricValue(value: number | null): string {
  if (value === null) return "0";
  if (value >= 100000000) return `${(value / 100000000).toFixed(1)}亿`;
  if (value >= 10000) return `${(value / 10000).toFixed(1)}万`;
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
  return String(value);
}

function splitMarkdownFrontmatter(
  fileType: string | null,
  fileContent: string | null,
): string | null {
  if (fileType !== "markdown" || typeof fileContent !== "string") {
    return fileContent;
  }
  const match = fileContent.match(FRONTMATTER_PATTERN);
  if (!match) {
    return fileContent;
  }
  return fileContent.slice(match[0].length).trim();
}

function renderPreviewContent(
  fileType: string | null,
  fileContent: string | null,
  fallbackDescription: string | null = null,
): ReactNode {
  // 加载失败时显示 fallback description
  if (fileContent === null && fallbackDescription) {
    return (
      <div className={styles.streamingMarkdown}>
        <Text style={{ fontSize: 14, color: "#1a1a1a", lineHeight: 1.7 }}>
          {fallbackDescription}
        </Text>
      </div>
    );
  }

  if (fileContent === null) {
    return (
      <Text type="secondary" style={{ fontSize: 13 }}>
        暂无文档内容
      </Text>
    );
  }

  if (fileType === "binary") {
    return (
      <div
        style={{
          width: "100%",
          boxSizing: "border-box",
          border: "1px dashed #d9d9d9",
          borderRadius: 8,
          padding: 24,
          backgroundColor: "#fafafa",
          textAlign: "center",
        }}
      >
        <Text type="secondary">该文件为二进制内容，当前仅支持只读占位预览。</Text>
      </div>
    );
  }

  if (fileType === "markdown") {
    const previewContent = splitMarkdownFrontmatter(fileType, fileContent) ?? "";
    return (
      <div
        style={{
          width: "100%",
          maxWidth: "100%",
          boxSizing: "border-box",
        }}
      >
        <div
          className={styles.streamingMarkdown}
          data-testid="skill-markdown-preview"
        >
          <ReactMarkdown remarkPlugins={[remarkGfm]}>
            {previewContent}
          </ReactMarkdown>
        </div>
      </div>
    );
  }

  if (fileType === "json") {
    let formatted = fileContent;
    try {
      formatted = JSON.stringify(JSON.parse(fileContent), null, 2);
    } catch {
      // 解析失败时回退原始内容，避免预览中断
    }
    return (
      <pre
        style={{
          margin: 0,
          width: "100%",
          boxSizing: "border-box",
          backgroundColor: "#1f1f1f",
          color: "#f5f5f5",
          borderRadius: 8,
          padding: 16,
          overflow: "auto",
          fontSize: 13,
          lineHeight: 1.6,
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          overflowWrap: "anywhere",
        }}
      >
        {formatted}
      </pre>
    );
  }

  return (
    <pre
      style={{
        margin: 0,
        width: "100%",
        boxSizing: "border-box",
        backgroundColor: "#fafafa",
        borderRadius: 8,
        padding: 16,
        border: "1px solid #f0f0f0",
        overflow: "auto",
        fontSize: 13,
        lineHeight: 1.6,
        whiteSpace: "pre-wrap",
        wordBreak: "break-word",
        overflowWrap: "anywhere",
      }}
    >
      {fileContent}
    </pre>
  );
}

export function SkillDetailDrawer(
  props: SkillDetailDrawerProps,
) {
  const {
    open,
    skill,
    isManager,
    onDistribute,
    onLookupOwners,
    onRecall,
    onUnpublish,
    onDelete,
    sourceId,
    categoryName,
    onRefresh,
    onEdit,
  } = props;
  const [fileDetail, setFileDetail] = useState<FileContentResponse | null>(null);
  const [fileLoading, setFileLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [versionHistoryOpen, setVersionHistoryOpen] = useState(false);
  const [downloadingCurrentVersion, setDownloadingCurrentVersion] = useState(false);
  const normalizedCategoryName = categoryName?.trim();

  const [usagePage, setUsagePage] = useState(1);
  const [usagePageSize, setUsagePageSize] = useState(DEFAULT_USAGE_PAGE_SIZE);

  // 统计配置相关状态
  const [includeInStatistics, setIncludeInStatistics] = useState<boolean>(false);
  const [, setIsUpdatingStatistics] = useState(false);

  const triggerBrowserDownload = useCallback((blob: Blob, filename: string) => {
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  }, []);

  const handleDownloadCurrentVersion = useCallback(async () => {
    if (!skill || !sourceId) return;
    setDownloadingCurrentVersion(true);
    try {
      const { blob, filename } = await marketApi.downloadSkill(
        sourceId,
        skill.item_id,
      );
      triggerBrowserDownload(
        blob,
        filename || `${skill.name}-${skill.version}.zip`,
      );
      message.success(
        "已开始下载当前版本。如需下载历史版本，请打开“版本历史”后按版本单独下载。",
      );
    } catch (err) {
      const errorMsg = err instanceof Error ? err.message : "下载失败";
      message.error(errorMsg);
    } finally {
      setDownloadingCurrentVersion(false);
    }
  }, [skill, sourceId, triggerBrowserDownload]);

  // 初始化统计配置状态
  useEffect(() => {
    if (skill) {
      setIncludeInStatistics(skill.include_in_statistics ?? false);
    }
  }, [skill]);

  // 更新统计配置
  const handleStatisticsConfigChange = useCallback(async (checked: boolean) => {
    if (!skill || !sourceId) return;

    setIsUpdatingStatistics(true);
    try {
      await marketApi.updateSkillStatisticsConfig(sourceId, skill.item_id, {
        include_in_statistics: checked,
      });
      setIncludeInStatistics(checked);
      message.success(checked ? "已纳入统计" : "已取消统计");
      onRefresh?.();
    } catch {
      message.error("更新失败");
    } finally {
      setIsUpdatingStatistics(false);
    }
  }, [skill, sourceId, onRefresh]);

  const moreMenuItems: MenuProps["items"] = useMemo(() => {
    const items: MenuProps["items"] = [];
    if (onRecall) {
      items.push({
        key: "recall",
        icon: <Undo2 size={12} />,
        label: "撤回",
        onClick: onRecall,
      });
    }
    if (onUnpublish) {
      items.push({
        key: "unpublish",
        icon: <Archive size={12} />,
        label: "下架",
        onClick: () => {
          Modal.confirm({
            title: "确认下架此技能？",
            content: "下架后用户将无法查看此技能，但数据仍保留",
            okText: "下架",
            cancelText: "取消",
            onOk: onUnpublish,
          });
        },
      });
    }
    if (onDelete) {
      items.push({
        key: "delete",
        icon: <Trash2 size={12} />,
        label: "删除",
        danger: true,
        onClick: () => {
          Modal.confirm({
            title: "彻底删除此技能？",
            content: "删除后技能文件和版本历史将全部清除，无法恢复",
            okText: "删除",
            okButtonProps: { danger: true },
            cancelText: "取消",
            onOk: onDelete,
          });
        },
      });
    }
    // 统计配置修改（仅管理员）
    if (isManager) {
      items.push({
        key: "toggle_statistics",
        icon: <BarChart3 size={12} />,
        label: includeInStatistics ? "取消纳入统计" : "纳入统计",
        onClick: () => {
          Modal.confirm({
            title: "确认操作",
            content: includeInStatistics
              ? "取消后该技能将不再纳入统计分析，如：运营看板-技能使用排行榜、定时任务技能详情等。"
              : "纳入后该技能将纳入统计分析，如：运营看板-技能使用排行榜、定时任务技能详情等。",
            okText: includeInStatistics ? "取消统计" : "纳入统计",
            cancelText: "取消",
            onOk: () => handleStatisticsConfigChange(!includeInStatistics),
          });
        },
      });
    }
    return items;
  }, [onRecall, onUnpublish, onDelete, isManager, includeInStatistics, handleStatisticsConfigChange]);

  useEffect(() => {
    if (!open || !skill || !sourceId) {
      return;
    }

    let cancelled = false;
    setFileLoading(true);
    setPreviewError(null);
    setFileDetail(null);

    marketApi.readSkillFile(sourceId, skill.item_id, "SKILL.md")
      .then((data) => {
        if (!cancelled) {
          setFileDetail(data);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setPreviewError("暂未获取到 Skill 文档预览");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setFileLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [open, skill, sourceId]);

  useEffect(() => {
    setUsagePage(1);
    setUsagePageSize(DEFAULT_USAGE_PAGE_SIZE);
  }, [open, skill?.item_id]);

  const userStatsColumns = useMemo(
    () => [
      {
        title: "用户ID",
        dataIndex: "user_id",
        key: "user_id",
        width: "30%",
      },
      {
        title: "用户名称",
        dataIndex: "user_name",
        key: "user_name",
        width: "40%",
      },
      {
        title: "调用次数",
        dataIndex: "call_count",
        key: "call_count",
        width: "30%",
        align: "right" as const,
        sorter: (
          a: { call_count: number },
          b: { call_count: number },
        ) => a.call_count - b.call_count,
      },
    ],
    [],
  );

  if (!open || !skill) {
    return null;
  }

  // 中文名和技能名
  const chineseName = skill.chinese_name?.trim() || "";
  const skillName = skill.name;

  // 简介
  const description = skill.description || "暂无描述";

  return (
    <>
      <div style={{ height: "100%", display: "flex", flexDirection: "column", backgroundColor: "#fafafa" }}>
        {/* 顶栏：固定 */}
        <div style={HEADER_STYLE}>
          {/* 单行：状态图标 + 名称 + 元数据 + 操作按钮 */}
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", width: "100%" }}>
            {/* 左侧：状态图标 + 名称 */}
            <div style={{ display: "flex", alignItems: "center", gap: 12, flex: 1, minWidth: 0 }}>
              {/* 发布状态图标（仅已发布时显示） */}
              {skill.status === "active" && (
                <Tooltip title="已发布">
                  <span style={{
                    display: "inline-flex",
                    alignItems: "center",
                    justifyContent: "center",
                    width: 22,
                    height: 22,
                    borderRadius: "50%",
                    backgroundColor: "#52c41a",
                    cursor: "pointer",
                  }}>
                    <CheckCircle size={12} style={{ color: "#fff" }} />
                  </span>
                </Tooltip>
              )}

              {/* 统计状态图标（仅已纳入统计时显示，仅管理员可见） */}
              {isManager && includeInStatistics && (
                <Tooltip title="已纳入统计">
                  <span style={{
                    display: "inline-flex",
                    alignItems: "center",
                    justifyContent: "center",
                    width: 22,
                    height: 22,
                    borderRadius: "50%",
                    backgroundColor: "#2f54eb",
                    cursor: "pointer",
                  }}>
                    <BarChart3 size={12} style={{ color: "#fff" }} />
                  </span>
                </Tooltip>
              )}

              {/* 中文名（大号） + 技能名（小号） */}
              <span style={CHINESE_NAME_STYLE}>
                {chineseName}
                {chineseName && skillName && (
                  <span style={SKILL_NAME_STYLE}> ({skillName})</span>
                )}
                {!chineseName && skillName && (
                  <span style={CHINESE_NAME_STYLE}>{skillName}</span>
                )}
              </span>

              {/* 编辑按钮 */}
              {isManager && onEdit && (
                <Tooltip title="编辑技能名称、分类和归属分行">
                  <Button
                    type="text"
                    size="small"
                    icon={<EditOutlined style={{ fontSize: 12, color: "#3769fc" }} />}
                    onClick={onEdit}
                    style={{ padding: 4 }}
                  >
                    编辑
                  </Button>
                </Tooltip>
              )}

              {/* 分类 */}
              {normalizedCategoryName && (
                <span style={META_ITEM_STYLE}>
                  <TagIcon size={12} />
                  {normalizedCategoryName}
                </span>
              )}

              {/* 版本 */}
              <span style={META_ITEM_STYLE}>
                <GitBranch size={12} />
                v{skill.version}
              </span>

              {/* 创建时间 */}
              <span style={META_ITEM_STYLE}>
                <Calendar size={12} />
                {formatDate(skill.created_at)}
              </span>

              {/* 创建人 */}
              <span style={META_ITEM_STYLE}>
                <Users size={12} />
                {skill.creator_name || "未知"}
              </span>
            </div>

            {/* 右侧：操作按钮 */}
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexShrink: 0, marginLeft: "auto" }}>
            <Button
              onClick={handleDownloadCurrentVersion}
              loading={downloadingCurrentVersion}
              style={DOWNLOAD_BUTTON_STYLE}
            >
              <DownloadOutlined style={{ fontSize: 12 }} />
              下载 ZIP
            </Button>
            <Button
              onClick={() => setVersionHistoryOpen(true)}
              style={HISTORY_BUTTON_STYLE}
            >
              <HistoryOutlined style={{ fontSize: 12 }} />
              版本历史
            </Button>
            {isManager && onDistribute && (
              <Button
                type="primary"
                onClick={onDistribute}
                style={PRIMARY_BUTTON_STYLE}
              >
                <Send size={12} />
                分发
              </Button>
            )}
            {isManager && onLookupOwners && (
              <Button
                onClick={onLookupOwners}
                style={USER_BUTTON_STYLE}
              >
                <UserOutlined style={{ fontSize: 12 }} />
                用户可执行性
              </Button>
            )}
            {isManager && moreMenuItems.length > 0 && (
              <Dropdown menu={{ items: moreMenuItems }} trigger={["click"]}>
                <Button style={MORE_BUTTON_STYLE}>
                  <MoreOutlined style={{ fontSize: 12 }} />
                </Button>
              </Dropdown>
            )}
          </div>
        </div>
      </div>

        {/* 主区域：文档 + 用户明细 */}
        <div
          style={{
            display: "flex",
            gap: 12,
            padding: 16,
            flex: 1,
            minHeight: 0,
          }}
        >
          {/* 左侧：简介 + 文档内容（可滚动） */}
          <div
            style={{
              flex: isManager ? "1 1 auto" : "1 1 100%",
              minWidth: 0,
              backgroundColor: "#fff",
              borderRadius: 8,
              padding: 20,
              overflow: "auto",
            }}
          >
            {/* 简介 */}
            <div style={{ marginBottom: 16, paddingBottom: 16, borderBottom: "1px solid #f0f0f0" }}>
              <Text style={{ fontSize: 14, color: "#595959", lineHeight: 1.6 }}>
                {description}
              </Text>
            </div>

            {/* 文档内容 */}
            {previewError ? (
              <Text type="secondary">{previewError}</Text>
            ) : fileLoading ? (
              <div
                style={{
                  display: "flex",
                  justifyContent: "center",
                  alignItems: "center",
                  minHeight: 200,
                }}
              >
                <Spin />
              </div>
            ) : (
              renderPreviewContent(
                fileDetail?.file_type ?? null,
                fileDetail?.content ?? null,
              )
            )}
          </div>

          {/* 右侧：用户明细（固定，仅管理员） */}
          {isManager && (
            <div
              style={{
                flex: "0 0 360px",
                maxWidth: 360,
                backgroundColor: "#fff",
                borderRadius: 8,
                padding: 16,
                overflow: "hidden",
              }}
            >
              {/* 标题 + 统计数据 */}
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  marginBottom: 12,
                }}
              >
                <Title level={5} style={{ margin: 0, fontSize: 13, fontWeight: 500 }}>
                  使用用户明细
                </Title>
                <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  <Tooltip title="本月调用次数（按对话去重，统计范围与技能列表一致）">
                    <Tag
                      bordered={false}
                      style={{
                        ...STAT_TAG_STYLE,
                        backgroundColor: "#eef4ff",
                        color: "#365d97",
                      }}
                    >
                      <PhoneCall size={12} />
                      {formatMetricValue(skill.call_count)}
                    </Tag>
                  </Tooltip>
                  <Tooltip title="本月使用用户数（统计范围与技能列表一致）">
                    <Tag
                      bordered={false}
                      style={{
                        ...STAT_TAG_STYLE,
                        backgroundColor: "#edf8f2",
                        color: "#2f7a55",
                      }}
                    >
                      <Users size={12} />
                      {formatMetricValue(skill.user_count)}
                    </Tag>
                  </Tooltip>
                </div>
              </div>

              {/* 用户表格 */}
              <div className={styles.usageTable}>
                <Table
                  dataSource={skill.user_stats}
                  columns={userStatsColumns}
                  rowKey="user_id"
                  pagination={{
                    current: usagePage,
                    pageSize: usagePageSize,
                    hideOnSinglePage: true,
                    showSizeChanger: true,
                    pageSizeOptions: ["5", "10", "20", "50"],
                    size: "small",
                    onChange: (nextPage, nextPageSize) => {
                      setUsagePage(nextPageSize !== usagePageSize ? 1 : nextPage);
                      setUsagePageSize(nextPageSize);
                    },
                  }}
                  size="small"
                  scroll={{ y: 380 }}
                />
              </div>
            </div>
          )}
        </div>
      </div>

      <VersionHistoryModal
        open={versionHistoryOpen}
        itemId={skill.item_id}
        skillName={chineseName || skillName}
        currentVersion={skill.version}
        sourceId={sourceId || ""}
        isManager={isManager}
        onClose={() => setVersionHistoryOpen(false)}
        onVersionSwitched={onRefresh}
      />

    </>
  );
}
