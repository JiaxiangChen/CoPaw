import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Button, Empty, Input, Spin, Tag, Typography } from "antd";
import {
  ReloadOutlined,
  RobotOutlined,
  SearchOutlined,
} from "@ant-design/icons";
import {
  marketApi,
  type Category,
  type ExpertVersion,
  type MarketExpert,
} from "../../api/modules/market";
import { useAppMessage } from "../../hooks/useAppMessage";
import { useIframeStore } from "../../stores/iframeStore";
import { DEFAULT_SOURCE_ID } from "../../constants/identity";
import { useAgentStore } from "../../stores/agentStore";
import { BBK_ID_MAP, BBK_ID_TO_NAME_MAP } from "../../constants/bbk";
import { ExpertCard } from "./ExpertCard";
import { ExpertDetailDrawer } from "./ExpertDetailDrawer";
import { ExpertVersionHistoryModal } from "./ExpertVersionHistoryModal";
import { countExpertBbkIds, matchesExpertSearch } from "./expertCommunity";
import { DistributeTargetModal } from "../Market/DistributeTargetModal";
import { ExpertRecallModal } from "./ExpertRecallModal";

const { Title, Text } = Typography;

export default function ExpertCommunityPage() {
  const sourceId = useIframeStore((state) => state.source) || DEFAULT_SOURCE_ID;
  const manager = useIframeStore((state) => state.manager);
  const userId = useIframeStore((state) => state.userId) || "default";
  const selectedAgent = useAgentStore((state) => state.selectedAgent);
  const [items, setItems] = useState<MarketExpert[]>([]);
  const [query, setQuery] = useState("");
  const [categoryId, setCategoryId] = useState<number | null>(null);
  const [bbkId, setBbkId] = useState<string | null>(null);
  const [categories, setCategories] = useState<Category[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [receivedIds, setReceivedIds] = useState<Set<string>>(() => new Set());
  const [selectedExpert, setSelectedExpert] = useState<MarketExpert | null>(
    null,
  );
  const [detailOpen, setDetailOpen] = useState(false);
  const [versionItem, setVersionItem] = useState<MarketExpert | null>(null);
  const [versions, setVersions] = useState<ExpertVersion[]>([]);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [restoringVersionId, setRestoringVersionId] = useState<string | null>(
    null,
  );
  const [distributeTarget, setDistributeTarget] = useState<MarketExpert | null>(
    null,
  );
  const [recallTarget, setRecallTarget] = useState<MarketExpert | null>(null);
  const { message } = useAppMessage();

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setItems(
        await marketApi.listMarketExperts(sourceId, {
          categoryId: categoryId ?? undefined,
          bbkIds: bbkId ? [bbkId] : [],
        }),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "加载专家社区失败");
    } finally {
      setLoading(false);
    }
  }, [bbkId, categoryId, sourceId]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    setReceivedIds(new Set());
  }, [selectedAgent, sourceId, userId]);

  useEffect(() => {
    void marketApi
      .listCategories(sourceId)
      .then(setCategories)
      .catch(() => setCategories([]));
  }, [sourceId]);

  const visibleItems = useMemo(
    () => items.filter((item) => matchesExpertSearch(item, query)),
    [items, query],
  );
  const bbkCountMap = useMemo(() => countExpertBbkIds(items), [items]);
  const categoryCountMap = useMemo(() => {
    const counts = new Map<number, number>();
    items.forEach((item) => {
      if (item.category_id !== null) {
        counts.set(item.category_id, (counts.get(item.category_id) || 0) + 1);
      }
    });
    return counts;
  }, [items]);

  const openDetail = (item: MarketExpert) => {
    setSelectedExpert(item);
    setDetailOpen(true);
  };

  const receive = async (item: MarketExpert) => {
    setBusyId(item.item_id);
    try {
      const result = await marketApi.installExpert(
        sourceId,
        item.item_id,
        userId,
        selectedAgent,
      );
      if (!result.success) {
        message.error(result.reason || "专家已接收，不能重复安装");
        return;
      }
      setReceivedIds((current) => new Set(current).add(item.item_id));
      message.success("专家已接收");
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : "接收失败");
    } finally {
      setBusyId(null);
    }
  };

  const showVersions = async (item: MarketExpert) => {
    setVersionItem(item);
    setVersionsLoading(true);
    try {
      const result = await marketApi.listExpertVersions(sourceId, item.item_id);
      setVersions(result.versions);
    } catch (reason) {
      message.error(
        reason instanceof Error ? reason.message : "加载版本历史失败",
      );
      setVersions([]);
    } finally {
      setVersionsLoading(false);
    }
  };

  const distribute = (item: MarketExpert) => setDistributeTarget(item);
  const recall = (item: MarketExpert) => setRecallTarget(item);

  const unpublish = async (item: MarketExpert) => {
    setBusyId(item.item_id);
    try {
      await marketApi.unpublishExpert(sourceId, item.item_id);
      message.success("专家已下架");
      setDetailOpen(false);
      await load();
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : "下架失败");
    } finally {
      setBusyId(null);
    }
  };

  const restoreVersion = async (versionId: string) => {
    if (!versionItem) return;
    setRestoringVersionId(versionId);
    try {
      await marketApi.restoreExpertVersion(
        sourceId,
        versionItem.item_id,
        versionId,
      );
      message.success(`已恢复到 v${versionId}`);
      await Promise.all([load(), showVersions(versionItem)]);
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : "恢复版本失败");
    } finally {
      setRestoringVersionId(null);
    }
  };

  const selectedCategoryName = categories.find(
    (category) => category.id === categoryId,
  )?.name;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div
        style={{
          padding: 16,
          borderBottom: "1px solid #f0f0f0",
          backgroundColor: "#fff",
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 16,
            marginBottom: 16,
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              minWidth: 0,
            }}
          >
            <RobotOutlined style={{ fontSize: 20, flexShrink: 0 }} />
            <Title level={4} style={{ margin: 0 }}>
              专家社区
            </Title>
          </div>
          <Button
            icon={<ReloadOutlined />}
            onClick={() => void load()}
            disabled={loading}
          >
            刷新
          </Button>
        </div>
        <div style={{ display: "flex", gap: 12 }}>
          <Input
            placeholder="搜索专家名称、描述或发布者…"
            prefix={<SearchOutlined />}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            allowClear
            style={{ flex: 1 }}
          />
          <Tag
            style={{
              margin: 0,
              display: "inline-flex",
              alignItems: "center",
              paddingInline: 12,
            }}
          >
            共 {visibleItems.length} 个
          </Tag>
        </div>
      </div>

      <div style={{ flex: 1, overflow: "hidden", display: "flex" }}>
        <aside
          aria-label="专家社区筛选"
          style={{
            width: 200,
            borderRight: "1px solid #f0f0f0",
            padding: 16,
            overflow: "auto",
            flexShrink: 0,
          }}
        >
          <div style={{ marginBottom: 12 }}>
            <Text strong style={{ fontSize: 14 }}>
              分类
            </Text>
            {categoryId !== null ? (
              <Button
                type="link"
                size="small"
                style={{ fontSize: 12, padding: "0 0 0 8px" }}
                onClick={() => setCategoryId(null)}
              >
                清除
              </Button>
            ) : null}
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <button
              type="button"
              onClick={() => setCategoryId(null)}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                padding: "8px 12px",
                border: 0,
                borderRadius: 6,
                cursor: "pointer",
                textAlign: "left",
                backgroundColor:
                  categoryId === null ? "#e6f7ff" : "transparent",
                color: categoryId === null ? "#1890ff" : "inherit",
              }}
            >
              <span>全部</span>
              <Tag style={{ margin: 0 }}>{items.length}</Tag>
            </button>
            {categories.map((category) => {
              const active = categoryId === category.id;
              return (
                <button
                  type="button"
                  key={category.id}
                  onClick={() => setCategoryId(active ? null : category.id)}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    padding: "8px 12px",
                    border: 0,
                    borderRadius: 6,
                    cursor: "pointer",
                    textAlign: "left",
                    backgroundColor: active ? "#e6f7ff" : "transparent",
                    color: active ? "#1890ff" : "inherit",
                  }}
                >
                  <span
                    style={{
                      minWidth: 0,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {category.name}
                  </span>
                  <Tag style={{ margin: 0 }}>
                    {categoryCountMap.get(category.id) || 0}
                  </Tag>
                </button>
              );
            })}
          </div>

          <div style={{ marginTop: 24, marginBottom: 12 }}>
            <Text strong style={{ fontSize: 14 }}>
              所属分行
            </Text>
            {bbkId !== null ? (
              <Button
                type="link"
                size="small"
                style={{ fontSize: 12, padding: "0 0 0 8px" }}
                onClick={() => setBbkId(null)}
              >
                清除
              </Button>
            ) : null}
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <button
              type="button"
              onClick={() => setBbkId(null)}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                padding: "8px 12px",
                border: 0,
                borderRadius: 6,
                cursor: "pointer",
                textAlign: "left",
                backgroundColor: bbkId === null ? "#e6f7ff" : "transparent",
                color: bbkId === null ? "#1890ff" : "inherit",
              }}
            >
              <span>全部</span>
              <Tag style={{ margin: 0 }}>{items.length}</Tag>
            </button>
            {BBK_ID_MAP.map((bbk) => {
              const count = bbkCountMap.get(bbk.value) || 0;
              if (!count) return null;
              const active = bbkId === bbk.value;
              return (
                <button
                  type="button"
                  key={bbk.value}
                  onClick={() => setBbkId(active ? null : bbk.value)}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    padding: "8px 12px",
                    border: 0,
                    borderRadius: 6,
                    cursor: "pointer",
                    textAlign: "left",
                    backgroundColor: active ? "#e6f7ff" : "transparent",
                    color: active ? "#1890ff" : "inherit",
                  }}
                >
                  <span
                    style={{
                      minWidth: 0,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {BBK_ID_TO_NAME_MAP[bbk.value] || bbk.label}
                  </span>
                  <Tag style={{ margin: 0 }}>{count}</Tag>
                </button>
              );
            })}
          </div>
        </aside>

        <main style={{ flex: 1, minWidth: 0, padding: 16, overflow: "auto" }}>
          <div style={{ marginBottom: 12 }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {selectedCategoryName
                ? `分类：${selectedCategoryName}`
                : "分类：全部"}
              {bbkId
                ? ` · 分行：${BBK_ID_TO_NAME_MAP[bbkId] || bbkId}`
                : " · 分行：全部"}
              {query.trim() ? ` · 搜索：${query.trim()}` : ""}
            </Text>
          </div>
          {error ? (
            <Alert
              type="error"
              showIcon
              message={error}
              action={
                <Button size="small" onClick={() => void load()}>
                  重试
                </Button>
              }
            />
          ) : loading ? (
            <div
              style={{
                display: "flex",
                justifyContent: "center",
                alignItems: "center",
                height: 240,
              }}
            >
              <Spin />
            </div>
          ) : visibleItems.length === 0 ? (
            <Empty
              description={
                query || categoryId !== null || bbkId !== null
                  ? "未找到匹配的专家"
                  : "暂无专家"
              }
              image={Empty.PRESENTED_IMAGE_SIMPLE}
            />
          ) : (
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
                gap: 16,
              }}
            >
              {visibleItems.map((item) => (
                <ExpertCard
                  key={item.item_id}
                  expert={item}
                  isManager={manager}
                  isReceived={receivedIds.has(item.item_id)}
                  busy={busyId === item.item_id}
                  onOpen={() => openDetail(item)}
                  categoryName={
                    categories.find(
                      (category) => category.id === item.category_id,
                    )?.name
                  }
                  onReceive={!manager ? () => void receive(item) : undefined}
                  onVersions={() => void showVersions(item)}
                  onDistribute={manager ? () => distribute(item) : undefined}
                  onRecall={manager ? () => recall(item) : undefined}
                  onUnpublish={manager ? () => void unpublish(item) : undefined}
                />
              ))}
            </div>
          )}
        </main>
      </div>

      <ExpertDetailDrawer
        sourceId={sourceId}
        expert={selectedExpert}
        open={detailOpen}
        isManager={manager}
        busy={Boolean(selectedExpert && busyId === selectedExpert.item_id)}
        onClose={() => setDetailOpen(false)}
        onVersions={() => {
          if (selectedExpert) void showVersions(selectedExpert);
        }}
        onDistribute={
          manager && selectedExpert
            ? () => distribute(selectedExpert)
            : undefined
        }
        onRecall={
          manager && selectedExpert ? () => recall(selectedExpert) : undefined
        }
        onUnpublish={
          manager && selectedExpert
            ? () => void unpublish(selectedExpert)
            : undefined
        }
      />

      {manager ? (
        <>
          <DistributeTargetModal
            open={Boolean(distributeTarget)}
            type="expert"
            item={distributeTarget}
            sourceId={sourceId}
            onClose={() => setDistributeTarget(null)}
            onSuccess={() => void load()}
          />
          <ExpertRecallModal
            open={Boolean(recallTarget)}
            sourceId={sourceId}
            itemId={recallTarget?.item_id || ""}
            itemName={recallTarget?.name || ""}
            onClose={() => setRecallTarget(null)}
            onSuccess={() => void load()}
          />
        </>
      ) : null}

      <ExpertVersionHistoryModal
        open={versionItem !== null}
        expertName={versionItem?.name}
        versions={versions}
        loading={versionsLoading}
        isManager={manager}
        restoringId={restoringVersionId}
        onClose={() => {
          setVersionItem(null);
          setVersions([]);
        }}
        onRestore={(versionId) => void restoreVersion(versionId)}
      />
    </div>
  );
}
