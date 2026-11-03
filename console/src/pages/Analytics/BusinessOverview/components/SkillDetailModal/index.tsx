import { useState, useEffect, useCallback } from "react";
import { Modal, Spin, Pagination, Tag, Empty, message } from "antd";
import { Wrench, User, Bot } from "lucide-react";
import dayjs from "dayjs";
import { tracingApi, TraceListItem, TraceDetail } from "../../../../../api/modules/tracing";
import { getBbkDisplayName } from "../../../../../constants/bbk";
import styles from "./index.module.less";

const DEFAULT_TRACE_PAGE_SIZE = 10;

interface SkillDetailModalProps {
  open: boolean;
  skillName: string;
  skillDisplayName?: string;
  startDate: string;
  endDate: string;
  bbkIds?: string;
  onClose: () => void;
}

export default function SkillDetailModal({
  open,
  skillName,
  skillDisplayName,
  startDate,
  endDate,
  bbkIds,
  onClose,
}: SkillDetailModalProps) {
  // 对话列表状态
  const [traces, setTraces] = useState<TraceListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_TRACE_PAGE_SIZE);
  const [loading, setLoading] = useState(false);

  // 选中的对话
  const [selectedTraceId, setSelectedTraceId] = useState<string | null>(null);
  const [traceDetail, setTraceDetail] = useState<TraceDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  // 格式化用户显示：机构/用户姓名
  const formatUserDisplay = (trace: { user_name?: string | null; user_id: string; bbk_id?: string | null }) => {
    const parts: string[] = [];
    if (trace.bbk_id) {
      const bbkName = getBbkDisplayName(trace.bbk_id);
      if (bbkName && bbkName !== "-") {
        parts.push(bbkName);
      }
    }
    if (trace.user_name) {
      parts.push(trace.user_name);
    }
    if (parts.length > 0) {
      return `${parts.join("/")}`;
    }
    return trace.user_id || "-";
  };

  // 获取对话列表
  const fetchTraces = useCallback(async (pageNum: number, pageSizeNum: number) => {
    if (!skillName) return;
    setLoading(true);
    try {
      const data = await tracingApi.getSkillTraces(skillName, pageNum, pageSizeNum, {
        start_date: startDate,
        end_date: endDate,
        bbk_ids: bbkIds,
      });
      setTraces(data.items || []);
      setTotal(data.total || 0);
      // 默认选中第一个对话
      setTraceDetail(null);
      setSelectedTraceId(data.items?.[0]?.trace_id ?? null);
    } catch (error) {
      console.error("Failed to fetch skill traces:", error);
      message.error("获取技能对话列表失败");
    } finally {
      setLoading(false);
    }
  }, [skillName, startDate, endDate, bbkIds]);

  // 获取对话详情
  const fetchTraceDetail = useCallback(async (traceId: string) => {
    if (!traceId) return;
    setDetailLoading(true);
    try {
      const detail = await tracingApi.getTraceDetail(traceId);
      setTraceDetail(detail);
    } catch (error) {
      console.error("Failed to fetch trace detail:", error);
      message.error("获取对话详情失败");
    } finally {
      setDetailLoading(false);
    }
  }, []);

  // Modal 打开时加载数据
  useEffect(() => {
    if (open && skillName) {
      setPage(1);
      setPageSize(DEFAULT_TRACE_PAGE_SIZE);
      setTraceDetail(null);
      fetchTraces(1, DEFAULT_TRACE_PAGE_SIZE);
    }
  }, [open, skillName, fetchTraces]);

  // 选中对话变化时加载详情
  useEffect(() => {
    if (selectedTraceId) {
      fetchTraceDetail(selectedTraceId);
    }
  }, [selectedTraceId, fetchTraceDetail]);

  // 关闭时重置状态
  const handleClose = () => {
    setTraces([]);
    setTotal(0);
    setPage(1);
    setPageSize(DEFAULT_TRACE_PAGE_SIZE);
    setSelectedTraceId(null);
    setTraceDetail(null);
    onClose();
  };

  // 分页变化
  const handlePageChange = (newPage: number, newPageSize: number) => {
    const nextPage = newPageSize !== pageSize ? 1 : newPage;
    setPage(nextPage);
    setPageSize(newPageSize);
    fetchTraces(nextPage, newPageSize);
  };

  // 选择对话
  const handleSelectTrace = (traceId: string) => {
    setSelectedTraceId(traceId);
  };

  // 格式化 Token 数量
  const formatTokens = (tokens: number) => {
    if (!tokens) return "0";
    if (tokens < 1000) return tokens.toString();
    if (tokens < 1000000) return `${(tokens / 1000).toFixed(1)}K`;
    return `${(tokens / 1000000).toFixed(1)}M`;
  };

  // 格式化时长
  const formatDuration = (ms: number | null) => {
    if (!ms) return "-";
    if (ms < 1000) return `${ms}ms`;
    if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
    return `${(ms / 60000).toFixed(1)}m`;
  };

  // 格式化时间
  const formatTime = (time: string | null) => {
    if (!time) return "-";
    return dayjs(time).format("MM-DD HH:mm:ss");
  };

  // 截断 ID 显示
  const truncateId = (id: string) => {
    if (id.length <= 20) return id;
    return id.slice(0, 20) + "...";
  };

  // 获取状态颜色
  const getStatusColor = (status: string) => {
    switch (status) {
      case "completed":
        return "success";
      case "running":
        return "processing";
      case "error":
        return "error";
      case "cancelled":
        return "default";
      default:
        return "default";
    }
  };

  return (
    <Modal
      title={
        <span>
          <Wrench size={18} style={{ marginRight: 8 }} />
          技能「{skillDisplayName || skillName}」调用记录
        </span>
      }
      open={open}
      onCancel={handleClose}
      width={900}
      footer={null}
      destroyOnClose
    >
      <div className={styles.modalContent}>
        {/* 左侧：对话列表 */}
        <div className={styles.leftPanel}>
          <div className={styles.traceListTitle}>对话列表</div>

          {loading ? (
            <div className={styles.traceListLoading}>
              <Spin size="small" />
            </div>
          ) : traces.length === 0 ? (
            <div className={styles.traceListEmpty}>暂无调用记录</div>
          ) : (
            <>
              {traces.map((trace) => (
                <div
                  key={trace.trace_id}
                  className={`${styles.traceCard} ${
                    selectedTraceId === trace.trace_id ? styles.selected : ""
                  }`}
                  onClick={() => handleSelectTrace(trace.trace_id)}
                >
                  <div className={styles.traceCardHeader}>
                    <Tag color={getStatusColor(trace.status)} style={{ margin: 0 }}>
                      {trace.status}
                    </Tag>
                    <span className={styles.traceCardTime}>
                      {formatTime(trace.start_time)}
                    </span>
                  </div>
                  <div className={styles.traceCardMeta}>
                    <span>{formatUserDisplay(trace)}</span>
                    <span>时长: {formatDuration(trace.duration_ms)}</span>
                  </div>
                  <div className={styles.traceCardStats}>
                    <span>Token: {formatTokens(trace.total_tokens)}</span>
                    {trace.model_name && (
                      <span className={styles.traceCardModel}>
                        {truncateId(trace.model_name)}
                      </span>
                    )}
                  </div>
                </div>
              ))}

              {total > pageSize && (
                <div className={styles.traceListPagination}>
                  <Pagination
                    simple
                    current={page}
                    pageSize={pageSize}
                    showSizeChanger
                    pageSizeOptions={["5", "10", "20", "50"]}
                    total={total}
                    onChange={handlePageChange}
                  />
                </div>
              )}
            </>
          )}
        </div>

        {/* 右侧：对话详情 */}
        <div className={styles.rightPanel}>
          <div className={styles.detailTitle}>对话详情</div>

          {detailLoading ? (
            <div className={styles.detailLoading}>
              <Spin />
            </div>
          ) : traceDetail ? (
            <div className={styles.detailContent}>
              {/* 基础信息 */}
              <div className={styles.detailSection}>
                <div className={styles.detailRow}>
                  <span className={styles.detailLabel}>对话ID:</span>
                  <span className={styles.detailValue}>
                    {truncateId(traceDetail.trace.trace_id)}
                  </span>
                </div>
                <div className={styles.detailRow}>
                  <span className={styles.detailLabel}>用户:</span>
                  <span className={styles.detailValue}>
                    {formatUserDisplay(traceDetail.trace)}
                  </span>
                </div>
                <div className={styles.detailRow}>
                  <span className={styles.detailLabel}>模型:</span>
                  <span className={styles.detailValue}>
                    {traceDetail.trace.model_name || "-"}
                  </span>
                </div>
                <div className={styles.detailRow}>
                  <span className={styles.detailLabel}>Token:</span>
                  <span className={styles.detailValue}>
                    输入 {formatTokens(traceDetail.trace.total_input_tokens)} /
                    输出 {formatTokens(traceDetail.trace.total_output_tokens)}
                  </span>
                </div>
              </div>

              {/* 技能使用 */}
              {traceDetail.trace.skills_used && traceDetail.trace.skills_used.length > 0 && (
                <div className={styles.skillsSection}>
                  <span className={styles.sectionLabel}>技能使用:</span>
                  <div className={styles.tagList}>
                    {traceDetail.trace.skills_used.map((skill) => (
                      <Tag key={skill} color="blue">
                        {skill}
                      </Tag>
                    ))}
                  </div>
                </div>
              )}

              {/* 用户输入 */}
              {traceDetail.trace.user_message && (
                <div className={styles.userMessage}>
                  <div className={styles.messageLabel}>
                    <User size={14} style={{ marginRight: 4 }} />
                    用户输入
                  </div>
                  <div className={styles.messageContent}>
                    {traceDetail.trace.user_message}
                  </div>
                </div>
              )}

              {/* 模型输出 - 仅在 completed 状态下展示 */}
              {traceDetail.trace.model_output && traceDetail.trace.status === "completed" && (
                <div className={styles.modelOutput}>
                  <div className={styles.messageLabel}>
                    <Bot size={14} style={{ marginRight: 4 }} />
                    模型输出
                  </div>
                  <div className={styles.messageContent}>
                    {traceDetail.trace.model_output}
                  </div>
                </div>
              )}

              {/* 错误信息 */}
              {traceDetail.trace.error && (
                <div className={styles.errorSection}>
                  <span className={styles.sectionLabel}>错误信息:</span>
                  <pre className={styles.errorText}>{traceDetail.trace.error}</pre>
                </div>
              )}
            </div>
          ) : (
            <Empty description="请选择对话查看详情" />
          )}
        </div>
      </div>
    </Modal>
  );
}
