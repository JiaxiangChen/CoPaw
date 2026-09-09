/**
 * 通用分发目标弹窗。
 *
 * 支持技能、MCP 和专家分发，统一交互和布局。
 */
import { useEffect, useMemo, useState } from "react";
import { Modal, message } from "antd";
import { marketApi, DistributeRequest } from "../../api/modules/market";
import type {
  UserSkillStatus,
  DistributionPreviewResponse,
  MarketSkill,
  MarketExpert,
} from "../../api/modules/market";
import { marketMcpApi } from "../../api/modules/marketMcp";
import { TenantSelector } from "../../components/TenantSelector";
import { DistributionPreview } from "../../components/DistributionPreview";
import { fetchTenantsBySource } from "../../api/modules/userInfo";
import type { MarketMCPItem } from "../../api/types";
import type { TargetMode } from "../../components/TenantSelector/types";

export type DistributeTargetType = "skill" | "mcp" | "expert";

interface DistributeTargetModalProps {
  open: boolean;
  type: DistributeTargetType;
  item: MarketSkill | MarketMCPItem | MarketExpert | null;
  sourceId: string;
  onClose: () => void;
  onSuccess: () => void;
}

export function DistributeTargetModal({
  open,
  type,
  item,
  sourceId,
  onClose,
  onSuccess,
}: DistributeTargetModalProps) {
  const [submitting, setSubmitting] = useState(false);
  const [selectedTenantIds, setSelectedTenantIds] = useState<string[]>([]);
  // 当前选择模式（从 TenantSelector 获取）
  const [, setTargetMode] = useState<TargetMode>("bbk_id");
  // 用于触发 TenantSelector 选择已分发用户/机构
  const [distributedUserIdsToSelect, setDistributedUserIdsToSelect] = useState<
    string[]
  >([]);
  // 触发计数器：每次勾选 checkbox 都增加，让 TenantSelector 能检测到变化
  const [triggerCount, setTriggerCount] = useState(0);

  // 预览状态
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewData, setPreviewData] =
    useState<DistributionPreviewResponse | null>(null);
  // 是否已获取过预览（防止重复请求）
  const [hasFetchedPreview, setHasFetchedPreview] = useState(false);

  // 用户技能状态映射
  const userSkillStatusMap = useMemo(() => {
    if (!previewData) return new Map<string, UserSkillStatus>();
    return new Map(previewData.users.map((u) => [u.tenant_id, u]));
  }, [previewData]);

  // 打开时清空选择和预览状态
  useEffect(() => {
    if (!open) return;
    setSelectedTenantIds([]);
    setPreviewData(null);
    setHasFetchedPreview(false);
    setDistributedUserIdsToSelect([]);
    setTriggerCount(0);
  }, [open]);

  // 当弹窗打开且有 item 时，获取预览数据（仅请求一次）
  useEffect(() => {
    if (!open || !item || hasFetchedPreview) return;
    if (type !== "skill") return; // 仅技能分发需要预览

    setHasFetchedPreview(true);
    const fetchPreview = async () => {
      setPreviewLoading(true);
      try {
        // 获取租户列表
        const tenants = await fetchTenantsBySource(sourceId);
        const tenantIds = tenants.map((t) => t.tenant_id);

        // 获取预览数据
        const preview = await marketApi.getDistributionPreview(
          sourceId,
          (item as MarketSkill).item_id,
          tenantIds,
        );
        setPreviewData(preview);
      } catch (error) {
        console.error("获取预览失败:", error);
      } finally {
        setPreviewLoading(false);
      }
    };

    fetchPreview();
  }, [open, item, sourceId, type, hasFetchedPreview]);

  // 处理"选中已分发用户"（根据模式选择机构或用户）
  const handleSelectDistributed = (distributedIds: string[]) => {
    if (distributedIds.length === 0) {
      // 取消勾选：清空选择和计数器
      setDistributedUserIdsToSelect([]);
      setTriggerCount(0);
      return;
    }

    // 勾选：设置 distributedIds 并增加计数器触发设置
    setDistributedUserIdsToSelect(distributedIds);
    setTriggerCount((c) => c + 1);
  };

  // 提交分发
  const handleSubmit = async () => {
    if (!item || selectedTenantIds.length === 0) return;
    setSubmitting(true);
    try {
      if (type === "skill") {
        const payload: DistributeRequest = {
          target_type: "user_id",
          target_values: selectedTenantIds,
        };
        const result = await marketApi.distributeSkill(
          sourceId,
          (item as MarketSkill).item_id,
          payload,
        );
        message.success(`技能分发任务已提交：${result.task_id}`);
      } else if (type === "mcp") {
        const result = await marketMcpApi.distributeMCP(
          (item as MarketMCPItem).item_id,
          {
            target_tenant_ids: selectedTenantIds,
            overwrite: true,
          },
        );
        message.success(`MCP 分发任务已提交：${result.task_id}`);
      } else {
        const result = await marketApi.distributeExpert(
          sourceId,
          (item as MarketExpert).item_id,
          { target_type: "user_id", target_values: selectedTenantIds },
        );
        const failed = result.results.filter((entry) => !entry.success);
        if (failed.length > 0) {
          Modal.info({
            title: result.distributed_count > 0 ? "部分分发成功" : "分发未生效",
            content: (
              <div style={{ display: "grid", gap: 8 }}>
                <div>成功分发 {result.distributed_count} 个用户</div>
                <div>以下 {failed.length} 个用户分发失败：</div>
                <pre style={{ margin: 0, whiteSpace: "pre-wrap" }}>
                  {failed
                    .map(
                      (entry) =>
                        `${entry.user_id}（${entry.reason || "未知原因"}）`,
                    )
                    .join("\n")}
                </pre>
              </div>
            ),
            okText: "关闭",
          });
        } else {
          message.success(`已分发 ${result.distributed_count} 个用户`);
        }
      }
      onSuccess();
      onClose();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "分发失败");
    } finally {
      setSubmitting(false);
    }
  };

  const hintText =
    type === "skill"
      ? "将当前技能分发到目标用户的工作空间中，用户可在「我的技能」中查看。"
      : type === "mcp"
      ? "将当前市场 MCP 分发到目标租户的 default agent 中，如已存在同名 MCP 将覆盖。"
      : "将当前专家分发到目标用户的 default agent 中。";

  return (
    <Modal
      open={open}
      title={`分发「${item?.name || ""}」`}
      onCancel={submitting ? undefined : onClose}
      onOk={handleSubmit}
      okText="分发"
      cancelText="取消"
      okButtonProps={{
        disabled: selectedTenantIds.length === 0,
        loading: submitting,
      }}
      width={720}
    >
      <div style={{ display: "grid", gap: 12 }}>
        {/* 分发预览卡片 */}
        {type === "skill" && previewData && (
          <DistributionPreview
            skillVersion={previewData.skill_version}
            users={previewData.users}
            distributedUserIds={previewData.distributed_user_ids}
            selectedTenantIds={selectedTenantIds}
            loading={previewLoading}
            onSelectDistributed={handleSelectDistributed}
          />
        )}

        <div style={{ color: "#666", fontSize: 12 }}>{hintText}</div>
        <div style={{ fontWeight: 500 }}>
          当前条目：{item?.name || "-"}（共选择 {selectedTenantIds.length}{" "}
          个用户）
        </div>
        <TenantSelector
          selectedTenantIds={selectedTenantIds}
          onChange={setSelectedTenantIds}
          userSkillStatusMap={userSkillStatusMap}
          skillVersion={previewData?.skill_version}
          onTargetModeChange={setTargetMode}
          distributedUserIds={distributedUserIdsToSelect}
          distributedTriggerKey={triggerCount}
        />
      </div>
    </Modal>
  );
}
