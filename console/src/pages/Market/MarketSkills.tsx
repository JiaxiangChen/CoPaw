/**
 * 应用市场页面 - 技能 + MCP 双分支
 */
import { useEffect, useState, useCallback } from "react";
import {
  Input,
  Button,
  Empty,
  Spin,
  Typography,
  Tag,
  message,
  Modal,
} from "antd";
import {
  SearchOutlined,
  ReloadOutlined,
  ShopOutlined,
  UploadOutlined,
  ArrowLeftOutlined,
} from "@ant-design/icons";
import { SkillCard } from "./SkillCard";
import { SkillDetailDrawer } from "./SkillDetailDrawer";
import { CategoryManagementModal } from "./CategoryManagementModal";
import { SkillEditModal } from "./SkillEditModal";
import {
  DistributeTargetModal,
  DistributeTargetType,
} from "./DistributeTargetModal";
import { RecallModal, RecallTargetType } from "./components/RecallModal";
import { SkillReadinessModal } from "./SkillReadinessModal";
import UploadSkillModal from "./components/UploadSkillModal";
import { MCPCard } from "./MCPCard";
import { MCPDetailDrawer } from "./MCPDetailDrawer";
import { MCPUploadModal } from "./MCPUploadModal";
import { MCPEditModal } from "./MCPEditModal";
import { useMarket } from "./useMarket";
import {
  marketApi,
  MarketSkill,
  MarketSkillDetail,
  Category,
  MarketBrowseResponse,
} from "../../api/modules/market";
import { marketMcpApi } from "../../api/modules/marketMcp";
import { BBK_ID_TO_NAME_MAP } from "../../constants/bbk";
import type { MarketMCPItem, MarketMCPDetail } from "../../api/types";

type ResourceType = "skill" | "mcp";

const { Title, Text } = Typography;

interface MarketSkillsProps {
  sourceId: string;
  isManager: boolean;
  bbkId: string;
}

export function matchesMarketSkillSearch(
  skill: MarketSkill,
  query: string,
): boolean {
  const normalizedQuery = query.trim().toLowerCase();
  return (
    skill.name.toLowerCase().includes(normalizedQuery) ||
    (skill.chinese_name?.toLowerCase().includes(normalizedQuery) ?? false) ||
    (skill.description?.toLowerCase().includes(normalizedQuery) ?? false) ||
    (skill.creator_name?.toLowerCase().includes(normalizedQuery) ?? false)
  );
}

export function getMarketSearchResultLabel(
  query: string,
  matchCount: number,
): string | null {
  return query.trim() ? `匹配 ${matchCount} 个` : null;
}

