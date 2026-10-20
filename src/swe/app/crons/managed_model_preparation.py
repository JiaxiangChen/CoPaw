# -*- coding: utf-8 -*-
"""分发时在接收方校验模型，必要时沿用原规则回退接收方默认模型。"""


def get_model_manager(spec):
    from ...config.context import resolve_storage_tenant_id
    from ...providers.provider_manager import ProviderManager

    tenant = resolve_storage_tenant_id(spec.tenant_id, spec.source_id)
    ProviderManager.ensure_tenant_provider_storage(tenant)
    return ProviderManager.get_instance(tenant)


def _available(manager, slot):
    if slot is None:
        return False
    provider = manager.get_provider(slot.provider_id)
    return provider is not None and provider.has_model(slot.model)


async def resolve_broadcast_model(spec):
    if spec.task_type != "agent":
        return spec, None
    manager = get_model_manager(spec)
    requested = spec.model_slot
    if _available(manager, requested):
        return spec, None
    if not _available(manager, manager.get_active_model()):
        raise ValueError("cron_default_model_unavailable")
    if requested is None:
        return spec, None
    reason = (
        "provider_not_found"
        if manager.get_provider(requested.provider_id) is None
        else "model_not_found"
    )
    meta = {
        **spec.meta,
        "broadcast_original_model_slot": requested.model_dump(mode="json"),
        "broadcast_model_slot_fallback_reason": reason,
    }
    return (
        spec.model_copy(update={"model_slot": None, "meta": meta}),
        "broadcast_model_fallback",
    )
