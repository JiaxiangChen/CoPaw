import { Card, Tag, Typography, Button, Space, Popconfirm, Dropdown, Popover, type MenuProps } from "antd";
import { MarketSkill } from "../../api/modules/market";
import { BBK_ID_TO_NAME_MAP } from "../../constants/bbk";
import { Users, PhoneCall, Calendar, GitBranch, CheckCircle, Sparkles, Tag as TagIcon, Eye, Trash2, Send, MoreVertical, Archive, Building2, BarChart3 } from "lucide-react";

const { Text } = Typography;

interface SkillCardProps {
  skill: MarketSkill;
  onClick: () => void;
  onDistribute?: () => void;
  onLookupOwners?: () => void;
  onUnpublish?: () => void;
  onDelete?: () => void;
  isManager: boolean;
  isInstalled?: boolean;
  isFeatured?: boolean;
  categoryName?: string;
  statsStatus?: "loading" | "ready" | "error";
}

export function SkillCard({ skill, onClick, onDistribute, onLookupOwners, onUnpublish, onDelete, isManager, isInstalled, isFeatured, categoryName, statsStatus = "ready" }: SkillCardProps) {
  const formatMetricValue = (value: number | null): string => {
    if (value === null) return "0";
    if (value >= 100000000) return `${(value / 100000000).toFixed(1)}亿`;
    if (value >= 10000) return `${(value / 10000).toFixed(1)}万`;
    if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
    return String(value);
  };
  const managerMenuItems: NonNullable<MenuProps["items"]> = [
    onLookupOwners
      ? {
          key: "lookup_owners",
          label: (
            <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <Users size={14} />
              用户可执行性
            </span>
          ),
          onClick: onLookupOwners,
        }
      : null,
    onUnpublish
      ? {
          key: "unpublish",
          label: (
            <Popconfirm
              title="确认下架此技能？"
              description="下架后用户将无法查看此技能，但数据仍保留"
              onConfirm={onUnpublish}
              okText="下架"
              cancelText="取消"
            >
              <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <Archive size={14} />
                下架
              </span>
            </Popconfirm>
          ),
        }
      : null,
    onDelete
      ? {
          key: "delete",
          label: (
            <Popconfirm
              title="彻底删除此技能？"
              description="删除后技能文件和版本历史将全部清除，无法恢复"
              onConfirm={onDelete}
              okText="删除"
              okButtonProps={{ danger: true }}
              cancelText="取消"
            >
              <span style={{ display: "flex", alignItems: "center", gap: 8, color: "#ff4d4f" }}>
                <Trash2 size={14} />
                彻底删除
              </span>
            </Popconfirm>
          ),
        }
      : null,
  ].filter(Boolean) as NonNullable<MenuProps["items"]>;

  return (
    <div
      className="group"
      style={{
        padding: 20,
        borderRadius: 16,
        border: "1px solid #f0eee6",
        backgroundColor: "#fff",
        cursor: "pointer",
        transition: "all 0.2s ease",
        display: "flex",
        flexDirection: "column",
      }}
      onClick={onClick}
      onMouseEnter={(e) => {
        e.currentTarget.style.backgroundColor = "#faf9f5";
        e.currentTarget.style.borderColor = "#e8e6dc";
        e.currentTarget.style.boxShadow = "rgba(0,0,0,0.06) 0px 4px 20px";
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.backgroundColor = "#fff";
        e.currentTarget.style.borderColor = "#f0eee6";
        e.currentTarget.style.boxShadow = "none";
      }}
    >
      {/* Header: name + badges */}
      <div style={{ marginBottom: 12, flex: 1 }}>
        <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 4 }}>
              <Text strong style={{ fontSize: 15, color: "#141413" }}>
                {skill.chinese_name ? (
                  <>
                    {skill.chinese_name}
                    <span style={{ marginLeft: 6, color: "#87867f", fontWeight: 400, fontSize: 14 }}>
                      ({skill.name})
                    </span>
                  </>
                ) : (
                  skill.name
                )}
              </Text>
              {isFeatured && (
                <Tag
                  style={{
                    fontSize: 11,
                    fontWeight: 500,
                    backgroundColor: "#fdf3e7",
                    color: "#c4956a",
                    border: "1px solid #f5d9c4",
                    borderRadius: 999,
                    padding: "0 8px",
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 4,
                  }}
                >
                  <Sparkles size={12} style={{ fill: "#c4956a" }} />
                  精品
                </Tag>
              )}
              {categoryName && (
                <Tag
                  style={{
                    fontSize: 11,
                    color: "#5e5d59",
                    backgroundColor: "#f5f4ed",
                    border: "1px solid #e8e6dc",
                    borderRadius: 999,
                    padding: "0 8px",
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 4,
                  }}
                >
                  <TagIcon size={12} style={{ color: "#87867f" }} />
                  {categoryName}
                </Tag>
              )}
              {skill.bbk_ids?.length > 0 && (
                <>
                  {/* 分行 Tag：单个分行直接显示，多个分行显示首个+N，hover展开全部 */}
                  {skill.bbk_ids.length === 1 ? (
                    <Tag
                      style={{
                        fontSize: 11,
                        color: "#5e5d59",
                        backgroundColor: "#f5f4ed",
                        border: "1px solid #e8e6dc",
                        borderRadius: 999,
                        padding: "0 8px",
                        display: "inline-flex",
                        alignItems: "center",
                        gap: 4,
                      }}
                    >
                      <Building2 size={12} style={{ color: "#87867f" }} />
                      {BBK_ID_TO_NAME_MAP[skill.bbk_ids[0]] || skill.bbk_ids[0]}
                    </Tag>
                  ) : (
                    <Popover
                      trigger="hover"
                      placement="bottom"
                      content={
                        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                          {skill.bbk_ids.map((bbkId) => (
                            <span key={bbkId} style={{ fontSize: 12 }}>
                              {BBK_ID_TO_NAME_MAP[bbkId] || bbkId}
                            </span>
                          ))}
                        </div>
                      }
                    >
                      <Tag
                        style={{
                          fontSize: 11,
                          color: "#5e5d59",
                          backgroundColor: "#f5f4ed",
                          border: "1px solid #e8e6dc",
                          borderRadius: 999,
                          padding: "0 8px",
                          display: "inline-flex",
                          alignItems: "center",
                          gap: 4,
                          cursor: "pointer",
                        }}
                      >
                        <Building2 size={12} style={{ color: "#87867f" }} />
                        {BBK_ID_TO_NAME_MAP[skill.bbk_ids[0]] || skill.bbk_ids[0]}
                        <span style={{ color: "#87867f" }}>+{skill.bbk_ids.length - 1}</span>
                      </Tag>
                    </Popover>
                  )}
                </>
              )}
              {isInstalled && (
                <Tag
                  style={{
                    fontSize: 11,
                    fontWeight: 500,
                    backgroundColor: "#edf7f0",
                    color: "#2e7d4f",
                    border: "1px solid #c4e8d1",
                    borderRadius: 999,
                    padding: "0 8px",
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 4,
                  }}
                >
                  <CheckCircle size={12} />
                  已安装
                </Tag>
              )}
              {/* 统计徽章：仅管理员可见，且技能已纳入统计时显示 */}
              {isManager && skill.include_in_statistics && (
                <Tag
                  style={{
                    fontSize: 11,
                    fontWeight: 500,
                    backgroundColor: "#e6f4ff",
                    color: "#1677ff",
                    border: "1px solid #91caff",
                    borderRadius: 999,
                    padding: "0 8px",
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 4,
                  }}
                >
                  <BarChart3 size={12} />
                  纳入统计
                </Tag>
              )}
            </div>
            {skill.description && (
              <p
                style={{
                  display: "-webkit-box",
                  WebkitLineClamp: 2,
                  WebkitBoxOrient: "vertical",
                  overflow: "hidden",
                  fontSize: 14,
                  color: "#87867f",
                  marginTop: 8,
                  lineHeight: "22px",
                  margin: "8px 0 0",
                  wordBreak: "break-word",
                }}
              >
                {skill.description || "暂无描述"}
              </p>
            )}
          </div>
          {/* Stats badges */}
          <div aria-busy={statsStatus === "loading"} title={statsStatus === "loading" ? "统计加载中" : statsStatus === "error" ? "统计暂不可用，刷新可重试" : undefined} style={{ marginLeft: 12, display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 6, flexShrink: 0 }}>
            <div
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                padding: "4px 8px",
                borderRadius: 8,
                border: "1px solid #d7e2f5",
                background: "linear-gradient(135deg, #f4f8ff 0%, #ebf2ff 100%)",
                color: "#365d97",
                boxShadow: "rgba(54,93,151,0.06) 0px 2px 6px",
              }}
            >
              <PhoneCall size={12} />
              <span style={{ fontSize: 11, color: "#6a7fa5" }}>调用</span>
              <span style={{ fontSize: 12, fontWeight: 600, fontVariantNumeric: "tabular-nums" }}>
                {statsStatus === "ready" ? formatMetricValue(skill.call_count) : "—"}
              </span>
            </div>
            <div
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                padding: "4px 8px",
                borderRadius: 8,
                border: "1px solid #cfe4d9",
                background: "linear-gradient(135deg, #f2faf5 0%, #e9f7ef 100%)",
                color: "#2f7a55",
                boxShadow: "rgba(47,122,85,0.06) 0px 2px 6px",
              }}
            >
              <Users size={12} />
              <span style={{ fontSize: 11, color: "#4c8669" }}>用户</span>
              <span style={{ fontSize: 12, fontWeight: 600, fontVariantNumeric: "tabular-nums" }}>
                {statsStatus === "ready" ? formatMetricValue(skill.user_count) : "—"}
              </span>
            </div>
          </div>
        </div>
      </div>

      {/* Footer: metadata + actions */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          flexWrap: "wrap",
          gap: 8,
          paddingTop: 12,
          borderTop: "1px solid #f0eee6",
        }}
      >
        <div style={{ display: "flex", flexWrap: "wrap", gap: 12, fontSize: 12, color: "#87867f" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
            <Calendar size={12} />
            <span>{skill.created_at ? new Date(skill.created_at).toLocaleDateString("zh-CN") : "-"}</span>
          </div>
          {skill.version && (
            <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
              <GitBranch size={12} />
              <span>v{skill.version}</span>
            </div>
          )}
          {skill.creator_name && (
            <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
              <Users size={12} />
              <span>{skill.creator_name}</span>
            </div>
          )}
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 6 }} onClick={(e) => e.stopPropagation()}>
          <Button
            size="small"
            onClick={onClick}
            style={{
              height: 28,
              padding: "0 12px",
              fontSize: 12,
              color: "#5e5d59",
              border: "1px solid #e8e6dc",
              backgroundColor: "#f5f4ed",
              borderRadius: 8,
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
            }}
          >
            <Eye size={12} />
            详情
          </Button>
          {isManager && onDistribute && (
            <Button
              size="small"
              type="primary"
              onClick={onDistribute}
              style={{
                height: 28,
                padding: "0 12px",
                fontSize: 12,
                borderRadius: 8,
                display: "inline-flex",
                alignItems: "center",
                gap: 4,
              }}
            >
              <Send size={12} />
              分发
            </Button>
          )}
          {isManager && managerMenuItems.length > 0 && (
            <Dropdown
              menu={{ items: managerMenuItems }}
              trigger={['click']}
            >
              <Button
                size="small"
                style={{
                  height: 28,
                  padding: "0 12px",
                  fontSize: 12,
                  borderRadius: 8,
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 4,
                }}
              >
                <MoreVertical size={12} />
                管理
              </Button>
            </Dropdown>
          )}
        </div>
      </div>
    </div>
  );
}
