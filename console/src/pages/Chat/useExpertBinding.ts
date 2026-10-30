import { useCallback, useEffect, useRef, useState } from "react";
import { chatApi } from "@/api/modules/chat";
import type {
  ExpertBindingConflict,
  ExpertBindingView,
} from "@/api/types/chat";

export const EXPERT_BINDING_MESSAGES = {
  expert_binding_changed:
    "会话专家已在其他页面变更，已同步最新身份，请确认后重新发送。文本和附件已保留。",
  expert_main_disabled:
    "专家主执行模式已关闭，已解除绑定。文本和附件已保留，请重新发送。",
  expert_unavailable:
    "当前绑定专家不可用，请选择其他专家或默认 Agent。文本和附件已保留。",
};

export function readExpertBindingConflict(
  payload: unknown,
): ExpertBindingConflict | null {
  if (!payload || typeof payload !== "object") return null;
  const detail = (payload as { detail?: unknown }).detail;
  if (!detail || typeof detail !== "object") return null;
  const conflict = detail as ExpertBindingConflict;
  const binding = conflict.expert_binding;
  return Object.prototype.hasOwnProperty.call(
    EXPERT_BINDING_MESSAGES,
    conflict.code,
  ) &&
    binding &&
    typeof binding.revision === "number" &&
    typeof binding.enabled === "boolean"
    ? conflict
    : null;
}

type BindingOwner = {
  key: string;
  chatId: string | null;
  view: ExpertBindingView | null;
  loading: boolean;
  saving: boolean;
};
type BindingChangeResult = "saved" | "legacy" | "failed" | "busy" | "conflict";

export function useExpertBinding(options: {
  chatId: string | null;
  scopeKey: string;
  createChat: (expertId: string) => Promise<{
    id: string;
    expert_binding: ExpertBindingView | null;
  }>;
  notify: (message: string) => void;
}) {
  const { chatId, scopeKey } = options;
  const key = JSON.stringify([scopeKey, chatId]);
  const ownerRef = useRef<BindingOwner>({
    key,
    chatId,
    view: null,
    loading: Boolean(chatId),
    saving: false,
  });
  if (ownerRef.current.key !== key) {
    ownerRef.current = {
      key,
      chatId,
      view: null,
      loading: Boolean(chatId),
      saving: false,
    };
  }
  const [, rerender] = useState(0);
  const optionsRef = useRef(options);
  optionsRef.current = options;
  const publish = useCallback((owner: BindingOwner) => {
    if (ownerRef.current === owner) rerender((value) => value + 1);
  }, []);
  const syncConflict = useCallback(
    (payload: unknown, targetChatId: string | null) => {
      const conflict = readExpertBindingConflict(payload);
      const owner = ownerRef.current;
      if (!conflict || owner.chatId !== targetChatId) return false;
      owner.view = conflict.expert_binding;
      publish(owner);
      optionsRef.current.notify(
        conflict.expert_binding.reason === "expert_execution_unavailable"
          ? "原审批执行快照已不可恢复，请重新发起任务。文本和附件已保留。"
          : EXPERT_BINDING_MESSAGES[conflict.code],
      );
      return true;
    },
    [publish],
  );

  useEffect(() => {
    const owner = ownerRef.current;
    let cancelled = false;
    if (!chatId) return;
    chatApi
      .getExpertBinding(chatId)
      .then((view) => {
        if (cancelled || ownerRef.current !== owner) return;
        if (
          !owner.saving &&
          (!owner.view || view.revision >= owner.view.revision)
        )
          owner.view = view;
        if (view.reason === "expert_main_disabled")
          optionsRef.current.notify(
            EXPERT_BINDING_MESSAGES.expert_main_disabled,
          );
      })
      .catch((error: unknown) => {
        if (!cancelled && ownerRef.current === owner)
          optionsRef.current.notify(
            error instanceof Error ? error.message : "加载会话专家失败，请重试",
          );
      })
      .finally(() => {
        if (!cancelled) {
          owner.loading = false;
          publish(owner);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [key, chatId, publish]);

  const change = useCallback(
    async (expertId: string | null): Promise<BindingChangeResult> => {
      const owner = ownerRef.current;
      if (owner.saving || owner.loading) return "busy";
      if (owner.view?.enabled === false) return "legacy";
      if (!owner.chatId && !expertId) return "legacy";
      if (owner.view && owner.view.expert_id === expertId) return "saved";
      owner.saving = true;
      publish(owner);
      try {
        if (!owner.chatId && expertId) {
          const created = await optionsRef.current.createChat(expertId);
          if (ownerRef.current !== owner) return "failed";
          if (!created.expert_binding)
            throw new Error("创建会话未返回专家状态，请重试");
          owner.chatId = created.id;
          owner.view = created.expert_binding;
          return "saved";
        }
        if (!owner.chatId || ownerRef.current !== owner) return "failed";
        if (!owner.view)
          owner.view = await chatApi.getExpertBinding(owner.chatId);
        if (ownerRef.current !== owner) return "failed";
        if (!owner.view.enabled) return "legacy";
        const view = await chatApi.updateExpertBinding(owner.chatId, {
          expected_revision: owner.view.revision,
          expert_id: expertId,
        });
        if (ownerRef.current !== owner) return "failed";
        owner.view = view;
        return "saved";
      } catch (error) {
        const conflict = readExpertBindingConflict(
          (error as { data?: unknown })?.data,
        );
        if (
          ownerRef.current === owner &&
          !owner.chatId &&
          conflict?.code === "expert_main_disabled"
        ) {
          owner.view = conflict.expert_binding;
          return "legacy";
        }
        if (ownerRef.current === owner && !owner.chatId && conflict?.chat_id) {
          owner.chatId = conflict.chat_id;
          syncConflict((error as { data?: unknown })?.data, owner.chatId);
          return "conflict";
        }
        if (
          ownerRef.current === owner &&
          (!owner.chatId ||
            !syncConflict((error as { data?: unknown })?.data, owner.chatId))
        ) {
          optionsRef.current.notify(
            error instanceof Error ? error.message : "保存会话专家失败，请重试",
          );
        }
        return "failed";
      } finally {
        owner.saving = false;
        publish(owner);
      }
    },
    [publish, syncConflict],
  );

  const refresh = useCallback(async () => {
    const owner = ownerRef.current;
    if (!owner.chatId) return true;
    try {
      const view = await chatApi.getExpertBinding(owner.chatId);
      if (ownerRef.current !== owner) return false;
      if (
        !owner.saving &&
        (!owner.view || view.revision >= owner.view.revision)
      )
        owner.view = view;
      owner.loading = false;
      publish(owner);
      return true;
    } catch {
      optionsRef.current.notify("加载会话专家失败，请重试");
      return false;
    }
  }, [publish]);

  const getSnapshot = useCallback(() => ownerRef.current, []);
  const owner = ownerRef.current;
  return {
    view: owner.view,
    loading: owner.loading,
    saving: owner.saving,
    change,
    syncConflict,
    getSnapshot,
    refresh,
  };
}
