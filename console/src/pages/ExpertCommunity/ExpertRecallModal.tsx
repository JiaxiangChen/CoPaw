import { useEffect, useMemo, useState } from "react";
import { Alert, Modal, Spin, message } from "antd";
import { marketApi, type ExpertRecallResponse } from "../../api/modules/market";
import type { DistributionRecord } from "../../api/types";
import type { TenantSourceInfo } from "../../api/modules/userInfo";
import { TenantSelector } from "../../components/TenantSelector";

interface ExpertRecallModalProps {
  open: boolean;
  sourceId: string;
  itemId: string;
  itemName: string;
  onClose: () => void;
  onSuccess: () => void;
}

export function ExpertRecallModal({
  open,
  sourceId,
  itemId,
  itemName,
  onClose,
  onSuccess,
}: ExpertRecallModalProps) {
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [holders, setHolders] = useState<DistributionRecord[]>([]);
  const [selectedTenantIds, setSelectedTenantIds] = useState<string[]>([]);

  useEffect(() => {
    if (!open) return;
    let active = true;
    setLoading(true);
    setSelectedTenantIds([]);
    marketApi
      .getExpertDistributions(sourceId, itemId)
      .then((result) => {
        if (active) setHolders(result);
      })
      .catch((error) => {
        if (!active) return;
        message.error(
          error instanceof Error ? error.message : "获取持有用户失败",
        );
        setHolders([]);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [itemId, open, sourceId]);

  const holderIds = useMemo(
    () => Array.from(new Set(holders.map((holder) => holder.target_user_id))),
    [holders],
  );

  const holderOptions = useMemo<TenantSourceInfo[]>(
    () =>
      holders.map((holder) => ({
        tenant_id: holder.target_user_id,
        tenant_name: holder.target_user_name || null,
        bbk_id: holder.target_bbk_id || null,
      })),
    [holders],
  );

  const handleSubmit = async () => {
    if (selectedTenantIds.length === 0) return;
    setSubmitting(true);
    try {
      const result: ExpertRecallResponse = await marketApi.recallExpert(
        sourceId,
        itemId,
        selectedTenantIds,
      );
      const failed = result.results.filter((entry) => !entry.success);
      if (failed.length > 0) {
        Modal.info({
          title: result.recalled_count > 0 ? "部分撤回成功" : "撤回未生效",
          content: (
            <div style={{ display: "grid", gap: 8 }}>
              <div>成功撤回 {result.recalled_count} 个用户</div>
              <div>以下 {failed.length} 个用户撤回失败：</div>
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
        message.success(`已撤回 ${result.recalled_count} 个用户的专家`);
      }
      onSuccess();
      onClose();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "撤回失败");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      open={open}
      title={`撤回「${itemName}」`}
      onCancel={submitting ? undefined : onClose}
      onOk={() => void handleSubmit()}
      okText="确认撤回"
      cancelText="取消"
      okButtonProps={{
        disabled: selectedTenantIds.length === 0,
        loading: submitting,
        danger: true,
      }}
      width={720}
    >
      <div style={{ display: "grid", gap: 12 }}>
        <Alert
          type="warning"
          showIcon
          message="仅显示当前实际持有该专家的用户；用户主动接收和管理员分发的副本均包含在内。"
        />
        {loading ? (
          <Spin />
        ) : holderIds.length === 0 ? (
          <Alert type="info" message="暂无用户持有该专家。" />
        ) : (
          <TenantSelector
            selectedTenantIds={selectedTenantIds}
            onChange={setSelectedTenantIds}
            allowedTenantIds={holderIds}
            additionalTenantOptions={holderOptions}
            allowManualIds={false}
            hint={`当前持有用户：${holderIds.length} 个`}
          />
        )}
      </div>
    </Modal>
  );
}
