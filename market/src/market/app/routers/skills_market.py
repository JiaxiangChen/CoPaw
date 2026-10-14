# -*- coding: utf-8 -*-
"""管理员市场 API."""

import asyncio
import io
import json
import logging
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TypedDict

from fastapi import (
    APIRouter,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field

from ...marketplace.fs import (
    _atomic_write_json,
    default_workspace_skill_manifest,
    get_skill_dir,
    get_workspace_skill_manifest_path,
)
from ...marketplace.schemas import (
    AsyncTaskSubmitResponse,
    DistributeRequest,
    DistributeResponse,
    DistributionPreviewRequest,
    DistributionPreviewResponse,
    MarketSkillResponse,
    PublishSkillRequest,
    UploadSkillResponse,
    UserSkillStatus,
)
from ...marketplace.service import (
    MarketItem,
    SkillNameConflictError,
    SkillVersionConflictError,
    load_index,
    save_index,
)
from ...marketplace.errors import MarketplaceMetadataSyncError
from ...marketplace.version_service import SkillVersionService
from ...security import SkillScanError
from ..async_tasks import AsyncTaskStore
from ..deps import decode_user_name, require_source_id
from ...utils.logging_utils import log_params
from .skills_browse import (
    _decode_zip_filename,
    _extract_zip_skills,
    _flush_skill_scan_history,
    _read_validated_zip_upload,
    _scan_found_skills_or_raise,
)

router = APIRouter()
logger = logging.getLogger(__name__)


class _InitUserSkillsResult(TypedDict):
    """init_user_skills 返回结果类型."""

    dry_run: bool
    processed_users: int
    processed_workspaces: int
    processed_skills: int
    created_skill_json: int
    updated_source: int
    skipped_marketplace: int
    errors: list[dict[str, str]]
    details: list[dict[str, str]]


def _require_manager(x_manager: Optional[str]) -> None:
    """验证管理员权限."""
    if x_manager != "true":
        raise HTTPException(status_code=403, detail="Manager access required")


def _parse_skill_metadata(
    skill_dir: Path,
    skill_name: str,
) -> tuple[dict, str, str, str, str]:
    """解析技能元数据.

    Returns:
        (skill_json, skill_md, name, description, version)
    """
    skill_json_path = skill_dir / "skill.json"
    skill_md_path = skill_dir / "SKILL.md"

    skill_json = {}
    skill_md = ""
    name_from_skill = skill_name
    description_from_skill = ""
    version_from_skill = ""

    # 读取 skill.json
    if skill_json_path.exists():
        try:
            skill_json = json.loads(
                skill_json_path.read_text(encoding="utf-8"),
            )
            name_from_skill = skill_json.get("name", skill_name)
            description_from_skill = skill_json.get("description", "")
            version_from_skill = skill_json.get("version", "")
        except json.JSONDecodeError:
            pass

    # 读取 SKILL.md 并解析 frontmatter
    if skill_md_path.exists():
        skill_md = skill_md_path.read_text(encoding="utf-8")
        name_from_skill, description_from_skill, version_from_md = (
            _parse_frontmatter(
                skill_md,
                name_from_skill,
                description_from_skill,
            )
        )
        # SKILL.md 中的 version 优先级更高（与版本历史对齐）
        if version_from_md:
            version_from_skill = version_from_md

    return (
        skill_json,
        skill_md,
        name_from_skill,
        description_from_skill,
        version_from_skill,
    )


def _parse_frontmatter(
    skill_md: str,
    default_name: str,
    default_desc: str,
) -> tuple[str, str, str]:
    """从 SKILL.md 解析 frontmatter（委托共享工具）.

    Returns:
        (name, description, version)
    """
    from ...utils.skill_md import extract_metadata

    meta = extract_metadata(skill_md)
    name = meta["name"] or default_name
    desc = meta["description"] or default_desc
    version = meta["version"]
    return name, desc, version


def _copy_skill_to_market(
    skill_dir: Path,
    market_skill_dir: Path,
    skill_json: dict,  # noqa: ARG001 - 保留参数签名兼容，但不再写入
    skill_md: str,
) -> None:
    """复制技能文件到市场目录.

    覆盖时先清空目标目录，确保旧文件不会残留。
    不再写入 skill.json，元数据从 SKILL.md frontmatter 读取。
    """
    market_skill_dir.mkdir(parents=True, exist_ok=True)

    # 清空目标目录中的旧文件（覆盖场景）
    for existing in market_skill_dir.iterdir():
        if existing.is_dir():
            shutil.rmtree(existing)
        else:
            existing.unlink()

    # 复制 SKILL.md（newline="" 防止 Windows 上 write_text 把 LF 转为 CRLF，
    # 导致与 copytree 路径写入的文件签名不一致，R7 no-op 跨路径失效）
    if skill_md:
        (market_skill_dir / "SKILL.md").write_text(
            skill_md,
            encoding="utf-8",
            newline="",
        )

    # 复制其他文件（排除 skill.json）
    for f in skill_dir.iterdir():
        if f.name not in ("skill.json", "SKILL.md"):
            target = market_skill_dir / f.name
            if f.is_dir():
                shutil.copytree(f, target)
            else:
                shutil.copy2(f, target)


async def _log_publish_operation(
    svc,
    source_id: str,
    user_id: str,
    user_name: str,
    item: MarketItem,
) -> None:
    """记录上架操作日志."""
    if not svc.db.is_connected:
        return

    try:
        await svc.db.execute(
            """
            INSERT INTO swe_marketplace_operation_logs
                (source_id, operator_id, operator_name, operation,
                 item_type, item_id, item_name,
                 target_user_id, target_user_name, target_bbk_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                source_id,
                user_id,
                user_name,
                "publish",
                "skill",
                item.item_id,
                item.name,
                None,
                None,
                None,
            ),
        )
    except Exception as e:
        logger.warning("Failed to log publish operation: %s", e)


def _new_async_task_id() -> str:
    """生成异步任务 ID。"""
    return str(uuid.uuid4())


def _get_async_task_store(request: Request) -> AsyncTaskStore:
    """创建 Market 异步任务写入器。"""
    db = request.app.state.marketplace.db
    if db is None or not getattr(db, "is_connected", False):
        raise HTTPException(
            status_code=503,
            detail="Async task database connection is not available",
        )
    return AsyncTaskStore(db)


def _distribution_summary(kind: str, name: str, target_count: int) -> str:
    """构造包含分发对象的任务摘要。"""
    object_name = str(name or "").strip() or "-"
    return f"分发{kind}「{object_name}」，目标 {target_count} 个用户"


def _find_market_skill_item(
    svc,
    source_id: str,
    item_ref: str,
) -> MarketItem | None:
    """按 item_id、skill_id 或名称解析市场技能条目。"""
    items = load_index(svc.marketplace_root, source_id)
    return next(
        (
            candidate
            for candidate in items
            if candidate.item_type == "skill"
            and item_ref
            in {
                candidate.item_id,
                candidate.skill_id,
                candidate.name,
            }
        ),
        None,
    )


async def _run_skill_distribution_task(
    *,
    task_id: str,
    store: AsyncTaskStore,
    svc,
    source_id: str,
    item_id: str,
    operator_id: str,
    operator_name: str,
    req: DistributeRequest,
    target_user_ids: list[str],
) -> None:
    """后台执行技能分发并写回任务表。"""
    try:
        await store.mark_running(task_id)
        result = await svc.distribute_skill(
            source_id,
            item_id,
            operator_id=operator_id,
            operator_name=operator_name,
            req=req,
        )
        result_map = {
            item.user_id: item
            for item in getattr(result, "results", [])
            if getattr(item, "user_id", "")
        }
        conflict_ids = {item.user_id for item in result.conflicts}
        succeeded_count = 0
        failed_count = 0
        for user_id in target_user_ids:
            item_result = result_map.get(user_id)
            if item_result is not None:
                if item_result.success:
                    succeeded_count += 1
                else:
                    failed_count += 1
                await store.record_item_result(
                    task_id=task_id,
                    target_id=user_id,
                    success=bool(item_result.success),
                    item_status=(
                        "succeeded" if item_result.success else "failed"
                    ),
                    error_message=item_result.error,
                    result={
                        "item_id": item_id,
                        "status": item_result.status,
                        "error": item_result.error,
                    },
                )
            elif user_id in conflict_ids:
                failed_count += 1
                conflict_reason = next(
                    item.reason
                    for item in result.conflicts
                    if item.user_id == user_id
                )
                await store.record_item_result(
                    task_id=task_id,
                    target_id=user_id,
                    success=False,
                    item_status="failed",
                    error_message=conflict_reason,
                    result={
                        "item_id": item_id,
                        "status": "conflict",
                        "error": conflict_reason,
                    },
                )
            else:
                failed_count += 1
                await store.record_item_result(
                    task_id=task_id,
                    target_id=user_id,
                    success=False,
                    item_status="failed",
                    error_message="distribution result missing",
                    result={
                        "item_id": item_id,
                        "status": "failed",
                        "error": "distribution result missing",
                    },
                )
        await store.finish_task(
            task_id=task_id,
            status=(
                "succeeded"
                if failed_count == 0
                else ("failed" if succeeded_count == 0 else "partial_failed")
            ),
            done_count=succeeded_count + failed_count,
            failed_count=failed_count,
            error_message=(None if failed_count == 0 else "部分目标分发失败"),
            result=result.model_dump(),
        )
    except Exception as exc:  # pylint: disable=broad-except
        for user_id in target_user_ids:
            try:
                await store.record_item_result(
                    task_id=task_id,
                    target_id=user_id,
                    success=False,
                    error_message=str(exc),
                )
            except Exception:
                logger.warning(
                    "Failed to record skill distribution item failure: task_id=%s user_id=%s",
                    task_id,
                    user_id,
                    exc_info=True,
                )
        try:
            await store.finish_task(
                task_id=task_id,
                status="failed",
                done_count=0,
                failed_count=len(target_user_ids),
                error_message=str(exc),
                result={
                    "item_id": item_id,
                    "status": "failed",
                    "error": str(exc),
                },
            )
        except Exception:
            logger.warning(
                "Failed to finish skill distribution task: task_id=%s",
                task_id,
                exc_info=True,
            )


def _create_market_item(
    name: str,
    chinese_name: str,
    description: str,
    version: str,
    user_id: str,
    user_name: str,
    category_id: Optional[int],
    skill_id: Optional[str] = None,
    bbk_ids: Optional[list[str]] = None,
) -> MarketItem:
    """创建市场条目."""
    now = datetime.now(timezone.utc).isoformat()
    return MarketItem(
        item_id=str(uuid.uuid4()),
        item_type="skill",
        name=name,
        skill_id=skill_id or "",
        chinese_name=chinese_name,
        description=description,
        version=version or "1.0.0",
        creator_id=user_id,
        creator_name=user_name,
        category_id=category_id,
        bbk_ids=bbk_ids or [],
        status="active",
        created_at=now,
        updated_at=now,
    )


def _resolve_skill_cn_name_and_id(
    skill_md: str,
    name: str,
    cn_name: str,
    user_id: str,
) -> tuple[str, str]:
    """解析技能的 cn_name 和 skill_id.

    Args:
        skill_md: SKILL.md 内容
        name: 技能名称
        cn_name: 用户输入的中文展示名
        user_id: 用户 ID

    Returns:
        (resolved_cn_name, resolved_skill_id)
    """
    from ...utils.skill_md import (
        extract_cn_name_from_title,
        extract_skill_id,
        parse_frontmatter,
    )

    # 解析 chinese_name：优先用户输入，其次 metadata.cn_name，再次一级标题
    resolved_cn_name = cn_name.strip() if cn_name else ""
    if not resolved_cn_name:
        fm = parse_frontmatter(skill_md) if skill_md else {}
        metadata = fm.get("metadata", {})
        if isinstance(metadata, dict):
            resolved_cn_name = metadata.get("cn_name", "") or ""
    if not resolved_cn_name and skill_md:
        resolved_cn_name = extract_cn_name_from_title(skill_md)
    if not resolved_cn_name:
        resolved_cn_name = name  # fallback 到技能名

    # 提取 skill_id（从 SKILL.md metadata.skill_id）
    resolved_skill_id = ""
    if skill_md:
        resolved_skill_id = extract_skill_id(
            skill_md,
            source="",  # 市场上传时 item_id 未生成，使用空 source
            skill_name=name,
            creator_id=user_id,
        )

    return resolved_cn_name, resolved_skill_id


def _update_existing_market_item(
    existing: MarketItem,
    description: str,
    cn_name: str,
    skill_id: str,
    user_id: str,
    user_name: str,
    category_id: Optional[int],
    bbk_ids: Optional[list[str]] = None,
) -> bool:
    """更新已有的市场条目.

    Returns:
        cn_name 是否发生变化
    """
    from ...marketplace.service import _bump_patch

    now = datetime.now(timezone.utc).isoformat()
    cn_name_changed = existing.chinese_name != cn_name
    existing.created_at = now
    existing.status = "active"
    existing.chinese_name = cn_name
    existing.description = description
    existing.version = _bump_patch(existing.version)
    existing.creator_id = user_id
    existing.creator_name = user_name
    existing.category_id = category_id
    existing.bbk_ids = bbk_ids or []
    existing.updated_at = now
    # 同名技能覆盖时，复用已有 skill_id（若已有）
    if existing.skill_id:
        skill_id = existing.skill_id
    elif not existing.skill_id and skill_id:
        existing.skill_id = skill_id
    return cn_name_changed


def _create_market_version_snapshot(
    svc,
    source_id: str,
    item: MarketItem,
    market_skill_dir: Path,
    user_id: str,
    user_name: str,
    cn_name_changed: bool,
) -> bool:
    """创建市场版本快照.

    Args:
        cn_name_changed: cn_name 是否发生变化

    Returns:
        version_unchanged 标志
    """
    version_svc = SkillVersionService(svc.marketplace_root)
    version_unchanged = False
    try:
        snapshot = version_svc.create_version_snapshot(
            source_id=source_id,
            item_id=item.item_id,
            skill_dir=market_skill_dir,
            description="",  # 去掉重复的版本号信息
            creator=user_id,
            creator_name=user_name,
            current_market_version=item.version,
            source_user_id="",
            source_user_name="",
            source_user_version="v0.0.0",
        )
        # F2：让 MarketItem.version 严格跟随快照的 version_id
        # F3：cn_name 变化时不应返回 version_unchanged，即使文件内容未变
        if snapshot.version_id and snapshot.version_id != item.version:
            version_unchanged = not cn_name_changed
            item.version = snapshot.version_id
        elif cn_name_changed:
            version_unchanged = False
    except Exception as e:
        logger.warning("Failed to create version snapshot: %s", e)
    return version_unchanged


def _process_skill_upload_single(
    skill_dir: Path,
    skill_name: str,
    svc,
    source_id: str,
    user_id: str,
    user_name: str,
    category_id: Optional[int],
    overwrite: bool = False,
    cn_name: Optional[str] = None,
    skill_id: Optional[str] = None,
    bbk_ids: Optional[list[str]] = None,
    include_in_statistics: bool = False,
) -> tuple[
    Optional[str],
    Optional[dict],
    Optional[str],
    str,
    bool,
    Optional[str],
    Optional[str],
]:
    """处理单个技能的上架逻辑.

    Args:
        overwrite: 是否覆盖同名技能，默认 False（返回冲突）
        cn_name: 用户输入的中文展示名
        skill_id: parse-zip 生成的 skill_id，前端传入确保一致性
        bbk_ids: 所属分行 ID 列表
        include_in_statistics: 是否纳入排行榜统计

    Returns:
        (imported_name, conflict_info, parsed_name_for_first, resolved_cn_name, version_unchanged, item_id)
    """
    skill_json, skill_md, name, description, version = _parse_skill_metadata(
        skill_dir,
        skill_name,
    )

    # 直接使用前端传入的 cn_name 和 skill_id（parse-zip 已解析）
    # 如果前端未传（向后兼容），则从 SKILL.md 解析
    resolved_cn_name = cn_name.strip() if cn_name else ""
    final_skill_id = skill_id.strip() if skill_id else ""

    # 向后兼容：前端未传时，从 SKILL.md 解析
    if not resolved_cn_name or not final_skill_id:
        parsed_cn_name, parsed_skill_id = _resolve_skill_cn_name_and_id(
            skill_md,
            name,
            resolved_cn_name,
            user_id,
        )
        if not resolved_cn_name:
            resolved_cn_name = parsed_cn_name
        if not final_skill_id:
            final_skill_id = parsed_skill_id

    # 检查市场是否已存在同名技能
    items = load_index(svc.marketplace_root, source_id)
    existing = next((i for i in items if i.name == name), None)

    # 未显式 overwrite 时返回冲突信息，由前端弹窗让用户确认
    if existing and not overwrite:
        conflict_info = {
            "skill_name": name,
            "suggested_name": name,
            "existing_creator_id": existing.creator_id,
            "existing_creator_name": existing.creator_name,
            "existing_version": existing.version,
        }
        return (
            None,
            conflict_info,
            name,
            resolved_cn_name,
            False,
            None,
            final_skill_id,
        )

    version_unchanged = False
    cn_name_changed = False

    if existing:
        # R4: 同名（已确认覆盖） → 续接到现有条目
        cn_name_changed = _update_existing_market_item(
            existing,
            description,
            resolved_cn_name,
            final_skill_id,
            user_id,
            user_name,
            category_id,
            bbk_ids,
        )
        existing.include_in_statistics = include_in_statistics
        item = existing
    else:
        # 创建新市场条目，市场首发版本固定为 1.0.0
        item = _create_market_item(
            name,
            resolved_cn_name,
            description,
            "",  # 让 _create_market_item 内部 fallback 到 1.0.0
            user_id,
            user_name,
            category_id,
            skill_id=final_skill_id,
            bbk_ids=bbk_ids or [],
        )
        item.include_in_statistics = include_in_statistics
        items.append(item)

    # 复制技能文件到市场目录
    market_skill_dir = get_skill_dir(
        svc.marketplace_root,
        source_id,
        item.item_id,
    )
    _copy_skill_to_market(skill_dir, market_skill_dir, skill_json, skill_md)

    # 创建版本快照
    version_unchanged = _create_market_version_snapshot(
        svc,
        source_id,
        item,
        market_skill_dir,
        user_id,
        user_name,
        cn_name_changed,
    )

    save_index(svc.marketplace_root, source_id, items)

    return (
        name,
        None,
        name,
        resolved_cn_name,
        version_unchanged,
        item.item_id,
        final_skill_id,
    )


async def _process_published_skill_record(
    skill_dir: Path,
    skill_name: str,
    imported_name: str,
    resolved_cn_name: str,
    svc,
    source_id: str,
    x_user_id: str,
    user_name: str,
    parsed_name: Optional[str],
    parsed_description: Optional[str],
    parsed_cn_name: Optional[str],
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """处理已发布技能的记录逻辑.

    Returns:
        (parsed_name, parsed_description, parsed_cn_name) 更新后的值
    """
    # 记录首次解析的名称和描述
    if parsed_name is None and imported_name:
        skill_json, skill_md, _, desc, _ = _parse_skill_metadata(
            skill_dir,
            skill_name,
        )
        parsed_name = imported_name
        parsed_description = desc

    # 记录首次解析的中文名
    if parsed_cn_name is None:
        parsed_cn_name = resolved_cn_name

    # 异步记录操作日志
    item = next(
        (
            i
            for i in load_index(svc.marketplace_root, source_id)
            if i.name == imported_name
        ),
        None,
    )
    if item:
        await _log_publish_operation(
            svc,
            source_id,
            x_user_id,
            user_name,
            item,
        )

    return parsed_name, parsed_description, parsed_cn_name


async def _sync_skill_to_market_db(
    svc,
    source_id: str,
    item_id: str | None,
    skill_id: str,
    imported_name: str,
    resolved_cn_name: str,
    category_id: Optional[int],
    bbk_ids: Optional[list[str]],
    include_in_statistics: bool,
    x_user_id: str,
    user_name: str,
) -> None:
    """同步技能到 swe_marketplace_skills 数据库表."""
    if not item_id:
        return
    if not (svc.db and svc.db.is_connected):
        raise MarketplaceMetadataSyncError("Database unavailable")
    success = await svc.market_skill_registry.upsert_market_skill(
        source_id=source_id,
        item_id=item_id,
        skill_id=skill_id,
        skill_name=imported_name,
        cn_name=resolved_cn_name,
        category_id=category_id,
        bbk_ids=bbk_ids,
        include_in_statistics=include_in_statistics,
        creator_id=x_user_id,
        creator_name=user_name,
        updator_id=x_user_id,
        updator_name=user_name,
    )
    if not success:
        raise MarketplaceMetadataSyncError(
            "Failed to synchronize market skill metadata",
        )


async def _process_skill_upload_batch(
    found_skills,
    svc,
    source_id: str,
    user_id: str,
    user_name: str,
    category_id: Optional[int],
    overwrite: bool,
    cn_name: str,
    skill_id: str,
    parsed_bbk_ids: list[str],
    include_in_statistics: bool,
) -> tuple[
    list[str],
    list[dict],
    Optional[str],
    Optional[str],
    Optional[str],
    bool,
    Optional[str],
]:
    imported: list[str] = []
    conflicts: list[dict] = []
    parsed_name = None
    parsed_description = None
    parsed_cn_name = None
    has_unchanged = False
    final_skill_id = None

    for skill_dir, skill_name in found_skills:
        (
            imported_name,
            conflict,
            _first_name,
            resolved_cn_name,
            version_unchanged,
            item_id,
            final_skill_id,
        ) = await asyncio.to_thread(
            _process_skill_upload_single,
            skill_dir,
            skill_name,
            svc,
            source_id,
            user_id,
            user_name,
            category_id,
            overwrite,
            cn_name,
            skill_id,
            parsed_bbk_ids,
            include_in_statistics,
        )

        if conflict:
            conflicts.append(conflict)
            continue

        if version_unchanged:
            has_unchanged = True

        if imported_name:
            imported.append(imported_name)
            parsed_name, parsed_description, parsed_cn_name = (
                await _process_published_skill_record(
                    skill_dir,
                    skill_name,
                    imported_name,
                    resolved_cn_name,
                    svc,
                    source_id,
                    user_id,
                    user_name,
                    parsed_name,
                    parsed_description,
                    parsed_cn_name,
                )
            )
            await _sync_skill_to_market_db(
                svc=svc,
                source_id=source_id,
                item_id=item_id,
                skill_id=skill_id,
                imported_name=imported_name,
                resolved_cn_name=resolved_cn_name,
                category_id=category_id,
                bbk_ids=parsed_bbk_ids,
                include_in_statistics=include_in_statistics,
                x_user_id=user_id,
                user_name=user_name,
            )

    return (
        imported,
        conflicts,
        parsed_name,
        parsed_description,
        parsed_cn_name,
        has_unchanged,
        final_skill_id,
    )


@router.post(
    "/market/skills/publish-upload",
    response_model=UploadSkillResponse,
    status_code=status.HTTP_201_CREATED,
)
async def publish_skill_upload(
    request: Request,
    file: UploadFile = File(..., description="Skill zip file to publish"),
    category_id: Optional[int] = Query(default=None),
    overwrite: bool = Query(default=False),
    cn_name: str = Query(default=""),
    skill_id: str = Query(default=""),
    bbk_ids: str = Query(default=""),
    include_in_statistics: bool = Query(
        default=False,
        description="是否纳入排行榜统计",
    ),
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_user_name: Optional[str] = Header(default=None, alias="X-User-Name"),
    x_bbk_id: Optional[str] = Header(default=None, alias="X-Bbk-Id"),
):
    """上传 zip 文件上架技能到市场（管理员）.

    Args:
        overwrite: 是否覆盖同名技能，默认 False（返回冲突提示）
        skill_id: parse-zip 生成的 skill_id，前端传入确保一致性
        bbk_ids: 所属分行 ID，逗号分隔，如 "100,200"
    """
    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)
    if not x_user_id:
        raise HTTPException(
            status_code=400,
            detail="X-User-Id header is required",
        )

    svc = request.app.state.marketplace
    if not svc.db.is_connected:
        raise HTTPException(status_code=503, detail="Database unavailable")
    user_name = decode_user_name(x_user_name) or x_user_id

    log_params(
        logger,
        request.method,
        request.url.path,
        category_id=category_id,
        overwrite=overwrite,
        cn_name=cn_name,
        skill_id=skill_id,
        bbk_ids=bbk_ids,
        include_in_statistics=include_in_statistics,
        file_size=file.size,
    )

    # 解析 bbk_ids（逗号分隔）
    parsed_bbk_ids = []
    if bbk_ids.strip():
        parsed_bbk_ids = [b.strip() for b in bbk_ids.split(",") if b.strip()]

    # 读取并验证 zip 文件
    data = await _read_validated_zip_upload(file)

    try:
        tmp_dir, found_skills = await asyncio.to_thread(
            _extract_zip_skills,
            data,
            file.filename,
        )
        _scan_found_skills_or_raise(
            found_skills,
            source_id=source_id,
            user_id=x_user_id,
            bbk_id=x_bbk_id or "",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except SkillScanError as e:
        await _flush_skill_scan_history(request)
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not found_skills:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return UploadSkillResponse(imported=[], count=0, enabled=True)

    try:
        (
            imported,
            conflicts,
            parsed_name,
            parsed_description,
            parsed_cn_name,
            has_unchanged,
            final_skill_id,
        ) = await _process_skill_upload_batch(
            found_skills,
            svc,
            source_id,
            x_user_id,
            user_name,
            category_id,
            overwrite,
            cn_name,
            skill_id,
            parsed_bbk_ids,
            include_in_statistics,
        )
    except MarketplaceMetadataSyncError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)

    result = UploadSkillResponse(
        imported=imported,
        count=len(imported),
        enabled=True,
        name=parsed_name,
        description=parsed_description,
        cn_name=parsed_cn_name,
        version_unchanged=has_unchanged,
        skill_id=final_skill_id,
    )
    if conflicts:
        result.conflicts = conflicts
    return result


@router.post(
    "/market/skills",
    response_model=MarketSkillResponse,
    status_code=status.HTTP_201_CREATED,
)
async def publish_skill(
    req: PublishSkillRequest,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_user_name: Optional[str] = Header(default=None, alias="X-User-Name"),
):
    """上架技能（管理员）."""
    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)
    svc = request.app.state.marketplace
    if not svc.db.is_connected:
        raise HTTPException(status_code=503, detail="Database unavailable")
    operator_name = ""
    if x_user_name:
        from urllib.parse import unquote

        try:
            operator_name = unquote(x_user_name)
        except Exception:  # pylint: disable=broad-except
            operator_name = x_user_name
    try:
        item, version_unchanged = await svc.publish_skill(
            source_id,
            req,
            operator_id=x_user_id or "",
            operator_name=operator_name,
        )
    except SkillNameConflictError as exc:
        # 同名续接后此分支理论上不会触发；保留为兜底
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "existing_item_id": exc.existing_item_id,
                "existing_name": exc.existing_name,
                "existing_creator_id": exc.existing_creator_id,
                "existing_creator_name": exc.existing_creator_name,
                "existing_version": exc.existing_version,
            },
        ) from exc
    except SkillVersionConflictError as exc:
        # F3 修复：版本快照撞车不再静默吞掉，让前端可见
        raise HTTPException(
            status_code=409,
            detail={
                "code": "VERSION_CONFLICT",
                "message": str(exc),
                "hint": "本次同步内容与已有版本撞车，请稍后重试或联系管理员",
            },
        ) from exc
    except MarketplaceMetadataSyncError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return MarketSkillResponse(
        item_id=item.item_id,
        name=item.name,
        description=item.description,
        version=item.version,
        creator_id=item.creator_id,
        creator_name=item.creator_name,
        category_id=item.category_id,
        bbk_ids=item.bbk_ids,
        status=item.status,
        created_at=item.created_at,
        updated_at=item.updated_at,
        version_unchanged=version_unchanged,
        include_in_statistics=item.include_in_statistics,
    )


@router.delete(
    "/market/skills/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unpublish_skill(
    item_id: str,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_user_name: Optional[str] = Header(default=None, alias="X-User-Name"),
):
    """下架技能（管理员）."""
    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)
    svc = request.app.state.marketplace
    ok = await svc.unpublish_skill(
        source_id,
        item_id,
        operator_id=x_user_id or "",
        operator_name=decode_user_name(x_user_name) or "",
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Skill not found")


@router.delete(
    "/market/skills/{item_id}/delete",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_skill_permanently(
    item_id: str,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_user_name: Optional[str] = Header(default=None, alias="X-User-Name"),
):
    """彻底删除技能及其版本历史（管理员）。"""
    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)
    svc = request.app.state.marketplace
    ok = await svc.delete_market_skill(
        source_id,
        item_id,
        operator_id=x_user_id or "",
        operator_name=decode_user_name(x_user_name) or "",
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Skill not found")


@router.post(
    "/market/skills/{item_id}/distribute",
    response_model=AsyncTaskSubmitResponse,
)
async def distribute_skill(
    item_id: str,
    req: DistributeRequest,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_user_name: Optional[str] = Header(default=None, alias="X-User-Name"),
) -> AsyncTaskSubmitResponse:
    """分发技能（管理员）。"""
    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)
    svc = request.app.state.marketplace
    if not getattr(svc.db, "is_connected", False):
        raise HTTPException(
            status_code=503,
            detail="Async task database connection is not available",
        )
    task_id = _new_async_task_id()
    target_users = await svc._resolve_target_users(
        source_id,
        req,
    )  # noqa: SLF001
    target_user_ids = [user["tenant_id"] for user in target_users]
    target_user_names = {
        user["tenant_id"]: (
            user.get("tenant_name")
            or user.get("user_name")
            or user.get("name")
        )
        for user in target_users
    }
    skill_item = _find_market_skill_item(svc, source_id, item_id)
    if skill_item is None:
        raise HTTPException(status_code=404, detail="Skill not found")

    store = _get_async_task_store(request)
    await store.start_task(
        task_id=task_id,
        service="market",
        task_type="market.skill.distribute",
        source_id=source_id,
        actor_user_id=x_user_id or "",
        actor_user_name=decode_user_name(x_user_name) or "",
        target_ids=target_user_ids,
        target_names=target_user_names,
        summary=_distribution_summary(
            "技能",
            skill_item.name,
            len(target_user_ids),
        ),
    )
    asyncio.create_task(
        _run_skill_distribution_task(
            task_id=task_id,
            store=store,
            svc=svc,
            source_id=source_id,
            item_id=skill_item.item_id,
            operator_id=x_user_id or "",
            operator_name=decode_user_name(x_user_name) or "",
            req=req,
            target_user_ids=target_user_ids,
        ),
    )
    return AsyncTaskSubmitResponse(task_id=task_id)


def _process_user_skill(
    skill_dir: Path,
    skill_name: str,
    user_id: str,
    agent_id: str,
    dry_run: bool,
    results: _InitUserSkillsResult,
) -> None:
    """处理单个技能的初始化逻辑."""
    skill_json_path = skill_dir / "skill.json"

    try:
        if not skill_json_path.exists():
            # 无 skill.json，创建新文件
            skill_data = {
                "schema_version": "workspace-skill.v1",
                "name": skill_name,
                "source": "customized",
                "description": "",
                "version": "1.0.0",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            results["created_skill_json"] += 1
            results["details"].append(
                {
                    "user_id": user_id,
                    "agent_id": agent_id,
                    "skill_name": skill_name,
                    "action": "created",
                },
            )

            if not dry_run:
                _atomic_write_json(skill_json_path, skill_data)
            return

        # 已有 skill.json，检查 source 字段
        try:
            skill_data = json.loads(
                skill_json_path.read_text(encoding="utf-8"),
            )
        except json.JSONDecodeError as e:
            results["errors"].append(
                {
                    "user_id": user_id,
                    "skill_name": skill_name,
                    "error": f"JSON decode error: {e}",
                },
            )
            return

        current_source = skill_data.get("source", "")

        if current_source.startswith("marketplace:"):
            # 已是分发技能，跳过
            results["skipped_marketplace"] += 1
            return

        if current_source == "customized":
            # 已是正确的值，跳过
            return

        # 需要更新 source
        skill_data["source"] = "customized"
        results["updated_source"] += 1
        results["details"].append(
            {
                "user_id": user_id,
                "agent_id": agent_id,
                "skill_name": skill_name,
                "action": "updated",
                "old_source": current_source,
            },
        )

        if not dry_run:
            _atomic_write_json(skill_json_path, skill_data)

    except Exception as e:
        results["errors"].append(
            {
                "user_id": user_id,
                "skill_name": skill_name,
                "error": str(e),
            },
        )


def _process_workspace_skills(
    workspace_dir: Path,
    user_id: str,
    dry_run: bool,
    results: _InitUserSkillsResult,
) -> None:
    """处理单个 workspace 下的所有技能."""
    agent_id = workspace_dir.name
    skills_dir = workspace_dir / "skills"
    if not skills_dir.exists():
        return

    results["processed_workspaces"] += 1

    for skill_dir in skills_dir.iterdir():
        if not skill_dir.is_dir():
            continue
        skill_name = skill_dir.name
        results["processed_skills"] += 1
        _process_user_skill(
            skill_dir,
            skill_name,
            user_id,
            agent_id,
            dry_run,
            results,
        )


@router.post(
    "/market/admin/skills/init-user-skills",
)
async def init_user_skills(
    request: Request,
    dry_run: bool = True,
    user_id: str | None = None,
):
    """初始化用户的历史技能数据为「我创建的」.

    处理逻辑：
    1. 遍历 SWE_ROOT 下用户目录（指定 user_id 则仅处理该用户）
    2. 对于每个用户的技能目录：
       - 无 skill.json：创建文件，设置 source=customized
       - 有 skill.json 但 source 为空或非 marketplace:：设置 source=customized
       - 已是 marketplace: 开头：跳过（保持为「我接收的」）

    Args:
        dry_run: True 仅预览变更，不实际写入；False 执行写入
        user_id: 可选，指定要初始化的用户 ID，不传则处理所有用户
    """
    svc = request.app.state.marketplace
    swe_root = svc.swe_root

    results: _InitUserSkillsResult = {
        "dry_run": dry_run,
        "processed_users": 0,
        "processed_workspaces": 0,
        "processed_skills": 0,
        "created_skill_json": 0,
        "updated_source": 0,
        "skipped_marketplace": 0,
        "errors": [],
        "details": [],
    }

    # 遍历用户目录（支持按 user_id 过滤）
    for user_dir in swe_root.iterdir():
        if not user_dir.is_dir():
            continue
        uid = user_dir.name

        # 指定了 user_id 时跳过不匹配的用户
        if user_id and uid != user_id:
            continue

        results["processed_users"] += 1

        workspace_base = user_dir / "workspaces"
        if not workspace_base.exists():
            continue

        for workspace_dir in workspace_base.iterdir():
            if not workspace_dir.is_dir():
                continue
            _process_workspace_skills(workspace_dir, uid, dry_run, results)

    return results


class _ListSkillsRequest(BaseModel):
    """查询技能列表请求参数."""

    source_id: str = Field(..., description="来源ID")

    # 预留未来扩展参数
    # user_ids: list[str] | None = Field(default=None, description="用户ID列表")
    # skill_types: list[str] | None = Field(default=None, description="技能类型过滤")


class _InitSweSkillsRequest(BaseModel):
    """初始化 swe_skills 表请求参数."""

    source_ids: list[str] = Field(
        default_factory=list,
        description="租户 source_id 列表",
    )
    user_ids: list[str] = Field(
        default_factory=list,
        description="用户 user_id 列表，不传或为空时初始化所有用户，否则只初始化指定用户",
    )
    force: bool = Field(
        default=False,
        description="是否强制重新初始化（覆盖已有数据）",
    )
    dry_run: bool = Field(
        default=False,
        description="试运行模式，true=仅统计不实际写入",
    )


class _InitSweSkillsResult(TypedDict):
    """初始化 swe_skills 表返回结果."""

    dry_run: bool
    source_ids: list[str]
    user_ids: list[str]
    total_users: int
    total_skills: int
    processed: int
    inserted_db: int
    skipped: int
    errors: list[dict]
    details: list[dict]


@router.post(
    "/market/skills/list",
)
async def list_skills(
    request: Request,
    body: _ListSkillsRequest,
):
    """查询技能列表.

    Args:
        body: 请求参数，包含 source_id 等

    Returns:
        技能列表，每个 skill_id 只返回一条记录，包含 skill_id、skill_name、cn_name
    """
    from ...marketplace.market_skill_registry import MarketSkillRegistry

    svc = request.app.state.marketplace
    registry = MarketSkillRegistry(svc.db)

    skills = (
        await registry.list_statistics_eligible_unique_skills_by_source_id(
            body.source_id,
        )
    )
    return {
        "source_id": body.source_id,
        "count": len(skills),
        "skills": skills,
    }


def _find_tenant_dirs_for_source_id(
    swe_root: Path,
    source_id: str,
    user_ids: list[str] | None = None,
) -> list[Path]:
    """查找指定 source_id 下的租户目录.

    Args:
        swe_root: SWE 根目录
        source_id: 租户 source_id
        user_ids: 可选，用户 user_id 列表，为空时返回所有匹配的用户

    Returns:
        租户目录列表
    """
    from ...runtime.context import encode_scope_id
    from ...marketplace.fs import resolve_effective_user_id

    tenant_dirs = []

    # 如果指定了 user_ids，根据 user_id 和 source_id 计算目录名
    if user_ids:
        for user_id in user_ids:
            # 计算有效的目录名
            effective_user_id = resolve_effective_user_id(user_id, source_id)
            tenant_dir = swe_root / effective_user_id
            logger.debug(
                "查找用户目录: user_id=%s, source_id=%s, effective_user_id=%s, path=%s",
                user_id,
                source_id,
                effective_user_id,
                tenant_dir,
            )
            if tenant_dir.exists() and tenant_dir.is_dir():
                tenant_dirs.append(tenant_dir)
        return tenant_dirs

    # 未指定 user_ids，查找所有匹配 source_id 的用户目录
    # 直接匹配 default_<source_id>
    default_dir = swe_root / f"default_{source_id}"
    if default_dir.exists() and default_dir.is_dir():
        tenant_dirs.append(default_dir)

    # 遍历目录查找 encode_scope_id 格式的用户目录
    for user_dir in swe_root.iterdir():
        if not user_dir.is_dir():
            continue
        dir_name = user_dir.name
        if dir_name.startswith("default_"):
            continue
        if "." not in dir_name:
            continue
        try:
            from ...runtime.context import decode_scope_id

            _, decoded_source = decode_scope_id(dir_name)
            if decoded_source == source_id:
                tenant_dirs.append(user_dir)
        except ValueError:
            pass

    return tenant_dirs


@router.post(
    "/market/admin/skills/init-swe-skills",
)
async def init_swe_skills(
    request: Request,
    payload: _InitSweSkillsRequest,
):
    """初始化 swe_skills 表，将现有技能写入数据库.

    实际扫描 + upsert 逻辑已下沉到 marketplace.skill_sync.process_tenant_skills。
    """
    from ...marketplace.skill_registry import SkillRegistry
    from ...marketplace.skill_sync import process_tenant_skills

    svc = request.app.state.marketplace
    swe_root = svc.swe_root
    registry = SkillRegistry(svc.db)

    results: _InitSweSkillsResult = {
        "dry_run": payload.dry_run,
        "source_ids": payload.source_ids,
        "user_ids": payload.user_ids,
        "total_users": 0,
        "total_skills": 0,
        "processed": 0,
        "inserted_db": 0,
        "skipped": 0,
        "errors": [],
        "details": [],
    }

    if not payload.source_ids:
        logger.warning("source_ids 为空，无数据需要初始化")
        return results

    logger.info(
        "开始初始化 swe_skills 表，dry_run=%s, source_ids=%s, user_ids=%s, force=%s",
        payload.dry_run,
        payload.source_ids,
        payload.user_ids or "(all)",
        payload.force,
    )

    for source_id in payload.source_ids:
        tenant_dirs = _find_tenant_dirs_for_source_id(
            swe_root,
            source_id,
            payload.user_ids,
        )
        results["total_users"] += len(tenant_dirs)

        for tenant_dir in tenant_dirs:
            manifest_before = _dump_workspace_manifests_for_log(tenant_dir)
            logger.info(
                "manifest 之前: tenant=%s, source_id=%s, content=%s",
                tenant_dir.name,
                source_id,
                json.dumps(
                    manifest_before,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
            try:
                result = await process_tenant_skills(
                    tenant_dir,
                    source_id=source_id,
                    registry=registry,
                    force=payload.force,
                    dry_run=payload.dry_run,
                )
                results["total_skills"] += result["total_skills"]
                results["processed"] += result["total_skills"]
                results["details"].extend(result["details"])
                results["errors"].extend(result["errors"])
                if not payload.dry_run:
                    results["inserted_db"] += result["synced"]
            except Exception as exc:
                logger.exception(
                    "处理租户目录失败: dir=%s err=%s",
                    tenant_dir,
                    exc,
                )
                results["errors"].append(
                    {
                        "tenant_id": str(tenant_dir),
                        "error": str(exc),
                    },
                )
            else:
                manifest_after = _dump_workspace_manifests_for_log(tenant_dir)
                logger.info(
                    "manifest 之后: tenant=%s, source_id=%s, content=%s",
                    tenant_dir.name,
                    source_id,
                    json.dumps(
                        manifest_after,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )

    logger.info(
        "初始化完成: total_users=%d, total_skills=%d, processed=%d, inserted=%d, errors=%d",
        results["total_users"],
        results["total_skills"],
        results["processed"],
        results["inserted_db"],
        len(results["errors"]),
    )

    return results


def _dump_workspace_manifests(tenant_dir: Path) -> dict[str, object]:
    """读取 tenant_dir 下所有 workspace 的 skill.json 内容.

    给 init_swe_skills 在调 process_tenant_skills 前后做对比用：
    返回 {workspace_name: manifest_dict_or_error}，
    workspace 不存在 / manifest 缺失则不计入。
    """
    manifests: dict[str, object] = {}
    workspaces = tenant_dir / "workspaces"
    if not workspaces.exists():
        return manifests
    for ws_dir in workspaces.iterdir():
        if not ws_dir.is_dir():
            continue
        manifest_path = get_workspace_skill_manifest_path(ws_dir)
        if not manifest_path.exists():
            continue
        try:
            manifests[ws_dir.name] = json.loads(
                manifest_path.read_text(encoding="utf-8"),
            )
        except json.JSONDecodeError as exc:
            manifests[ws_dir.name] = {"_error": f"manifest 解析失败: {exc}"}
    return manifests


def _dump_workspace_manifests_for_log(tenant_dir: Path) -> object:
    """生成适合直接写入日志的 manifest 内容。

    只有一个 workspace 时，直接展开其 skill.json 内容，避免多包一层 workspace 名。
    """
    manifests = _dump_workspace_manifests(tenant_dir)
    if len(manifests) == 1:
        return next(iter(manifests.values()))
    return manifests


def _resolve_tenant_dir(swe_root: Path, tenant_id: str) -> Path | None:
    """根据 tenant_id 在 swe_root 下解析出对应的租户目录。

    解析规则（与 init-swe-skills 同款）：
    - "default" → swe_root / default_<default_source> （通常 swe_root / default）
    - tenant_id 含 "." → 尝试 decode_scope_id 取回原 user_id 后再拼目录名
    - 否则 → swe_root / tenant_id

    Returns:
        租户目录 Path；不存在返回 None。
    """
    from ...marketplace.fs import resolve_effective_user_id
    from ...runtime.context import decode_scope_id

    # 直接匹配
    direct = swe_root / tenant_id
    if direct.exists() and direct.is_dir():
        return direct

    # decode_scope_id 还原
    if "." in tenant_id:
        try:
            decoded_user_id, decoded_source = decode_scope_id(tenant_id)
            effective = resolve_effective_user_id(
                decoded_user_id,
                decoded_source,
            )
            candidate = swe_root / effective
            if candidate.exists() and candidate.is_dir():
                return candidate
        except ValueError:
            pass

    # default_<source_id> 形态
    if tenant_id == "default":
        for child in swe_root.iterdir():
            if child.is_dir() and child.name.startswith("default_"):
                return child

    return None


def _decode_source_id_from_tenant_id(tenant_id: str) -> str | None:
    """从 scope 编码的 tenant_id 解出 source_id 段。

    src/swe 的 bootstrap_tenant_id 形如 "<user>.<source>"（来自
    resolve_storage_tenant_id 的 scope 编码）；内部同步端点拿到该 ID 时
    应还原 source_id 写入 swe_skills.source_id 列，避免新建租户的技能
    因 source_id 为空而无法被 init-swe-skills 等按 source 维度的接口检索到。

    Args:
        tenant_id: bootstrap 阶段的 tenant_id（可能含 "."）

    Returns:
        解出的 source_id；无 "." 或解码失败时返回 None。
    """
    from ...runtime.context import decode_scope_id

    if "." not in tenant_id:
        return None
    try:
        _, decoded_source = decode_scope_id(tenant_id)
    except ValueError:
        return None
    return decoded_source or None


def _check_internal_caller(request: Request) -> bool:
    """校验是否来自同集群内部调用（X-Internal-Token）。"""
    from ...config.constant import MARKET_INTERNAL_TOKEN

    if not MARKET_INTERNAL_TOKEN:
        return True
    return request.headers.get("X-Internal-Token") == MARKET_INTERNAL_TOKEN


@router.post("/market/internal/tenants/{tenant_id}/sync-skills")
async def internal_sync_skills(
    request: Request,
    tenant_id: str,
):
    """为指定租户触发一次 swe_skills 同步（由 src/swe 的 tenant_initializer 调用）。

    失败语义：单个技能写库失败被吞到 result.errors，路由仍返回 200。
    """
    if not _check_internal_caller(request):
        raise HTTPException(status_code=403, detail="forbidden")

    from ...marketplace.skill_registry import SkillRegistry
    from ...marketplace.skill_sync import process_tenant_skills

    svc = request.app.state.marketplace
    swe_root = svc.swe_root
    registry = SkillRegistry(svc.db)

    tenant_dir = _resolve_tenant_dir(swe_root, tenant_id)
    if tenant_dir is None:
        logger.warning(
            "internal_sync_skills: tenant_dir 不存在 swe_root=%s tenant_id=%s",
            swe_root,
            tenant_id,
        )
        return {
            "tenant_id": tenant_id,
            "synced": 0,
            "warning": "tenant_dir_not_found",
        }

    # 从 scope 编码的 tenant_id 还原 source_id，保持与 admin 端点
    # init-swe-skills 写入 swe_skills.source_id 的语义一致
    source_id = _decode_source_id_from_tenant_id(tenant_id)

    try:
        result = await process_tenant_skills(
            tenant_dir,
            source_id=source_id,
            registry=registry,
            force=False,
            dry_run=False,
            # src/swe 自己负责写 skill.json，禁止回写避免污染
            # per-user skill_id（参见 skill_sync.write_manifest_back 说明）
            write_manifest_back=False,
        )
    except Exception as exc:
        logger.exception(
            "internal_sync_skills 失败 tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return {
            "tenant_id": tenant_id,
            "synced": 0,
            "error": str(exc),
        }

    logger.info(
        "internal_sync_skills 完成 tenant=%s synced=%d errors=%d",
        tenant_id,
        result["synced"],
        len(result["errors"]),
    )
    return {
        "tenant_id": tenant_id,
        "synced": result["synced"],
        "errors": result["errors"],
    }


@router.get(
    "/market/skills/{item_id}/distributions",
)
async def get_skill_distributions(
    item_id: str,
    request: Request,
    skill_name: Optional[str] = None,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
):
    """查询技能分发记录（管理员）."""
    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)
    svc = request.app.state.marketplace
    distributions = await svc.get_distributions(
        source_id,
        item_id,
        "skill",
        skill_name=skill_name,
    )
    return distributions


class _UpdateSkillRequest(BaseModel):
    """更新技能市场元数据请求体."""

    skill_id: str
    chinese_name: str
    category_id: int | None = None
    bbk_ids: list[str] | None = None
    sync_to_users: bool = False
    target_user_ids: list[str] = Field(default_factory=list)


class _UpdateSkillResponse(BaseModel):
    """更新技能市场元数据响应体."""

    success: bool
    market_updated: bool
    synced_users: int
    skipped_users: int
    errors: list[dict]
    synced_category_users: int = 0


@router.patch("/market/skills/{item_id}")
async def update_skill_cn_name(
    item_id: str,
    req: _UpdateSkillRequest,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
):
    """更新市场技能元数据，可选同步用户空间名称和分类."""
    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)
    svc = request.app.state.marketplace

    # 从 MarketItem 获取 skill_name
    items = load_index(svc.marketplace_root, source_id)
    item = next(
        (i for i in items if i.item_id == item_id and i.item_type == "skill"),
        None,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Skill not found")

    try:
        result = await svc.update_skill_metadata(
            source_id=source_id,
            item_id=item_id,
            skill_id=req.skill_id,
            skill_name=item.name,
            chinese_name=req.chinese_name,
            category_id=(
                req.category_id
                if req.category_id is not None
                else item.category_id
            ),
            bbk_ids=req.bbk_ids if req.bbk_ids is not None else item.bbk_ids,
            sync_to_users=req.sync_to_users,
            target_user_ids=req.target_user_ids,
        )
    except MarketplaceMetadataSyncError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return _UpdateSkillResponse(**result)


@router.post(
    "/market/skills/recall",
)
async def recall_skill_by_name(
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_user_name: Optional[str] = Header(default=None, alias="X-User-Name"),
) -> dict:
    """按技能名称撤回（管理员）."""
    from ...marketplace.schemas import RecallRequest

    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)
    svc = request.app.state.marketplace

    # 解析请求体
    body = await request.json()
    target_user_ids = body.get("target_user_ids")
    skill_name = body.get("skill_name")
    req = RecallRequest(
        target_user_ids=target_user_ids,
        skill_name=skill_name,
    )

    try:
        result = await svc.recall_skill(
            source_id,
            None,
            operator_id=x_user_id or "",
            operator_name=decode_user_name(x_user_name) or "",
            req=req,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result.model_dump()


@router.post(
    "/market/skills/{item_id}/recall",
)
async def recall_skill(
    item_id: str,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_user_name: Optional[str] = Header(default=None, alias="X-User-Name"),
) -> dict:
    """撤回已分发的技能（管理员）."""
    from ...marketplace.schemas import RecallRequest

    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)
    svc = request.app.state.marketplace

    # 解析请求体
    body = await request.json()
    target_user_ids = body.get("target_user_ids")
    force = body.get("force", False)
    req = RecallRequest(
        target_user_ids=target_user_ids,
        force=force,
    )

    try:
        result = await svc.recall_skill(
            source_id,
            item_id,
            operator_id=x_user_id or "",
            operator_name=decode_user_name(x_user_name) or "",
            req=req,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return result.model_dump()


class _InitMarketSkillsRequest(BaseModel):
    """初始化市场技能请求参数."""

    source_ids: list[str] = Field(
        default_factory=list,
        description="来源ID列表，不传或为空时初始化所有来源",
    )
    dry_run: bool = Field(
        default=False,
        description="试运行模式，仅统计不实际写入",
    )


class _InitMarketSkillsResult(TypedDict):
    """初始化市场技能返回结果."""

    dry_run: bool
    source_ids: list[str]
    total_items: int
    processed: int
    updated: int
    skipped: int
    errors: list[dict]
    details: list[dict]


def _truncate_chinese_name(cn_name: str, max_length: int = 50) -> str:
    """截断中文名，防止过长."""
    if len(cn_name) > max_length:
        return cn_name[:max_length]
    return cn_name


def _extract_skill_metadata_from_md(
    skill_md_path: Path,
    item_id: str,
    item_name: str,
) -> tuple[str, str]:
    """从 SKILL.md 提取 skill_id 和 chinese_name.

    Args:
        skill_md_path: SKILL.md 文件路径
        item_id: 市场条目 ID
        item_name: 技能名称

    Returns:
        (skill_id, chinese_name)
    """
    from ...utils.skill_md import (
        extract_skill_id,
        extract_cn_name_from_title,
        parse_frontmatter,
    )

    skill_id = ""
    chinese_name = ""

    if not skill_md_path.exists():
        return skill_id, chinese_name

    try:
        md_content = skill_md_path.read_text(encoding="utf-8")

        # 提取 skill_id（市场技能 source 为 marketplace:{item_id}）
        skill_id = extract_skill_id(
            md_content,
            f"marketplace:{item_id}",
            item_name,
            creator_id="",
        )

        # 提取 chinese_name：优先 frontmatter，其次一级标题
        fm = parse_frontmatter(md_content)
        metadata = fm.get("metadata", {})
        if isinstance(metadata, dict):
            chinese_name = metadata.get("cn_name", "") or metadata.get(
                "chinese_name",
                "",
            )
        if not chinese_name:
            chinese_name = extract_cn_name_from_title(md_content)
    except OSError as e:
        logger.warning(
            "读取 SKILL.md 失败: item_id=%s, error=%s",
            item_id,
            e,
        )

    return skill_id, chinese_name


def _process_single_skill_item(
    item: MarketItem,
    marketplace_root: Path,
    source_id: str,
) -> tuple[str, str, bool]:
    """处理单个技能条目，提取 skill_id 和 chinese_name.

    Args:
        item: 市场条目
        marketplace_root: 市场根目录
        source_id: 来源 ID

    Returns:
        (skill_id, chinese_name, needs_update)
    """
    skill_dir = get_skill_dir(marketplace_root, source_id, item.item_id)
    skill_md_path = skill_dir / "SKILL.md"

    # 提取 skill_id 和 chinese_name
    skill_id, chinese_name = _extract_skill_metadata_from_md(
        skill_md_path,
        item.item_id,
        item.name,
    )

    # fallback: skill_id 使用 item_id
    if not skill_id:
        skill_id = item.item_id

    # fallback: chinese_name 使用 MarketItem.chinese_name 或 name
    if not chinese_name:
        chinese_name = item.chinese_name or item.name

    # 截断 chinese_name（最多50字）
    chinese_name = _truncate_chinese_name(chinese_name, 50)

    # 检查是否需要更新
    needs_update = False
    if not item.skill_id and skill_id:
        item.skill_id = skill_id
        needs_update = True
    if not item.chinese_name and chinese_name:
        item.chinese_name = chinese_name
        needs_update = True

    return skill_id, chinese_name, needs_update


def _process_source_id_skills(
    source_id: str,
    marketplace_root: Path,
    dry_run: bool,
    results: _InitMarketSkillsResult,
) -> None:
    """处理单个 source_id 下的所有技能.

    Args:
        source_id: 来源 ID
        marketplace_root: 市场根目录
        dry_run: 试运行模式
        results: 结果统计
    """
    logger.info("处理 source_id=%s", source_id)

    # 加载 index.json
    items = load_index(marketplace_root, source_id)
    logger.debug(
        "加载 index.json: source_id=%s, items=%d",
        source_id,
        len(items),
    )

    # 过滤 skill 类型
    skill_items = [item for item in items if item.item_type == "skill"]
    results["total_items"] += len(skill_items)
    logger.debug(
        "过滤 skill 类型: source_id=%s, skill_items=%d",
        source_id,
        len(skill_items),
    )

    updated_items = []
    for item in skill_items:
        skill_id, chinese_name, needs_update = _process_single_skill_item(
            item,
            marketplace_root,
            source_id,
        )

        results["processed"] += 1

        if needs_update:
            updated_items.append(item)
            results["updated"] += 1
            results["details"].append(
                {
                    "source_id": source_id,
                    "item_id": item.item_id,
                    "name": item.name,
                    "skill_id": skill_id,
                    "chinese_name": chinese_name,
                },
            )
            logger.info(
                "更新条目: source_id=%s, item_id=%s, name=%s, skill_id=%s, chinese_name=%s",
                source_id,
                item.item_id,
                item.name,
                skill_id,
                chinese_name,
            )
        else:
            results["skipped"] += 1
            logger.info(
                "跳过条目（无需更新）: source_id=%s, item_id=%s, name=%s",
                source_id,
                item.item_id,
                item.name,
            )

    # 保存更新后的 index.json（非 dry_run 模式）
    if updated_items and not dry_run:
        save_index(marketplace_root, source_id, items)
        logger.info(
            "保存 index.json: source_id=%s, updated=%d",
            source_id,
            len(updated_items),
        )
    elif updated_items and dry_run:
        logger.info(
            "试运行模式，不保存: source_id=%s, would_update=%d",
            source_id,
            len(updated_items),
        )


@router.post(
    "/market/admin/skills/init-market-skills",
)
async def init_market_skills(
    request: Request,
    payload: _InitMarketSkillsRequest,
):
    """初始化市场技能的 skill_id 和 chinese_name.

    遍历 index.json 中 item_type == "skill" 的条目，
    从 SKILL.md 提取 skill_id 和 chinese_name，
    补充缺失的字段并保存。

    Args:
        payload.source_ids: 来源ID列表，不传或为空时初始化所有来源
        payload.dry_run: 试运行模式，仅统计不实际写入
    """
    svc = request.app.state.marketplace
    marketplace_root = svc.marketplace_root

    results: _InitMarketSkillsResult = {
        "dry_run": payload.dry_run,
        "source_ids": [],
        "total_items": 0,
        "processed": 0,
        "updated": 0,
        "skipped": 0,
        "errors": [],
        "details": [],
    }

    # 确定 source_ids 列表
    if payload.source_ids:
        source_ids = payload.source_ids
    else:
        # 遍历 marketplace_root 下所有目录作为 source_ids
        source_ids = []
        for dir_path in marketplace_root.iterdir():
            if dir_path.is_dir():
                index_path = dir_path / "index.json"
                if index_path.exists():
                    source_ids.append(dir_path.name)

    results["source_ids"] = source_ids

    logger.info(
        "开始初始化市场技能: dry_run=%s, source_ids=%s",
        payload.dry_run,
        source_ids,
    )

    for source_id in source_ids:
        _process_source_id_skills(
            source_id,
            marketplace_root,
            payload.dry_run,
            results,
        )

    logger.info(
        "初始化完成: dry_run=%s, total_items=%d, processed=%d, updated=%d, skipped=%d, errors=%d",
        payload.dry_run,
        results["total_items"],
        results["processed"],
        results["updated"],
        results["skipped"],
        len(results["errors"]),
    )

    return results


class _InitMissingSkillFieldsRequest(BaseModel):
    """初始化缺失分类和分行请求参数."""

    source_id: str = Field(..., description="来源ID")
    category_id: int = Field(..., description="要设置的分类ID")
    bbk_ids: list[str] = Field(
        default_factory=list,
        description="要设置的分行ID列表，默认为 ['100']",
    )
    dry_run: bool = Field(
        default=True,
        description="试运行模式，仅预览不实际写入",
    )


class _SkillMissingFieldsItem(BaseModel):
    """缺失字段的技能条目."""

    item_id: str
    name: str
    chinese_name: str
    category_id: int | None
    bbk_ids: list[str]
    missing_category: bool
    missing_bbk: bool


class _InitMissingSkillFieldsResult(BaseModel):
    """初始化缺失分类和分行返回结果."""

    dry_run: bool
    source_id: str
    category_id: int
    bbk_ids: list[str]
    total_skills: int
    missing_category_count: int
    missing_bbk_count: int
    skills: list[_SkillMissingFieldsItem]


def _collect_missing_skill_fields(
    items: list[MarketItem],
) -> tuple[list[_SkillMissingFieldsItem], int, int]:
    skills: list[_SkillMissingFieldsItem] = []
    missing_category_count = 0
    missing_bbk_count = 0
    for item in items:
        if item.item_type != "skill":
            continue
        missing_category = item.category_id is None
        missing_bbk = not item.bbk_ids
        if not (missing_category or missing_bbk):
            continue
        skills.append(
            _SkillMissingFieldsItem(
                item_id=item.item_id,
                name=item.name,
                chinese_name=item.chinese_name,
                category_id=item.category_id,
                bbk_ids=item.bbk_ids,
                missing_category=missing_category,
                missing_bbk=missing_bbk,
            ),
        )
        if missing_category:
            missing_category_count += 1
        if missing_bbk:
            missing_bbk_count += 1
    return skills, missing_category_count, missing_bbk_count


def _apply_missing_skill_fields(
    items: list[MarketItem],
    category_id: int,
    bbk_ids: list[str],
) -> list[MarketItem]:
    changed_items: list[MarketItem] = []
    for item in items:
        if item.item_type != "skill":
            continue
        changed = False
        if item.category_id is None:
            item.category_id = category_id
            changed = True
        if not item.bbk_ids:
            item.bbk_ids = bbk_ids.copy()
            changed = True
        if changed:
            item.updated_at = datetime.now(timezone.utc).isoformat()
            changed_items.append(item)
    return changed_items


async def _sync_missing_skill_fields_to_market_db(
    svc,
    source_id: str,
    changed_items: list[MarketItem],
) -> None:
    if not changed_items:
        return
    if not svc.db.is_connected:
        raise HTTPException(status_code=503, detail="Database unavailable")
    try:
        async with svc.db.transaction():
            for item in changed_items:
                success = await svc.market_skill_registry.upsert_market_skill(
                    source_id=source_id,
                    item_id=item.item_id,
                    skill_id=item.skill_id,
                    skill_name=item.name,
                    cn_name=item.chinese_name,
                    description=item.description,
                    version=item.version,
                    status=item.status,
                    category_id=item.category_id,
                    bbk_ids=item.bbk_ids,
                    content_path=str(
                        svc.marketplace_root
                        / source_id
                        / "skills"
                        / item.item_id,
                    ),
                    include_in_statistics=item.include_in_statistics,
                    creator_id=item.creator_id,
                    creator_name=item.creator_name,
                )
                if not success:
                    raise MarketplaceMetadataSyncError(
                        "Failed to synchronize market skill metadata",
                    )
    except MarketplaceMetadataSyncError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Failed to synchronize market skill metadata",
        ) from exc


@router.post(
    "/market/skills/init-missing-fields",
    response_model=_InitMissingSkillFieldsResult,
)
async def init_missing_skill_fields(
    request: Request,
    payload: _InitMissingSkillFieldsRequest,
):
    """初始化缺失分类和分行的技能.

    扫描 index.json 中 category_id 为空或 bbk_ids 为空的技能，
    预览或更新这些技能的 category_id 和 bbk_ids。

    Args:
        payload.source_id: 来源ID
        payload.category_id: 要设置的分类ID
        payload.bbk_ids: 要设置的分行ID列表，默认为 ["100"]
        payload.dry_run: 试运行模式，仅预览不实际写入
    """
    log_params(
        logger,
        request.method,
        request.url.path,
        source_id=payload.source_id,
        category_id=payload.category_id,
        bbk_ids=payload.bbk_ids,
        dry_run=payload.dry_run,
    )

    svc = request.app.state.marketplace
    items = load_index(svc.marketplace_root, payload.source_id)

    # 默认分行为 ["100"]（总行）
    bbk_ids_to_set = payload.bbk_ids if payload.bbk_ids else ["100"]

    skills, missing_category_count, missing_bbk_count = (
        _collect_missing_skill_fields(items)
    )

    # 非 dry_run 模式：实际更新
    if not payload.dry_run:
        changed_items = _apply_missing_skill_fields(
            items,
            payload.category_id,
            bbk_ids_to_set,
        )
        if changed_items:
            await _sync_missing_skill_fields_to_market_db(
                svc,
                payload.source_id,
                changed_items,
            )
            save_index(svc.marketplace_root, payload.source_id, items)

    logger.info(
        "初始化缺失字段完成: dry_run=%s, source_id=%s, category_id=%s, "
        "bbk_ids=%s, total_skills=%d, missing_category=%d, missing_bbk=%d",
        payload.dry_run,
        payload.source_id,
        payload.category_id,
        bbk_ids_to_set,
        len(skills),
        missing_category_count,
        missing_bbk_count,
    )

    return _InitMissingSkillFieldsResult(
        dry_run=payload.dry_run,
        source_id=payload.source_id,
        category_id=payload.category_id,
        bbk_ids=bbk_ids_to_set,
        total_skills=len(skills),
        missing_category_count=missing_category_count,
        missing_bbk_count=missing_bbk_count,
        skills=skills,
    )


@router.post(
    "/market/skills/{item_id}/distribution-preview",
    response_model=DistributionPreviewResponse,
)
async def get_distribution_preview(
    item_id: str,
    request: Request,
    body: DistributionPreviewRequest,
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
):
    """获取技能分发预览（管理员）.

    返回每个用户的技能持有状态：
    - first_time: 首次分发
    - update: 覆盖更新（显示当前版本）
    - conflict: 自建冲突（不可覆盖）
    """
    _require_manager(x_manager)
    svc = request.app.state.marketplace
    source_id = body.source_id
    target_tenant_ids = body.tenant_ids

    if not target_tenant_ids:
        # 无目标用户时返回空预览
        items = load_index(svc.marketplace_root, source_id)
        item = next(
            (
                i
                for i in items
                if i.item_id == item_id and i.item_type == "skill"
            ),
            None,
        )
        if item is None:
            raise HTTPException(status_code=404, detail="Skill not found")
        return DistributionPreviewResponse(
            skill_version=item.version,
            users=[],
            distributed_user_ids=[],
        )

    try:
        result = await svc.get_distribution_preview(
            source_id,
            item_id,
            target_tenant_ids,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return DistributionPreviewResponse(
        skill_version=result["skill_version"],
        users=[UserSkillStatus(**u) for u in result["users"]],
        distributed_user_ids=result["distributed_user_ids"],
    )


# ===== 统计配置管理 =====


class UpdateStatisticsConfigRequest(BaseModel):
    """更新统计配置请求."""

    include_in_statistics: bool = Field(
        ...,
        description="是否纳入统计",
    )
    updator_id: str = Field(default="", description="更新人ID")
    updator_name: str = Field(default="", description="更新人名称")


class UpdateStatisticsConfigResponse(BaseModel):
    """更新统计配置响应."""

    success: bool
    item_id: str
    skill_name: str
    include_in_statistics: bool


@router.patch(
    "/market/skills/{item_id}/statistics",
    response_model=UpdateStatisticsConfigResponse,
)
async def update_skill_statistics_config(
    item_id: str,
    req: UpdateStatisticsConfigRequest,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
) -> UpdateStatisticsConfigResponse:
    """更新技能统计配置（管理员）."""
    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)
    svc = request.app.state.marketplace

    # 从 index.json 获取技能
    items = load_index(svc.marketplace_root, source_id)
    item = next(
        (i for i in items if i.item_id == item_id and i.item_type == "skill"),
        None,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Skill not found")

    # 更新 index.json
    item.include_in_statistics = req.include_in_statistics
    item.updated_at = datetime.now(timezone.utc).isoformat()
    save_index(svc.marketplace_root, source_id, items)

    # 同步更新数据库
    if svc.db and svc.db.is_connected:
        from ...marketplace.market_skill_registry import MarketSkillRegistry

        registry = MarketSkillRegistry(svc.db)
        await registry.update_statistics_config(
            source_id=source_id,
            item_id=item_id,
            include_in_statistics=req.include_in_statistics,
            updator_id=req.updator_id,
            updator_name=req.updator_name,
        )

    return UpdateStatisticsConfigResponse(
        success=True,
        item_id=item_id,
        skill_name=item.name,
        include_in_statistics=req.include_in_statistics,
    )


# 已由 /market/admin/metadata/migrate 替代，停用旧的全量统计初始化入口。
# class InitStatisticsConfigRequest(BaseModel):
#     """初始化统计配置请求."""
#
#     source_ids: list[str] = Field(
#         default_factory=list,
#         description="来源ID列表，不传或为空时初始化所有来源",
#     )
#     default_include: bool = Field(
#         default=True,
#         description="默认是否纳入统计",
#     )
#     dry_run: bool = Field(
#         default=False,
#         description="试运行模式，仅统计不实际写入",
#     )
#
#
# class InitStatisticsConfigResult(TypedDict):
#     """初始化统计配置结果."""
#
#     dry_run: bool
#     source_ids: list[str]
#     total_skills: int
#     processed: int
#     inserted: int
#     updated: int
#     skipped: int
#     errors: list[dict]
#
#
# async def _init_single_skill_statistics(
#     registry,
#     item: MarketItem,
#     source_id: str,
#     default_include: bool,
#     dry_run: bool,
# ) -> tuple[bool, dict | None]:
#     """初始化单个技能的统计配置.
#
#     Returns:
#         (success, error_dict) - 成功时 error_dict 为 None
#     """
#     if dry_run:
#         return True, None
#
#     try:
#         success = await registry.upsert_market_skill(
#             source_id=source_id,
#             item_id=item.item_id,
#             skill_id=item.skill_id,
#             skill_name=item.name,
#             cn_name=item.chinese_name,
#             include_in_statistics=default_include,
#             creator_id=item.creator_id,
#             creator_name=item.creator_name,
#             updator_id=item.creator_id,
#             updator_name=item.creator_name,
#         )
#         if success:
#             return True, None
#         return False, {
#             "item_id": item.item_id,
#             "skill_name": item.name,
#             "reason": "数据库写入返回失败",
#         }
#     except Exception as e:
#         return False, {
#             "item_id": item.item_id,
#             "skill_name": item.name,
#             "reason": str(e),
#         }
#
#
# @router.post(
#     "/market/admin/skills/init-statistics",
# )
# async def init_skill_statistics_config(
#     request: Request,
#     req: InitStatisticsConfigRequest,
# ) -> InitStatisticsConfigResult:
#     """初始化技能统计配置."""
#     svc = request.app.state.marketplace
#
#     results: InitStatisticsConfigResult = {
#         "dry_run": req.dry_run,
#         "source_ids": [],
#         "total_skills": 0,
#         "processed": 0,
#         "inserted": 0,
#         "updated": 0,
#         "skipped": 0,
#         "errors": [],
#     }
#
#     # 检查数据库连接
#     if not svc.db or not svc.db.is_connected:
#         results["errors"].append(
#             {
#                 "reason": "数据库未连接，无法初始化",
#             },
#         )
#         return results
#
#     # 确定 source_ids 列表
#     if req.source_ids:
#         source_ids = req.source_ids
#     else:
#         # 遍历 marketplace_root 下所有目录
#         source_ids = []
#         for dir_path in svc.marketplace_root.iterdir():
#             if dir_path.is_dir():
#                 index_path = dir_path / "index.json"
#                 if index_path.exists():
#                     source_ids.append(dir_path.name)
#
#     results["source_ids"] = source_ids
#
#     # 初始化数据库操作类
#     from ...marketplace.market_skill_registry import MarketSkillRegistry
#
#     registry = MarketSkillRegistry(svc.db)
#
#     # dry_run 模式：只统计数量，不写入数据库和文件
#     if req.dry_run:
#         for source_id in source_ids:
#             items = load_index(svc.marketplace_root, source_id)
#             skill_items = [i for i in items if i.item_type == "skill"]
#             results["total_skills"] += len(skill_items)
#             results["processed"] += len(skill_items)
#             results["inserted"] = results["processed"]  # dry_run 假设全部成功
#         return results
#
#     # 实际执行模式
#     for source_id in source_ids:
#         items = load_index(svc.marketplace_root, source_id)
#         skill_items = [i for i in items if i.item_type == "skill"]
#         results["total_skills"] += len(skill_items)
#
#         for item in skill_items:
#             results["processed"] += 1
#
#             success, error = await _init_single_skill_statistics(
#                 registry=registry,
#                 item=item,
#                 source_id=source_id,
#                 default_include=req.default_include,
#                 dry_run=False,
#             )
#             if success:
#                 results["inserted"] += 1
#                 # 只更新 skill 类型的 include_in_statistics 字段
#                 item.include_in_statistics = req.default_include
#             else:
#                 results["skipped"] += 1
#                 if error:
#                     results["errors"].append(error)
#
#         # 保存 index.json（只包含 skill 类型的变更）
#         save_index(svc.marketplace_root, source_id, items)
#
#     return results
