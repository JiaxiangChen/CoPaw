# -*- coding: utf-8 -*-
"""只准备执行资源，不操作任务文件、旧状态缓存或调度平台。"""

import json
import logging
from uuid import NAMESPACE_URL, uuid5

from .models import CronJobSpec
from .managed_model_preparation import resolve_broadcast_model

logger = logging.getLogger(__name__)


def stable_chat_id(spec, agent_id):
    identity = [spec.tenant_id, spec.source_id, agent_id, spec.id]
    return str(
        uuid5(NAMESPACE_URL, "managed-cron-chat:" + json.dumps(identity)),
    )


async def validate_configured_model(spec):
    if spec.model_slot is None:
        return
    from ...config.context import resolve_storage_tenant_id
    from ...providers.provider_manager import ProviderManager

    tenant = resolve_storage_tenant_id(spec.tenant_id, spec.source_id)
    ProviderManager.ensure_tenant_provider_storage(tenant)
    manager = ProviderManager.get_instance(tenant)
    provider = manager.get_provider(spec.model_slot.provider_id)
    if provider is None or not provider.has_model(spec.model_slot.model):
        raise ValueError("cron_model_not_found")


class ResourcePreparer:
    def __init__(
        self,
        chats,
        *,
        agent_id,
        validate_model=validate_configured_model,
        workflow_store=None,
        broadcast_model=resolve_broadcast_model,
    ):
        self.chats = chats
        self.agent_id = agent_id
        self.validate_model = validate_model
        self.workflow_store = workflow_store
        self.broadcast_model = broadcast_model

    async def prepare(self, raw_spec, binding):
        spec = CronJobSpec.model_validate(raw_spec)
        logger.info(
            "开始准备新任务执行资源 job_id=%s task_type=%s agent_id=%s",
            spec.id,
            spec.task_type,
            self.agent_id,
        )
        policy = json.loads(binding.get("payload_json") or "{}").get(
            "preparation_policy",
        )
        spec, warning = await self._configuration(spec, policy)
        session = binding.get("task_session_id") or f"cron-task:{spec.id}"
        owner = str(spec.meta.get("creator_user_id") or "")
        if not owner:
            raise ValueError("cron_resource_owner_missing")
        chat_id = binding.get("task_chat_id") or stable_chat_id(
            spec,
            self.agent_id,
        )
        chat = await self.chats.get_chat(chat_id)
        if chat is not None:
            self._verify(chat, spec, session, owner)
            chat.name = spec.name
            await self.chats.update_chat(chat)
        else:
            chat = await self._create_chat(chat_id, spec, session, owner)
        meta = {
            **spec.meta,
            "task_chat_id": chat.id,
            "task_session_id": session,
        }
        request = spec.request
        if request is not None:
            request = request.model_copy(
                update={"user_id": owner, "session_id": session},
            )
        dispatch = spec.dispatch.model_copy(
            update={
                "target": spec.dispatch.target.model_copy(
                    update={"user_id": owner, "session_id": session},
                ),
            },
        )
        prepared = spec.model_copy(
            update={"meta": meta, "request": request, "dispatch": dispatch},
        )
        logger.info(
            "新任务执行资源准备完成 job_id=%s chat_id=%s",
            spec.id,
            chat.id,
        )
        return {
            "spec": prepared.model_dump(mode="json"),
            "chat_id": chat.id,
            "session_id": session,
            "warning": warning,
        }

    async def _configuration(self, spec, policy):
        warning = None
        if spec.task_type == "agent":
            if policy == "broadcast":
                spec, warning = await self.broadcast_model(spec)
            else:
                await self.validate_model(spec)
        if spec.task_type != "workflow":
            return spec, warning
        if self.workflow_store is None:
            raise RuntimeError("workflow_configuration_unavailable")
        if spec.skill_ids:
            config = await self.workflow_store.resolve_first_skill(
                spec.source_id,
                spec.skill_ids,
            )
        else:
            config = await self.workflow_store.get_current(
                spec.workflow_binding_id,
                source_id=spec.source_id,
            )
        if (
            spec.workflow_binding_id
            and spec.workflow_binding_id != config.binding_id
        ):
            raise ValueError("workflow_binding_conflict")
        prepared = spec.model_copy(
            update={"workflow_binding_id": config.binding_id},
        )
        return prepared, warning

    def _route(self, spec, owner):
        return {
            "session_kind": "task",
            "management_owner": "scheduler",
            "task_job_id": spec.id,
            "creator_user_id": owner,
            "task_tenant_id": spec.tenant_id or "",
            "task_source_id": spec.source_id or "",
            "task_scope_id": spec.scope_id or "",
            "task_agent_id": self.agent_id,
        }

    def _verify(self, chat, spec, session, owner):
        expected = self._route(spec, owner)
        if chat.user_id != owner or chat.session_id != session:
            raise ValueError("cron_resource_binding_conflict")
        if any(
            (chat.meta or {}).get(key) != value
            for key, value in expected.items()
        ):
            raise ValueError("cron_resource_binding_conflict")

    async def _create_chat(self, chat_id, spec, session, owner):
        from ..runner.models import ChatSpec

        return await self.chats.create_chat(
            ChatSpec(
                id=chat_id,
                name=spec.name,
                session_id=session,
                user_id=owner,
                meta=self._route(spec, owner),
            ),
        )

    async def cleanup(self, raw_spec, binding):
        spec = CronJobSpec.model_validate(raw_spec)
        logger.info(
            "开始清理新任务执行资源 job_id=%s agent_id=%s",
            spec.id,
            self.agent_id,
        )
        session = binding.get("task_session_id") or f"cron-task:{spec.id}"
        owner = str(spec.meta.get("creator_user_id") or "")
        chat_id = binding.get("task_chat_id") or stable_chat_id(
            spec,
            self.agent_id,
        )
        chat = await self.chats.get_chat(chat_id)
        if chat is None:
            logger.info("待清理的新任务资源已不存在 job_id=%s", spec.id)
            return {"cleaned": True}
        self._verify(chat, spec, session, owner)
        if not await self.chats.delete_chats([chat_id]):
            raise RuntimeError("cron_resource_cleanup_failed")
        logger.info("新任务执行资源清理完成 job_id=%s", spec.id)
        return {"cleaned": True}