export function MarketSkills({
  sourceId,
  isManager,
  bbkId,
}: MarketSkillsProps) {
  const {
    categories,
    skills,
    loading: skillsLoading,
    statsStatus,
    selectedCategory,
    setSelectedCategory,
    selectedBbkId,
    setSelectedBbkId,
    selectedSkill,
    detailDrawerOpen,
    setDetailDrawerOpen,
    refreshCategories,
    refreshSkills,
    refreshSkillsAndDetail,
    openSkillDetail,
    browse: skillBrowse,
  } = useMarket(sourceId);

  // MCP 相关状态
  const [mcpList, setMcpList] = useState<MarketMCPItem[]>([]);
  const [mcpLoading, setMcpLoading] = useState(false);
  const [selectedMCP, setSelectedMCP] = useState<MarketMCPDetail | null>(null);
  const [mcpDetailMode, setMcpDetailMode] = useState<"list" | "detail">("list");
  const [mcpUploadModalOpen, setMcpUploadModalOpen] = useState(false);
  const [mcpEditModalOpen, setMcpEditModalOpen] = useState(false);
  const [editingMCP, setEditingMCP] = useState<MarketMCPDetail | null>(null);

  // 统一分发弹窗状态
  const [distributeModalOpen, setDistributeModalOpen] = useState(false);
  const [distributeType, setDistributeType] =
    useState<DistributeTargetType>("skill");
  const [distributeTarget, setDistributeTarget] = useState<
    MarketSkill | MarketMCPItem | null
  >(null);

  // 撤回弹窗状态
  const [recallModalOpen, setRecallModalOpen] = useState(false);
  const [recallType, setRecallType] = useState<RecallTargetType>("skill");
  const [recallItemId, setRecallItemId] = useState<string>("");
  const [recallItemName, setRecallItemName] = useState<string>("");
  const [recallSkillName, setRecallSkillName] = useState<string>("");
  const [readinessSkill, setReadinessSkill] = useState<
    MarketSkill | MarketSkillDetail | null
  >(null);

  const [searchQuery, setSearchQuery] = useState("");
  const [activeResourceType, setActiveResourceType] =
    useState<ResourceType>("skill");
  const [uploadModalOpen, setUploadModalOpen] = useState(false);
  const [categoryManagementOpen, setCategoryManagementOpen] = useState(false);
  const [skillEditOpen, setSkillEditOpen] = useState(false);
  const [editingSkill, setEditingSkill] = useState<MarketSkill | null>(null);
  const [allCategories, setAllCategories] = useState<Category[]>([]);
  const isHeadOffice = bbkId === "100";

  // 获取所有分类列表及技能数量（管理员用）
  const refreshAllCategories = useCallback(() => {
    marketApi
      .listCategories(sourceId)
      .then((data) => setAllCategories(data))
      .catch(console.error);
  }, [sourceId]);

  useEffect(() => {
    refreshAllCategories();
  }, [refreshAllCategories]);

  useEffect(() => {
    refreshCategories();
    refreshAllCategories();
    refreshSkills();
  }, [refreshCategories, refreshAllCategories, refreshSkills]);

  // Handle unpublish skill (下架)
  const handleUnpublishSkill = async (
    skill: MarketSkill | MarketSkillDetail | null,
  ) => {
    if (!skill || !sourceId) return;
    try {
      await marketApi.unpublishSkill(sourceId, skill.item_id);
      message.success("下架成功");
      if (selectedSkill?.item_id === skill.item_id) {
        setDetailDrawerOpen(false);
      }
      refreshSkills();
    } catch {
      message.error("下架失败");
    }
  };

  // Handle delete skill permanently (彻底删除)
  const handleDeleteSkill = async (
    skill: MarketSkill | MarketSkillDetail | null,
  ) => {
    if (!skill || !sourceId) return;
    try {
      await marketApi.deleteSkill(sourceId, skill.item_id);
      message.success("删除成功");
      if (selectedSkill?.item_id === skill.item_id) {
        setDetailDrawerOpen(false);
      }
      refreshSkills();
    } catch {
      message.error("删除失败");
    }
  };

  // 刷新 MCP 列表
  const [selectedMcpBbkId, setSelectedMcpBbkId] = useState<string | null>(null);
  const [mcpBrowse, setMcpBrowse] = useState<MarketBrowseResponse | null>(null);
  const refreshMCP = useCallback(async () => {
    setMcpLoading(true);
    try {
      const data = await marketApi.browseMarket(sourceId, "mcp", {
        bbkId: selectedMcpBbkId,
      });
      setMcpBrowse(data);
      setMcpList(data.items as MarketMCPItem[]);
    } catch (err) {
      console.error("获取 MCP 列表失败:", err);
    } finally {
      setMcpLoading(false);
    }
  }, [sourceId, selectedMcpBbkId]);

  // 切换资源类型时刷新
  useEffect(() => {
    if (activeResourceType === "mcp") {
      refreshMCP();
    }
  }, [activeResourceType, refreshMCP]);

  useEffect(() => {
    if (
      skillBrowse &&
      selectedCategory !== null &&
      !skillBrowse.categories.some(
        (category) => category.id === selectedCategory,
      )
    ) {
      setSelectedCategory(null);
    }
  }, [selectedCategory, setSelectedCategory, skillBrowse]);

  // 获取 MCP 详情
  const openMCPDetail = useCallback(
    async (itemId: string) => {
      try {
        const detail = await marketMcpApi.getMarketMCPDetail(itemId, sourceId);
        if (detail) {
          setSelectedMCP(detail);
          setMcpDetailMode("detail");
        }
      } catch (err) {
        console.error("获取 MCP 详情失败:", err);
      }
    },
    [sourceId],
  );

  // 删除 MCP
  const handleDeleteMCP = useCallback(
    async (target?: MarketMCPItem | MarketMCPDetail | null) => {
      const item = target || selectedMCP;
      if (!item) return;
      try {
        await marketMcpApi.deleteMarketMCP(item.item_id);
        message.success("删除成功");
        if (selectedMCP?.item_id === item.item_id) {
          setSelectedMCP(null);
          setMcpDetailMode("list");
        }
        refreshMCP();
      } catch (err) {
        console.error("删除 MCP 失败:", err);
        message.error("删除失败");
      }
    },
    [selectedMCP, refreshMCP],
  );

  const confirmDeleteMCP = useCallback(
    (target: MarketMCPItem | MarketMCPDetail) => {
      Modal.confirm({
        title: "确认删除此 MCP？",
        content: "删除操作会直接删除市场条目，但不会影响已经分发出去的用户。",
        okText: "删除",
        okButtonProps: { danger: true },
        cancelText: "取消",
        onOk: async () => {
          await handleDeleteMCP(target);
        },
      });
    },
    [handleDeleteMCP],
  );

  // 打开技能分发弹窗
  const openSkillDistributeModal = useCallback((skill: MarketSkill) => {
    setDistributeType("skill");
    setDistributeTarget(skill);
    setDistributeModalOpen(true);
  }, []);

  // 打开 MCP 分发弹窗
  const openMCPDistributeModal = useCallback((mcp: MarketMCPItem) => {
    setDistributeType("mcp");
    setDistributeTarget(mcp);
    setDistributeModalOpen(true);
  }, []);

  // 打开技能撤回弹窗
  const openSkillRecallModal = useCallback((skill: MarketSkill) => {
    setRecallType("skill");
    setRecallItemId(skill.item_id);
    setRecallItemName(skill.name);
    setRecallSkillName(skill.skill_name || skill.name);
    setRecallModalOpen(true);
  }, []);

  const openSkillReadiness = useCallback(
    (skill: MarketSkill | MarketSkillDetail) => {
      setReadinessSkill(skill);
    },
    [],
  );

  const openSkillEdit = useCallback(
    (skill: MarketSkill | MarketSkillDetail) => {
      setEditingSkill(skill);
      setSkillEditOpen(true);
    },
    [],
  );

  // 打开 MCP 撤回弹窗
  const openMCPRecallModal = useCallback(
    (mcp: MarketMCPItem | MarketMCPDetail) => {
      setRecallType("mcp");
      setRecallItemId(mcp.item_id);
      setRecallItemName(mcp.name);
      setRecallModalOpen(true);
    },
    [],
  );

  const openMCPEditModal = useCallback(
    async (target: MarketMCPItem | MarketMCPDetail) => {
      try {
        const detail =
          "config" in target
            ? target
            : await marketMcpApi.getMarketMCPDetail(target.item_id, sourceId);
        if (!detail) {
          message.error("未找到 MCP 详情");
          return;
        }
        setEditingMCP(detail);
        setMcpEditModalOpen(true);
      } catch (err) {
        console.error("打开 MCP 编辑弹窗失败:", err);
        message.error("打开编辑弹窗失败");
      }
    },
    [sourceId],
  );

  const handleMCPEditSuccess = useCallback(
    async (detail: MarketMCPDetail) => {
      setMcpEditModalOpen(false);
      setEditingMCP(null);
      refreshMCP();
      if (selectedMCP?.item_id === detail.item_id) {
        try {
          const latest = await marketMcpApi.getMarketMCPDetail(
            detail.item_id,
            sourceId,
          );
          if (latest) {
            setSelectedMCP(latest);
          }
        } catch (err) {
          console.error("刷新编辑后的 MCP 详情失败:", err);
        }
      }
    },
    [refreshMCP, selectedMCP, sourceId],
  );

  // 过滤技能列表
  const filteredSkills = skills.filter((skill) => {
    return matchesMarketSkillSearch(skill, searchQuery);
  });

  // 过滤 MCP 列表
  const filteredMCP = mcpList.filter((mcp) => {
    const query = searchQuery.toLowerCase();
    return (
      mcp.name.toLowerCase().includes(query) ||
      (mcp.chinese_name?.toLowerCase().includes(query) ?? false)
    );
  });

  // API 已按分类和分行过滤，前端只做搜索过滤
  const displayedSkills = filteredSkills;

  const displayedMCP = filteredMCP;
  const searchMatchLabel = getMarketSearchResultLabel(
    searchQuery,
    activeResourceType === "skill"
      ? displayedSkills.length
      : displayedMCP.length,
  );
  const skillCategories = (skillBrowse?.categories ?? []).map((facet) => ({
    ...(allCategories.find((category) => category.id === facet.id) ?? {
      id: facet.id,
      source_id: sourceId,
      name: facet.name,
      sort_order: facet.id,
      branch_visible: true,
    }),
    name: facet.name,
    skill_count: facet.count,
  }));
  const skillBranches = skillBrowse?.branches ?? [];
  const mcpBranches = mcpBrowse?.branches ?? [];

  const isSkillDetailMode =
    activeResourceType === "skill" && detailDrawerOpen && !!selectedSkill;
  const isMCPDetailMode =
    activeResourceType === "mcp" && mcpDetailMode === "detail" && !!selectedMCP;
  const selectedSkillCategoryName = selectedSkill?.category_id
    ? categories.find((c) => String(c.id) === String(selectedSkill.category_id))
        ?.name
    : undefined;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {/* Header */}
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
            marginBottom: 16,
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <ShopOutlined style={{ fontSize: 20 }} />
            <Title level={4} style={{ margin: 0 }}>
              应用市场
            </Title>
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            {isManager && activeResourceType === "mcp" && (
              <Button
                type="primary"
                icon={<UploadOutlined />}
                onClick={() => setMcpUploadModalOpen(true)}
              >
                上传连接器
              </Button>
            )}
            {isManager && activeResourceType === "skill" && (
              <>
                <Button
                  icon={<ShopOutlined />}
                  onClick={() => setCategoryManagementOpen(true)}
                >
                  分类管理
                </Button>
                <Button
                  type="primary"
                  icon={<UploadOutlined />}
                  onClick={() => setUploadModalOpen(true)}
                >
                  上传技能
                </Button>
              </>
            )}
          </div>
        </div>
        {isSkillDetailMode || isMCPDetailMode ? (
          <div style={{ display: "flex", gap: 12 }}>
            <Button
              icon={<ArrowLeftOutlined />}
              onClick={() => {
                if (activeResourceType === "skill") {
                  setDetailDrawerOpen(false);
                  return;
                }
                setMcpDetailMode("list");
                setSelectedMCP(null);
              }}
            >
              返回列表
            </Button>
            <Button
              icon={<ReloadOutlined />}
              onClick={() => {
                if (activeResourceType === "skill") {
                  refreshSkills();
                  return;
                }
                refreshMCP();
              }}
            >
              刷新
            </Button>
          </div>
        ) : (
          <div style={{ display: "flex", gap: 12 }}>
            <Input
              placeholder={
                activeResourceType === "skill"
                  ? "搜索技能名称、描述…"
                  : "搜索 MCP 名称"
              }
              prefix={<SearchOutlined />}
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              allowClear
              style={{ flex: 1 }}
            />
            <Button
              icon={<ReloadOutlined />}
              onClick={() => {
                if (activeResourceType === "skill") {
                  refreshCategories();
                  refreshAllCategories();
                  refreshSkills();
                } else {
                  refreshMCP();
                }
              }}
            >
              刷新
            </Button>
          </div>
        )}
        {/* 资源类型切换 */}
        <div style={{ marginTop: 12, display: "flex", gap: 8 }}>
          <div
            onClick={() => setActiveResourceType("skill")}
            style={{
              flex: 1,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              gap: 6,
              padding: "8px 12px",
              borderRadius: 6,
              cursor: "pointer",
              border: `1px solid ${
                activeResourceType === "skill" ? "#d6e4ff" : "#f0f0f0"
              }`,
              backgroundColor:
                activeResourceType === "skill" ? "#e6f4ff" : "#fff",
              color: activeResourceType === "skill" ? "#1d39c4" : "#595959",
              transition: "all 0.15s ease",
            }}
          >
            <span style={{ fontWeight: 500 }}>技能</span>
          </div>
          <div
            onClick={() => setActiveResourceType("mcp")}
            style={{
              flex: 1,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              gap: 6,
              padding: "8px 12px",
              borderRadius: 6,
              cursor: "pointer",
              border: `1px solid ${
                activeResourceType === "mcp" ? "#b7eb8f" : "#f0f0f0"
              }`,
              backgroundColor:
                activeResourceType === "mcp" ? "#f6ffed" : "#fff",
              color: activeResourceType === "mcp" ? "#389e0d" : "#595959",
              transition: "all 0.15s ease",
            }}
          >
            <span style={{ fontWeight: 500 }}>MCP</span>
          </div>
        </div>
        {searchMatchLabel && (
          <div
            aria-live="polite"
            style={{
              marginTop: 8,
              color: "#8a94a6",
              fontSize: 12,
            }}
          >
            {searchMatchLabel}
          </div>
        )}
      </div>

      {/* Content */}
      <div style={{ flex: 1, overflow: "hidden", display: "flex" }}>
        {activeResourceType === "skill" ? (
          isSkillDetailMode && selectedSkill ? (
            <div style={{ flex: 1, overflow: "hidden" }}>
              <SkillDetailDrawer
                open={detailDrawerOpen}
                skill={selectedSkill}
                onClose={() => setDetailDrawerOpen(false)}
                isManager={isManager}
                onDistribute={
                  isManager
                    ? () => openSkillDistributeModal(selectedSkill)
                    : undefined
                }
                onLookupOwners={
                  isManager
                    ? () => openSkillReadiness(selectedSkill)
                    : undefined
                }
                onRecall={
                  isManager
                    ? () => openSkillRecallModal(selectedSkill)
                    : undefined
                }
                onUnpublish={
                  isManager
                    ? () => handleUnpublishSkill(selectedSkill)
                    : undefined
                }
                onDelete={
                  isManager ? () => handleDeleteSkill(selectedSkill) : undefined
                }
                sourceId={sourceId}
                onRefresh={refreshSkillsAndDetail}
                categoryName={selectedSkillCategoryName}
                onEdit={() => openSkillEdit(selectedSkill)}
              />
            </div>
          ) : (
            <>
              {/* Sidebar - Categories */}
              <div
                style={{
                  width: 200,
                  borderRight: "1px solid #f0f0f0",
                  padding: 16,
                  overflow: "auto",
                }}
              >
                <div style={{ marginBottom: 12 }}>
                  <Text strong style={{ fontSize: 14 }}>
                    分类
                  </Text>
                  {selectedCategory !== null && (
                    <Button
                      type="link"
                      size="small"
                      style={{ fontSize: 12, padding: "0 0 0 8px" }}
                      onClick={() => setSelectedCategory(null)}
                    >
                      清除
                    </Button>
                  )}
                </div>
                <div
                  style={{ display: "flex", flexDirection: "column", gap: 4 }}
                >
                  <div
                    onClick={() => setSelectedCategory(null)}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                      padding: "8px 12px",
                      borderRadius: 6,
                      cursor: "pointer",
                      backgroundColor:
                        selectedCategory === null ? "#e6f7ff" : "transparent",
                      color: selectedCategory === null ? "#1890ff" : "inherit",
                      transition: "all 0.15s ease",
                    }}
                  >
                    <span>全部</span>
                    <Tag style={{ margin: 0 }}>
                      {skillBrowse?.category_total ?? 0}
                    </Tag>
                  </div>
                  {skillCategories.map((cat) => {
                    const isActive =
                      String(selectedCategory) === String(cat.id);
                    return (
                      <div
                        key={cat.id}
                        onClick={() =>
                          setSelectedCategory(isActive ? null : cat.id)
                        }
                        style={{
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "space-between",
                          padding: "8px 12px",
                          borderRadius: 6,
                          cursor: "pointer",
                          backgroundColor: isActive ? "#e6f7ff" : "transparent",
                          color: isActive ? "#1890ff" : "inherit",
                          transition: "all 0.15s ease",
                        }}
                      >
                        <span>{cat.name}</span>
                        <Tag style={{ margin: 0 }}>{cat.skill_count}</Tag>
                      </div>
                    );
                  })}
                </div>

                {/* 所属分行筛选 */}
                <div style={{ marginTop: 24, marginBottom: 12 }}>
                  <Text strong style={{ fontSize: 14 }}>
                    所属分行
                  </Text>
                </div>
                <div
                  style={{ display: "flex", flexDirection: "column", gap: 4 }}
                >
                  {isHeadOffice ? (
                    // 管理员：显示全部选项和分行列表
                    <>
                      <div
                        onClick={() => setSelectedBbkId(null)}
                        style={{
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "space-between",
                          padding: "8px 12px",
                          borderRadius: 6,
                          cursor: "pointer",
                          backgroundColor:
                            selectedBbkId === null ? "#e6f7ff" : "transparent",
                          color: selectedBbkId === null ? "#1890ff" : "inherit",
                          transition: "all 0.15s ease",
                        }}
                      >
                        <span>全部</span>
                        <Tag style={{ margin: 0 }}>
                          {skillBrowse?.branch_total ?? 0}
                        </Tag>
                      </div>
                      {skillBranches.map((b) => {
                        const isActive = selectedBbkId === b.bbk_id;
                        return (
                          <div
                            key={b.bbk_id}
                            onClick={() =>
                              setSelectedBbkId(isActive ? null : b.bbk_id)
                            }
                            style={{
                              display: "flex",
                              alignItems: "center",
                              justifyContent: "space-between",
                              padding: "8px 12px",
                              borderRadius: 6,
                              cursor: "pointer",
                              backgroundColor: isActive
                                ? "#e6f7ff"
                                : "transparent",
                              color: isActive ? "#1890ff" : "inherit",
                              transition: "all 0.15s ease",
                            }}
                          >
                            <span>
                              {BBK_ID_TO_NAME_MAP[b.bbk_id] || b.bbk_id}
                            </span>
                            <Tag style={{ margin: 0 }}>{b.count}</Tag>
                          </div>
                        );
                      })}
                    </>
                  ) : (
                    // 分行用户：显示全部、总行和本分行
                    <>
                      <div
                        onClick={() => setSelectedBbkId(null)}
                        style={{
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "space-between",
                          padding: "8px 12px",
                          borderRadius: 6,
                          cursor: "pointer",
                          backgroundColor:
                            selectedBbkId === null ? "#e6f7ff" : "transparent",
                          color: selectedBbkId === null ? "#1890ff" : "inherit",
                        }}
                      >
                        <span>全部</span>
                        <Tag style={{ margin: 0 }}>
                          {skillBrowse?.category_total ?? 0}
                        </Tag>
                      </div>
                      <div
                        onClick={() =>
                          setSelectedBbkId(
                            selectedBbkId === "100" ? null : "100",
                          )
                        }
                        style={{
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "space-between",
                          padding: "8px 12px",
                          borderRadius: 6,
                          cursor: "pointer",
                          backgroundColor:
                            selectedBbkId === "100" ? "#e6f7ff" : "transparent",
                          color:
                            selectedBbkId === "100" ? "#1890ff" : "inherit",
                        }}
                      >
                        <span>总行</span>
                        <Tag style={{ margin: 0 }}>
                          {skillBranches.find(
                            (branch) => branch.bbk_id === "100",
                          )?.count ?? 0}
                        </Tag>
                      </div>
                      {bbkId !== "100" && (
                        <div
                          onClick={() =>
                            setSelectedBbkId(
                              selectedBbkId === bbkId ? null : bbkId,
                            )
                          }
                          style={{
                            display: "flex",
                            alignItems: "center",
                            justifyContent: "space-between",
                            padding: "8px 12px",
                            borderRadius: 6,
                            cursor: "pointer",
                            backgroundColor:
                              selectedBbkId === bbkId
                                ? "#e6f7ff"
                                : "transparent",
                            color:
                              selectedBbkId === bbkId ? "#1890ff" : "inherit",
                          }}
                        >
                          <span>{BBK_ID_TO_NAME_MAP[bbkId] || bbkId}</span>
                          <Tag style={{ margin: 0 }}>
                            {skillBranches.find(
                              (branch) => branch.bbk_id === bbkId,
                            )?.count ?? 0}
                          </Tag>
                        </div>
                      )}
                    </>
                  )}
                </div>
              </div>

              {/* 技能卡片列表 */}
              <div style={{ flex: 1, padding: 16, overflow: "auto" }}>
                {skillsLoading ? (
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "center",
                      alignItems: "center",
                      height: 200,
                    }}
                  >
                    <Spin />
                  </div>
                ) : displayedSkills.length === 0 ? (
                  <Empty
                    description={searchQuery ? "未找到匹配的技能" : "暂无技能"}
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                  />
                ) : (
                  <div
                    style={{
                      display: "grid",
                      gridTemplateColumns: "repeat(2, 1fr)",
                      gap: 16,
                    }}
                  >
                    {displayedSkills.map((skill) => {
                      const catName = skill.category_id
                        ? categories.find(
                            (c) => String(c.id) === String(skill.category_id),
                          )?.name
                        : undefined;
                      return (
                        <SkillCard
                          key={skill.item_id}
                          skill={skill}
                          statsStatus={statsStatus}
                          categoryName={catName}
                          onClick={() => openSkillDetail(skill.item_id)}
                          onDistribute={
                            isManager
                              ? () => openSkillDistributeModal(skill)
                              : undefined
                          }
                          onLookupOwners={
                            isManager
                              ? () => openSkillReadiness(skill)
                              : undefined
                          }
                          onUnpublish={
                            isManager
                              ? () => handleUnpublishSkill(skill)
                              : undefined
                          }
                          onDelete={
                            isManager
                              ? () => handleDeleteSkill(skill)
                              : undefined
                          }
                          isManager={isManager}
                        />
                      );
                    })}
                  </div>
                )}
              </div>
            </>
          )
        ) : (
          /* MCP 分支 - 左侧侧边栏 + 右侧卡片列表 */
          <div style={{ flex: 1, overflow: "hidden", display: "flex" }}>
            {/* MCP 侧边栏 - 分行筛选 */}
            <div
              style={{
                width: 200,
                borderRight: "1px solid #f0f0f0",
                padding: 16,
                overflow: "auto",
              }}
            >
              <div style={{ marginBottom: 12 }}>
                <Text strong style={{ fontSize: 14 }}>
                  所属分行
                </Text>
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                {isHeadOffice ? (
                  // 管理员：显示全部选项和分行列表
                  <>
                    <div
                      onClick={() => setSelectedMcpBbkId(null)}
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                        padding: "8px 12px",
                        borderRadius: 6,
                        cursor: "pointer",
                        backgroundColor:
                          selectedMcpBbkId === null ? "#e6f7ff" : "transparent",
                        color:
                          selectedMcpBbkId === null ? "#1890ff" : "inherit",
                        transition: "all 0.15s ease",
                      }}
                    >
                      <span>全部</span>
                      <Tag style={{ margin: 0 }}>
                        {mcpBrowse?.branch_total ?? 0}
                      </Tag>
                    </div>
                    {mcpBranches.map((b) => {
                      const isActive = selectedMcpBbkId === b.bbk_id;
                      return (
                        <div
                          key={b.bbk_id}
                          onClick={() =>
                            setSelectedMcpBbkId(isActive ? null : b.bbk_id)
                          }
                          style={{
                            display: "flex",
                            alignItems: "center",
                            justifyContent: "space-between",
                            padding: "8px 12px",
                            borderRadius: 6,
                            cursor: "pointer",
                            backgroundColor: isActive
                              ? "#e6f7ff"
                              : "transparent",
                            color: isActive ? "#1890ff" : "inherit",
                            transition: "all 0.15s ease",
                          }}
                        >
                          <span>
                            {BBK_ID_TO_NAME_MAP[b.bbk_id] || b.bbk_id}
                          </span>
                          <Tag style={{ margin: 0 }}>{b.count}</Tag>
                        </div>
                      );
                    })}
                  </>
                ) : (
                  // 分行用户：显示全部、总行和本分行
                  <>
                    <div
                      onClick={() => setSelectedMcpBbkId(null)}
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                        padding: "8px 12px",
                        borderRadius: 6,
                        cursor: "pointer",
                        backgroundColor:
                          selectedMcpBbkId === null ? "#e6f7ff" : "transparent",
                        color:
                          selectedMcpBbkId === null ? "#1890ff" : "inherit",
                      }}
                    >
                      <span>全部</span>
                      <Tag style={{ margin: 0 }}>
                        {mcpBrowse?.category_total ?? 0}
                      </Tag>
                    </div>
                    <div
                      onClick={() =>
                        setSelectedMcpBbkId(
                          selectedMcpBbkId === "100" ? null : "100",
                        )
                      }
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                        padding: "8px 12px",
                        borderRadius: 6,
                        cursor: "pointer",
                        backgroundColor:
                          selectedMcpBbkId === "100"
                            ? "#e6f7ff"
                            : "transparent",
                        color:
                          selectedMcpBbkId === "100" ? "#1890ff" : "inherit",
                      }}
                    >
                      <span>总行</span>
                      <Tag style={{ margin: 0 }}>
                        {mcpBranches.find((branch) => branch.bbk_id === "100")
                          ?.count ?? 0}
                      </Tag>
                    </div>
                    {bbkId !== "100" && (
                      <div
                        onClick={() =>
                          setSelectedMcpBbkId(
                            selectedMcpBbkId === bbkId ? null : bbkId,
                          )
                        }
                        style={{
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "space-between",
                          padding: "8px 12px",
                          borderRadius: 6,
                          cursor: "pointer",
                          backgroundColor:
                            selectedMcpBbkId === bbkId
                              ? "#e6f7ff"
                              : "transparent",
                          color:
                            selectedMcpBbkId === bbkId ? "#1890ff" : "inherit",
                        }}
                      >
                        <span>{BBK_ID_TO_NAME_MAP[bbkId] || bbkId}</span>
                        <Tag style={{ margin: 0 }}>
                          {mcpBranches.find((branch) => branch.bbk_id === bbkId)
                            ?.count ?? 0}
                        </Tag>
                      </div>
                    )}
                  </>
                )}
              </div>
            </div>

            {/* MCP 卡片列表区域 */}
            <div style={{ flex: 1, padding: 16, overflow: "auto" }}>
              {mcpDetailMode === "detail" && selectedMCP ? (
                <MCPDetailDrawer
                  mcp={selectedMCP}
                  sourceId={sourceId}
                  onDistribute={
                    isManager
                      ? () => openMCPDistributeModal(selectedMCP)
                      : undefined
                  }
                  onRecall={
                    isManager
                      ? () => openMCPRecallModal(selectedMCP)
                      : undefined
                  }
                  onEdit={() => void openMCPEditModal(selectedMCP)}
                  onDelete={
                    isManager ? () => confirmDeleteMCP(selectedMCP) : undefined
                  }
                  onRefresh={refreshMCP}
                  canEdit={isManager}
                  isManager={isManager}
                />
              ) : (
                <>
                  {mcpLoading ? (
                    <div
                      style={{
                        display: "flex",
                        justifyContent: "center",
                        alignItems: "center",
                        height: 200,
                      }}
                    >
                      <Spin />
                    </div>
                  ) : displayedMCP.length === 0 ? (
                    <Empty
                      description={
                        searchQuery ? "未找到匹配的 MCP" : "暂无 MCP"
                      }
                      image={Empty.PRESENTED_IMAGE_SIMPLE}
                    />
                  ) : (
                    <div
                      style={{
                        display: "grid",
                        gridTemplateColumns: "repeat(2, 1fr)",
                        gap: 16,
                      }}
                    >
                      {displayedMCP.map((mcp) => (
                        <MCPCard
                          key={mcp.item_id}
                          mcp={mcp}
                          onOpenDetail={() => openMCPDetail(mcp.item_id)}
                          onDistribute={
                            isManager
                              ? () => openMCPDistributeModal(mcp)
                              : undefined
                          }
                          onEdit={() => void openMCPEditModal(mcp)}
                          onDelete={
                            isManager ? () => confirmDeleteMCP(mcp) : undefined
                          }
                          canEdit={isManager}
                          isManager={isManager}
                        />
                      ))}
                    </div>
                  )}
                </>
              )}
            </div>
          </div>
        )}
      </div>

      {/* 技能上传弹窗 */}
      <UploadSkillModal
        open={uploadModalOpen}
        sourceId={sourceId}
        onClose={() => setUploadModalOpen(false)}
        onSuccess={() => {
          refreshSkillsAndDetail();
        }}
      />

      {isManager && (
        <CategoryManagementModal
          open={categoryManagementOpen}
          sourceId={sourceId}
          onClose={() => setCategoryManagementOpen(false)}
          onChanged={async () => {
            await refreshCategories();
            await refreshAllCategories();
            await refreshSkills();
          }}
        />
      )}

      {isManager && (
        <SkillEditModal
          open={skillEditOpen}
          sourceId={sourceId}
          skill={editingSkill}
          categories={categories}
          onClose={() => {
            setSkillEditOpen(false);
            setEditingSkill(null);
          }}
          onSuccess={async () => {
            await refreshSkillsAndDetail();
            setEditingSkill(null);
          }}
        />
      )}

      {/* 统一分发弹窗 */}
      {isManager && (
        <DistributeTargetModal
          open={distributeModalOpen}
          type={distributeType}
          item={distributeTarget}
          sourceId={sourceId}
          onClose={() => {
            setDistributeModalOpen(false);
            setDistributeTarget(null);
          }}
          onSuccess={() => {
            setDistributeModalOpen(false);
            setDistributeTarget(null);
            if (distributeType === "skill") {
              refreshSkills();
            } else {
              refreshMCP();
            }
          }}
        />
      )}

      {/* MCP 上传弹窗 */}
      <MCPUploadModal
        open={mcpUploadModalOpen}
        onClose={() => setMcpUploadModalOpen(false)}
        onSuccess={() => {
          refreshMCP();
        }}
      />

      <MCPEditModal
        open={mcpEditModalOpen}
        mcp={editingMCP}
        onClose={() => {
          setMcpEditModalOpen(false);
          setEditingMCP(null);
        }}
        onSuccess={(detail) => {
          void handleMCPEditSuccess(detail);
        }}
      />

      {/* 撤回弹窗 */}
      {isManager && (
        <RecallModal
          open={recallModalOpen}
          type={recallType}
          itemId={recallItemId}
          itemName={recallItemName}
          skillName={recallSkillName}
          sourceId={sourceId}
          onClose={() => {
            setRecallModalOpen(false);
            setRecallItemId("");
            setRecallItemName("");
            setRecallSkillName("");
          }}
          onSuccess={() => {
            setRecallModalOpen(false);
            setRecallItemId("");
            setRecallItemName("");
            setRecallSkillName("");
            if (recallType === "skill") {
              refreshSkills();
            } else {
              refreshMCP();
            }
          }}
        />
      )}

      {isManager && (
        <SkillReadinessModal
          open={Boolean(readinessSkill)}
          skill={readinessSkill}
          onClose={() => setReadinessSkill(null)}
        />
      )}
    </div>
  );
}
