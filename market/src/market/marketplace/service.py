# -*- coding: utf-8 -*-
# pylint: disable=too-many-public-methods
"""应用市场业务服务."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import tomllib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional
from urllib.parse import unquote

import httpx

from ..config.constant import SWE_INTERNAL_URL, SWE_INTERNAL_TOKEN
from ..database.connection import DatabaseConnection
from ..security import SkillScanError, scan_skill_directory
from ..utils.skill_md import (
    extract_version as _extract_version_md,
    parse_frontmatter,
)
from ..utils.skill_utils import clean_skill_name
from ..utils.version import bump_patch as _shared_bump_patch
from .fs import (
    _atomic_write_json,
    _mask_env_value,
    copy_mcp_to_user,
    copy_skill_to_user,
    get_expert_dir,
    get_expert_definition_path,
    get_user_expert_dir,
    get_mcp_dir,
    get_skill_dir,
    get_user_disabled_skills_dir,
    get_user_skill_manifest_path,
    get_user_skills_dir,
    _validate_path_segment,
    load_index,
    migrate_legacy_scope_dir_if_needed,
    mutate_user_skill_manifest,
    read_user_skill_manifest,
    resolve_registered_skill_path,
    load_mcp_config,
    normalize_mcp_config_data,
    resolve_effective_user_id,
    save_index,
    save_mcp_config,
    normalize_skill_name,
)
from .skill_registry import SkillRegistry
from ..runtime.context import decode_scope_id
from ..runtime.config_store import MCPClientConfig
from .models import MarketItem
from .schemas import (
    DistributeRequest,
    DistributeResponse,
    DistributeTenantResult,
    DistributionRecord,
    MCPDistributionRequest,
    MCPDistributionResponse,
    MCPDistributionTenantResult,
    MarketMCPDetail,
    MarketMCPItem,
    MarketExpertDetail,
    MarketExpertResponse,
    MarketSkillDetail,
    MarketSkillResponse,
    MCPConfigDetail,
    MCPUserStat,
    MySkillItem,
    PublishMCPRequest,
    PublishSkillRequest,
    RecallRequest,
    RecallResponse,
    RecallResultItem,
    ExpertDistributionRequest,
    ExpertDistributionResponse,
    ExpertInstallRequest,
    ExpertOperationResult,
    ExpertRecallResponse,
    SkillUserStat,
)
from .expert_version_service import ExpertVersionService
from .version_service import SkillVersionService

if TYPE_CHECKING:
    from .mcp_version_service import MCPVersionService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ExpertInstallTarget:
    definition_id: str
    definition_path: Path
    enabled: bool
    error: ExpertOperationResult | None = None


class MCPNameConflictError(Exception):
    """MCP 同名冲突异常，用于发布时检测到同名 MCP 已存在。"""

    def __init__(
        self,
        existing_item_id: str,
        existing_name: str,
        existing_creator_id: str = "",
        existing_creator_name: str = "",
        existing_version: str = "",
    ) -> None:
        self.existing_item_id = existing_item_id
        self.existing_name = existing_name
        self.existing_creator_id = existing_creator_id
        self.existing_creator_name = existing_creator_name
        self.existing_version = existing_version
        super().__init__(
            f"MCP with name '{existing_name}' already exists "
            f"(created by {existing_creator_name or existing_creator_id})",
        )


class SkillNameConflictError(Exception):
    """技能同名冲突异常，用于发布时检测到同名技能已存在。"""

    def __init__(
        self,
        existing_item_id: str,
        existing_name: str,
        existing_creator_id: str = "",
        existing_creator_name: str = "",
        existing_version: str = "",
    ) -> None:
        self.existing_item_id = existing_item_id
        self.existing_name = existing_name
        self.existing_creator_id = existing_creator_id
        self.existing_creator_name = existing_creator_name
        self.existing_version = existing_version
        super().__init__(
            f"Skill with name '{existing_name}' already exists "
            f"(created by {existing_creator_name or existing_creator_id})",
        )


class SkillVersionConflictError(Exception):
    """同步快照时 version_id 撞车（同 version_id 不同 signature）。

    F3 修复后，publish_skill 不再静默吞 ValueError，而是把它包成本异常
    抛到上层路由转 409，让前端能看到本次同步未产生新快照。
    """


class MCPVersionConflictError(Exception):
    """MCP 同步快照时 version_id 撞车（同 version_id 不同 signature）。"""


class ExpertNameConflictError(ValueError):
    """专家同名冲突异常。"""

    def __init__(
        self,
        existing_item_id: str,
        existing_name: str,
        existing_creator_id: str = "",
        existing_creator_name: str = "",
        existing_version: str = "",
    ) -> None:
        self.existing_item_id = existing_item_id
        self.existing_name = existing_name
        self.existing_creator_id = existing_creator_id
        self.existing_creator_name = existing_creator_name
        self.existing_version = existing_version
        super().__init__(
            f"Expert with name '{existing_name}' already exists "
            f"(created by {existing_creator_name or existing_creator_id})",
        )


class ExpertDependencyError(ValueError):
    """专家声明依赖缺失异常。"""


_BINARY_PREVIEW_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".pdf",
    ".ico",
    ".bmp",
}

_TRACING_STATS_SQL = """
    SELECT
        COUNT(*) AS call_count,
        COUNT(DISTINCT user_id) AS user_count
    FROM swe_tracing_spans
    WHERE event_type = 'skill_invocation'
      AND skill_name = %s
      AND source_id = %s
"""

_TRACING_USER_STATS_SQL = """
    SELECT
        user_id,
        MAX(COALESCE(user_name, '')) AS user_name,
        COUNT(*) AS call_count
    FROM swe_tracing_spans
    WHERE event_type = 'skill_invocation'
      AND skill_name = %s
      AND source_id = %s
    GROUP BY user_id
    ORDER BY call_count DESC
    LIMIT 100
"""

# MCP 专用统计 SQL - 使用 mcp_server 字段匹配 client_key
_TRACING_STATS_MCP_SQL = """
    SELECT
        COUNT(*) AS call_count,
        COUNT(DISTINCT user_id) AS user_count
    FROM swe_tracing_spans
    WHERE mcp_server = %s
      AND source_id = %s
"""

_TRACING_USER_STATS_MCP_SQL = """
    SELECT
        user_id,
        MAX(COALESCE(user_name, '')) AS user_name,
        COUNT(*) AS call_count
    FROM swe_tracing_spans
    WHERE mcp_server = %s
      AND source_id = %s
    GROUP BY user_id
    ORDER BY call_count DESC
    LIMIT 100
"""

_LOG_MARKET_OP_SQL = """
    INSERT INTO swe_marketplace_operation_logs
        (source_id, operator_id, operator_name, operation,
         item_type, item_id, item_name,
         target_user_id, target_user_name, target_bbk_id)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_QUERY_USERS_BY_SOURCE_SQL = """
    SELECT tenant_id, tenant_name, bbk_id
    FROM swe_tenant_init_source
    WHERE source_id = %s
"""

_QUERY_USERS_BY_BBK_SQL = """
    SELECT tenant_id, tenant_name, bbk_id
    FROM swe_tenant_init_source
    WHERE source_id = %s AND bbk_id IN ({placeholders})
"""

_QUERY_USERS_BY_TENANT_IDS_SQL = """
    SELECT tenant_id, tenant_name, bbk_id
    FROM swe_tenant_init_source
    WHERE source_id = %s AND tenant_id IN ({placeholders})
"""

_QUERY_DISTRIBUTIONS_SQL = """
    SELECT target_user_id, target_user_name, target_bbk_id, created_at
    FROM swe_marketplace_operation_logs
    WHERE source_id = %s AND item_id = %s AND item_type = %s AND operation = 'distribute'
    ORDER BY created_at DESC
"""

# 查询用户技能持有状态
_QUERY_USER_SKILL_STATUS_SQL = """
SELECT tenant_id, tenant_name, bbk_id, source, version_text
FROM swe_skills
WHERE skill_name = %s AND source_id = %s AND tenant_id IN ({placeholders})
"""

# 查询已分发用户（从技能表，只统计当前实际持有的）
_QUERY_DISTRIBUTED_USERS_SQL = """
SELECT tenant_id, tenant_name, bbk_id
FROM swe_skills
WHERE skill_name = %s AND source_id = %s AND source LIKE 'marketplace:%%'
"""


def _sort_items_by_updated_at_desc(
    items: list[MarketItem],
) -> list[MarketItem]:
    """按更新时间倒序排列，缺失时回退到创建时间。"""

    def sort_key(item: MarketItem) -> tuple[int, str]:
        timestamp = item.updated_at or item.created_at or ""
        return (1 if timestamp else 0, timestamp)

    return sorted(items, key=sort_key, reverse=True)


def _bump_patch(version: str) -> str:
    """Increment patch version: '1.0.0' -> '1.0.1'（委托共享工具）."""
    return _shared_bump_patch(version)


def _next_expert_version(current_version: str, existing_ids: set[str]) -> str:
    """为专家生成下一个唯一补丁版本."""
    candidate = (
        "1.0.0" if not current_version else _bump_patch(current_version)
    )
    for _ in range(100):
        if candidate not in existing_ids:
            return candidate
        candidate = _bump_patch(candidate)
    return candidate


def _read_expert_definition(source_dir: Path) -> dict[str, Any]:
    """读取专家 definition.toml."""
    definition_path = source_dir / "definition.toml"
    if not definition_path.exists():
        raise ValueError("definition.toml not found")
    try:
        return tomllib.loads(definition_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"Invalid expert definition: {exc}") from exc


def _as_str_list(value: object) -> list[str]:
    """将 TOML 字段归一为字符串列表."""
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
    return result


def _extract_expert_dependencies(
    definition: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """提取专家声明的 Skills / MCP 依赖."""
    skills = _as_str_list(definition.get("skills"))
    mcps = _as_str_list(definition.get("mcps")) or _as_str_list(
        definition.get("mcp"),
    )

    dependencies = definition.get("dependencies")
    if isinstance(dependencies, dict):
        if not skills:
            skills = _as_str_list(dependencies.get("skills"))
        if not mcps:
            mcps = _as_str_list(dependencies.get("mcps")) or _as_str_list(
                dependencies.get("mcp"),
            )
    _validate_expert_dependency_names(skills, "skill")
    _validate_expert_dependency_names(mcps, "MCP")
    return skills, mcps


def _validate_expert_dependency_names(
    names: list[str],
    dependency_type: str,
) -> None:
    """Keep declared dependency names inside the package's private roots."""
    for name in names:
        if name in {".", ".."} or any(
            separator in name for separator in ("/", "\\", "\x00")
        ):
            raise ExpertDependencyError(
                f"Declared dependency {dependency_type} has an unsafe path: {name}",
            )


def _normalize_expert_mcp_config(
    config: dict[str, Any],
    mcp_name: str,
) -> dict[str, Any]:
    """Validate one frozen MCP while preserving its complete configuration."""
    normalized = normalize_mcp_config_data(config)
    normalized.setdefault("name", mcp_name)
    try:
        MCPClientConfig.model_validate(normalized)
    except ValueError as exc:
        raise ExpertDependencyError(
            f"Invalid bundled MCP config: {mcp_name}",
        ) from exc
    return normalized


def _copy_expert_package(source_dir: Path, target_dir: Path) -> None:
    """把专家包同步到目标目录，保留 versions/ 和 versions.json."""
    preserve = {"versions", "versions.json"}
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{target_dir.name}-", dir=target_dir.parent),
    )
    backup = target_dir.with_name(f".{target_dir.name}.backup")
    try:
        for entry in source_dir.iterdir():
            if entry.name in preserve:
                continue
            target = staging / entry.name
            if entry.is_dir():
                shutil.copytree(entry, target)
            else:
                shutil.copy2(entry, target)
        if target_dir.is_dir():
            for name in preserve:
                existing = target_dir / name
                if existing.is_dir():
                    shutil.copytree(existing, staging / name)
                elif existing.is_file():
                    shutil.copy2(existing, staging / name)
        if backup.exists():
            shutil.rmtree(backup)
        if target_dir.exists():
            os.replace(target_dir, backup)
        os.replace(staging, target_dir)
        if backup.exists():
            shutil.rmtree(backup)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        if not target_dir.exists() and backup.exists():
            os.replace(backup, target_dir)
        raise


def _community_toml(
    toml_text: str,
    item_id: str,
    version: str,
    fingerprint: str,
) -> str:
    """在本地专家 TOML 末尾写入社区来源元数据。"""
    toml_text = _without_community_toml(toml_text)
    suffix = "\n" if toml_text.endswith("\n") else "\n\n"
    return (
        toml_text
        + suffix
        + "[community]\n"
        + f"item_id = {json.dumps(item_id, ensure_ascii=False)}\n"
        + f"version = {json.dumps(version, ensure_ascii=False)}\n"
        + f"content_fingerprint = {json.dumps(fingerprint, ensure_ascii=False)}\n"
    )


def _without_community_toml(toml_text: str) -> str:
    """Remove received-community metadata before publishing a new source."""
    return re.sub(
        r"(?ms)\n\[community\]\n.*?(?=\n\[[^\]]+\]\n|\Z)",
        "\n",
        toml_text,
    )


def _community_ref_from_toml(path: Path) -> dict[str, str] | None:
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    value = payload.get("community")
    if not isinstance(value, dict):
        return None
    keys = ("item_id", "version", "content_fingerprint")
    if not all(isinstance(value.get(key), str) for key in keys):
        return None
    return {key: value[key] for key in keys}


def _decode_creator_name(value: str) -> str:
    """解码通过请求头传入的创建人名称，并兼容历史已编码数据。"""
    if not value:
        return value
    try:
        return unquote(value)
    except Exception:  # pylint: disable=broad-except
        return value


def _item_visible(item: MarketItem, user_bbk_id: str) -> bool:
    """Return True if item is active (bbk_ids is for attribution, not visibility)."""
    return item.status == "active"


def _preview_sort_key(path: Path) -> tuple[int, str]:
    """统一文件预览树排序，优先展示核心入口文件。"""
    if path.name == "SKILL.md":
        return (0, path.name.lower())
    if path.name == "skill.json":
        return (1, path.name.lower())
    if path.is_dir():
        return (2, path.name.lower())
    return (3, path.name.lower())


def _build_file_tree_entries(
    root: Path,
    hidden_files: set[str] | None = None,
) -> list[dict[str, Any]]:
    """构建文件树列表，路径统一为 POSIX 格式。"""
    hidden_files = hidden_files or set()
    if not root.exists():
        return []

    def build_tree(path: Path) -> dict[str, Any]:
        relative = path.relative_to(root).as_posix()
        if path.is_file():
            return {
                "name": path.name,
                "type": "file",
                "path": relative,
            }
        children = []
        for child in sorted(path.iterdir(), key=_preview_sort_key):
            if child.name.startswith(".") or child.name in hidden_files:
                continue
            children.append(build_tree(child))
        return {
            "name": path.name,
            "type": "directory",
            "path": relative,
            "children": children,
        }

    items = sorted(root.iterdir(), key=_preview_sort_key)
    return [
        build_tree(item)
        for item in items
        if not item.name.startswith(".") and item.name not in hidden_files
    ]


def _read_preview_file(root: Path, file_path: str) -> tuple[str | None, str]:
    """读取预览文件内容，返回内容与类型。"""
    target = (root / Path(file_path)).resolve()

    try:
        target.relative_to(root.resolve())
    except ValueError:
        return None, "error"

    if not target.exists() or not target.is_file():
        return None, "error"

    ext = target.suffix.lower()
    if ext == ".md":
        file_type = "markdown"
    elif ext == ".json":
        file_type = "json"
    elif ext in _BINARY_PREVIEW_SUFFIXES:
        return None, "binary"
    else:
        file_type = "text"

    try:
        content = target.read_text(encoding="utf-8")
        return content, file_type
    except UnicodeDecodeError:
        return None, "binary"
    except Exception:
        return None, "error"


def _parse_md_frontmatter(
    md_content: str,
    fallback_name: str,
) -> tuple[str, str]:
    """从 SKILL.md frontmatter 中提取 name 和 description."""
    try:
        end_idx = md_content.index("---", 3)
        fm_text = md_content[3:end_idx].strip()
    except ValueError:
        return fallback_name, ""

    name = fallback_name
    description = ""
    for line in fm_text.split("\n"):
        if ":" in line:
            key, val = line.split(":", 1)
            key = key.strip().lower()
            val = val.strip()
            if key == "name" and val:
                # 去除引号（复用公共工具函数）
                name = clean_skill_name(val)
            elif key == "description" and val:
                description = val
    return name, description


def _extract_version_from_frontmatter(md_content: str) -> str:
    """从 SKILL.md frontmatter 中提取 version（委托共享工具）."""
    return _extract_version_md(md_content)


def _upsert_skill_item(
    items: list[MarketItem],
    existing: MarketItem | None,
    req: PublishSkillRequest,
) -> MarketItem:
    """更新已有技能条目或创建新条目，返回更新/创建后的 item。"""
    now = datetime.now(timezone.utc).isoformat()
    # 使用 cn_name（前端传递的字段名）
    cn_name = req.cn_name or req.chinese_name
    if existing is not None:
        version = _bump_patch(existing.version)
        existing.version = version
        existing.chinese_name = cn_name
        existing.description = req.description
        existing.creator_id = req.creator_id
        existing.creator_name = req.creator_name
        existing.category_id = req.category_id
        existing.bbk_ids = req.bbk_ids
        existing.include_in_statistics = req.include_in_statistics
        # 直接使用请求中的 skill_id
        if req.skill_id:
            existing.skill_id = req.skill_id
        # 重新发布已下架技能时，更新 created_at 为当前时间
        if existing.status == "inactive":
            existing.created_at = now
        existing.status = "active"
        existing.updated_at = now
        return existing

    item = MarketItem(
        item_id=str(uuid.uuid4()),
        item_type="skill",
        name=req.name,
        skill_id=req.skill_id,
        chinese_name=cn_name,
        description=req.description,
        version="1.0.0",
        creator_id=req.creator_id,
        creator_name=req.creator_name,
        category_id=req.category_id,
        bbk_ids=req.bbk_ids,
        status="active",
        created_at=now,
        updated_at=now,
        include_in_statistics=req.include_in_statistics,
    )
    items.append(item)
    return item


def _copy_skill_files(
    req: PublishSkillRequest,
    skill_dir: Path,
    swe_root: Path,
    source_id: str,
) -> None:
    """将技能文件复制到市场目录。"""
    if req.skill_name:
        src_skill_dir = (
            get_user_skills_dir(
                swe_root,
                req.creator_id,
                req.agent_id,
                source_id,
            )
            / req.skill_name
        )
        if src_skill_dir.exists() and src_skill_dir.is_dir():
            # 删除旧目录，复制整个目录（保持与用户工作区一致）
            if skill_dir.exists():
                shutil.rmtree(skill_dir)
            shutil.copytree(src_skill_dir, skill_dir)
            # 删除复制过来的 skill.json（不再需要）
            skill_json_path = skill_dir / "skill.json"
            if skill_json_path.exists():
                try:
                    skill_json_path.unlink()
                except OSError:
                    pass
            logger.info(
                "Copied entire skill directory from %s to %s",
                src_skill_dir,
                skill_dir,
            )
        else:
            # 源目录不存在，只写入 SKILL.md
            logger.warning(
                "Source skill directory %s not found, falling back to SKILL.md only",
                src_skill_dir,
            )
            if req.skill_md:
                (skill_dir / "SKILL.md").write_text(
                    req.skill_md,
                    encoding="utf-8",
                    newline="",
                )
    else:
        # 未提供 skill_name，只写入 SKILL.md
        if req.skill_md:
            (skill_dir / "SKILL.md").write_text(
                req.skill_md,
                encoding="utf-8",
                newline="",
            )


def _build_skill_metadata_for_manifest(
    skill_dir: Path,
    skill_name: str,
    source: str = "customized",
) -> dict[str, Any]:
    """从技能目录构建 manifest 所需的 metadata 字段.

    只从 SKILL.md 读取基本信息（name、description、version），
    skill_id 和 cn_name 由调用方通过 extra_metadata 参数传入。
    """
    skill_md_path = skill_dir / "SKILL.md"
    name = skill_name
    description = ""
    version_text = ""

    # 从 SKILL.md 读取基本信息
    if skill_md_path.exists():
        try:
            md_content = skill_md_path.read_text(encoding="utf-8")
            name, description = _parse_md_frontmatter(md_content, skill_name)
            version_text = _extract_version_from_frontmatter(md_content)
        except OSError:
            pass

    now = datetime.now(timezone.utc).isoformat()

    return {
        "name": name,
        "description": description,
        "version_text": version_text or "1.0.0",
        "commit_text": "",
        "signature": "",
        "source": source,
        "protected": False,
        "requirements": {"require_bins": [], "require_envs": []},
        "updated_at": now,
    }


class MarketplaceService:
    def __init__(
        self,
        db: DatabaseConnection,
        marketplace_root: Path,
        swe_root: Path,
    ) -> None:
        self.db = db
        self.marketplace_root = marketplace_root
        self.swe_root = swe_root
        self.skill_registry = SkillRegistry(db)
        self.skill_scan_history_recorder: Any | None = None

    def _get_expert_version_service(self) -> ExpertVersionService:
        """获取社区专家版本服务."""
        return ExpertVersionService(self.marketplace_root)

    async def _log_expert_operation(
        self,
        source_id: str,
        operator_id: str,
        operator_name: str,
        operation: str,
        item: MarketItem,
        *,
        target_user_id: str = "",
        target_user_name: str = "",
        target_bbk_id: str = "",
    ) -> None:
        if not self.db.is_connected:
            return
        try:
            await self.db.execute(
                _LOG_MARKET_OP_SQL,
                (
                    source_id,
                    operator_id,
                    operator_name,
                    operation,
                    "expert",
                    item.item_id,
                    item.name,
                    target_user_id,
                    target_user_name,
                    target_bbk_id,
                ),
            )
        except Exception as exc:  # pragma: no cover - audit must not block ops
            logger.warning(
                "Failed to log expert operation %s: %s",
                operation,
                exc,
            )

    async def _trigger_agent_reload(
        self,
        user_id: str,
        agent_id: str = "default",
        source_id: str | None = None,
    ) -> bool:
        """通过 HTTP 回调触发 src/swe 的 Agent 重载."""
        url = f"{SWE_INTERNAL_URL}/api/internal/agents/{agent_id}/reload"
        headers = {}
        if SWE_INTERNAL_TOKEN:
            headers["X-Internal-Token"] = f"Bearer {SWE_INTERNAL_TOKEN}"

        params = {"tenant_id": user_id}
        if source_id:
            params["source_id"] = source_id

        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.post(
                        url,
                        params=params,
                        headers=headers,
                    )
                    if response.status_code == 200:
                        logger.info(
                            "Agent reload triggered for '%s' (tenant=%s, source=%s)",
                            agent_id,
                            user_id,
                            source_id,
                        )
                        return True
                    logger.warning(
                        "Agent reload failed on attempt %s: %s - %s",
                        attempt + 1,
                        response.status_code,
                        response.text,
                    )
            except Exception as exc:
                logger.warning(
                    "Failed to trigger agent reload on attempt %s: %s",
                    attempt + 1,
                    exc,
                )
        logger.error(
            "Agent reload remained unavailable after retries (tenant=%s, source=%s)",
            user_id,
            source_id,
        )
        return False

    def _scan_skill_or_raise(
        self,
        user_id: str,
        skill_name: str,
        agent_id: str = "default",
        source_id: str | None = None,
        bbk_id: str = "",
    ) -> None:
        """扫描技能目录，发现安全问题抛出异常."""
        skills_dir = get_user_skills_dir(
            self.swe_root,
            user_id,
            agent_id,
            source_id,
        )
        skill_dir = skills_dir / skill_name
        if skill_dir.exists():
            scan_skill_directory(
                skill_dir,
                skill_name=skill_name,
                source_id=source_id or "",
                user_id=user_id,
                bbk_id=bbk_id,
            )

    def register_skill_in_manifest(
        self,
        user_id: str,
        skill_name: str,
        agent_id: str = "default",
        source_id: str | None = None,
        enabled: bool = True,
        source: str = "customized",
        extra_metadata: dict | None = None,
        package_path: Path | None = None,
    ) -> bool:
        """注册技能到 manifest（用于上传/分发时记录）。

        写入完整的字段，与 src/swe 的 reconcile_workspace_manifest 保持一致：
        - enabled: 启用状态
        - channels: 通道配置
        - source: 技能来源
        - metadata: 元数据（name、description、version、creator_id 等）
        - requirements: 报备要求
        - config: 配置（保留已有）
        - created_at/updated_at: 时间戳

        Args:
            extra_metadata: 额外的 metadata 字段（如 creator_id、creator_name、bbk_id）
        """

        # 获取技能目录，用于构建 metadata
        skills_dir = get_user_skills_dir(
            self.swe_root,
            user_id,
            agent_id,
            source_id,
        )
        skill_dir = package_path or skills_dir / skill_name

        def _update(payload: dict) -> bool:
            skills_dict = payload.setdefault("skills", {})
            existing = skills_dict.get(skill_name) or {}

            # 构建 metadata（从 SKILL.md 和 skill.json 读取）
            existing_metadata = existing.get("metadata")
            metadata = (
                dict(existing_metadata)
                if isinstance(existing_metadata, dict)
                else {}
            )
            metadata.update(
                _build_skill_metadata_for_manifest(
                    skill_dir,
                    skill_name,
                    source=source,
                ),
            )

            # 合并额外的 metadata（上传时传入的 creator_id、name 等）
            if extra_metadata:
                for key, value in extra_metadata.items():
                    # 允许 name 字段覆盖（用户重命名时指定的新名称）
                    if key == "name" and value:
                        metadata[key] = value
                    # 不覆盖其他核心字段
                    elif key not in ["description", "source"]:
                        metadata[key] = value
            # 分发技能：version_text 使用市场版本号而非 SKILL.md 的默认值
            if (
                source.startswith("marketplace:")
                and extra_metadata
                and extra_metadata.get("received_version")
            ):
                metadata["version_text"] = extra_metadata["received_version"]

            # 保留已有的 channels
            existing_channels = existing.get("channels") or ["all"]

            now = datetime.now(timezone.utc).isoformat()

            entry = dict(existing)
            entry.update(
                {
                    "enabled": enabled,
                    "channels": existing_channels,
                    "source": source,
                    "metadata": metadata,
                    "requirements": metadata["requirements"],
                    "updated_at": now,
                },
            )

            # 按原值保留已有 config，包括空字典或 None
            if "config" in existing:
                entry["config"] = existing["config"]

            # 保留已有的 created_at（首次注册时写入）
            entry["created_at"] = existing.get("created_at") or now

            skills_dict[skill_name] = entry
            return True

        result = mutate_user_skill_manifest(
            self.swe_root,
            user_id,
            agent_id,
            _update,
            source_id,
        )
        # 打印日志，记录实际更新的目录路径（user_id 是 base64 编码，需要知道实际目录）
        logger.info(
            "Register skill in manifest: skills_dir=%s, skill_name=%s, source=%s",
            skills_dir,
            skill_name,
            source,
        )
        return result

    async def enable_skill(
        self,
        user_id: str,
        skill_name: str,
        agent_id: str = "default",
        source_id: str | None = None,
        bbk_id: str = "",
    ) -> dict[str, Any]:
        """启用技能（含安全扫描 + 回调重载）.

        安全扫描策略：
        - 如果技能已在 manifest 中注册（之前已启用过），重新启用时跳过安全扫描，
          因为内容已受信任。禁用再启用是用户的常规操作，不应被扫描阻断。
        - 如果技能未在 manifest 中注册（首次启用），则执行安全扫描。
        """
        # 检查技能是否已在 manifest 中注册（之前已启用过）
        manifest = read_user_skill_manifest(
            self.swe_root,
            user_id,
            agent_id,
            source_id,
        )
        already_registered = skill_name in manifest.get("skills", {})
        skills_dir = get_user_skills_dir(
            self.swe_root,
            user_id,
            agent_id,
            source_id,
        )
        skill_dir = skills_dir / skill_name
        if not skill_dir.exists() and already_registered:
            skill_dir = (
                get_user_disabled_skills_dir(
                    self.swe_root,
                    user_id,
                    agent_id,
                    source_id,
                )
                / skill_name
            )
        if not skill_dir.exists():
            return {"success": False, "reason": "not_found"}

        # 仅对首次启用的技能执行安全扫描（已注册的技能重新启用时跳过）
        if not already_registered:
            try:
                self._scan_skill_or_raise(
                    user_id,
                    skill_name,
                    agent_id,
                    source_id,
                    bbk_id,
                )
            except SkillScanError as e:
                await self.flush_skill_scan_history()
                return {
                    "success": False,
                    "reason": "security_scan_failed",
                    "detail": str(e),
                }

        # 更新 manifest
        moved_from: Path | None = None
        moved_to: Path | None = None

        def _update(payload: dict) -> bool:
            nonlocal moved_from, moved_to
            registered_entry = payload.get("skills", {}).get(skill_name)
            entry = payload.setdefault("skills", {}).setdefault(skill_name, {})
            if registered_entry is not None:
                active_dir = skills_dir / skill_name
                disabled_dir = (
                    get_user_disabled_skills_dir(
                        self.swe_root,
                        user_id,
                        agent_id,
                        source_id,
                    )
                    / skill_name
                )
                if active_dir.exists() and disabled_dir.exists():
                    return False
                if not disabled_dir.exists():
                    if not active_dir.exists():
                        return False
                else:
                    active_dir.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.move(disabled_dir, active_dir)
                    except OSError:
                        return False
                    moved_from = disabled_dir
                    moved_to = active_dir
            entry["enabled"] = True
            entry["updated_at"] = datetime.now(timezone.utc).isoformat()
            return True

        def _rollback_move() -> None:
            if (
                moved_from is not None
                and moved_to is not None
                and moved_to.exists()
            ):
                moved_from.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(moved_to, moved_from)

        updated = mutate_user_skill_manifest(
            self.swe_root,
            user_id,
            agent_id,
            _update,
            source_id,
            rollback_fn=_rollback_move,
        )

        if updated:
            await self._trigger_agent_reload(user_id, agent_id, source_id)
            # 更新数据库 swe_skills 表
            await self.skill_registry.update_skill(
                user_id=user_id,
                skill_name=skill_name,
                source_id=source_id or "",
                enabled=True,
            )

        return {"success": updated}

    async def disable_skill(
        self,
        user_id: str,
        skill_name: str,
        agent_id: str = "default",
        source_id: str | None = None,
    ) -> dict[str, Any]:
        """禁用技能（含回调重载）."""

        def _update(payload: dict) -> bool:
            entry = payload.get("skills", {}).get(skill_name)
            if entry is None:
                return False
            workspace_dir = get_user_skill_manifest_path(
                self.swe_root,
                user_id,
                agent_id,
                source_id,
            ).parent
            resolved = resolve_registered_skill_path(
                workspace_dir,
                skill_name,
                entry,
            )
            skill_dir = resolved.path
            disabled_dir = (
                get_user_disabled_skills_dir(
                    self.swe_root,
                    user_id,
                    agent_id,
                    source_id,
                )
                / skill_name
            )
            if skill_dir is None:
                return False
            if skill_dir != disabled_dir:
                if disabled_dir.exists():
                    return False
                disabled_dir.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.move(skill_dir, disabled_dir)
                except OSError:
                    return False
            entry["enabled"] = False
            entry["updated_at"] = datetime.now(timezone.utc).isoformat()
            return True

        updated = mutate_user_skill_manifest(
            self.swe_root,
            user_id,
            agent_id,
            _update,
            source_id,
        )

        if updated:
            await self._trigger_agent_reload(user_id, agent_id, source_id)
            # 更新数据库 swe_skills 表
            await self.skill_registry.update_skill(
                user_id=user_id,
                skill_name=skill_name,
                source_id=source_id or "",
                enabled=False,
            )

        return {"success": updated}

    async def batch_delete_skills(
        self,
        user_id: str,
        skill_names: list[str],
        agent_id: str = "default",
        source_id: str | None = None,
    ) -> dict[str, Any]:
        """批量删除技能."""
        results: dict[str, Any] = {}

        for skill_name in skill_names:
            disabled = await self.disable_skill(
                user_id,
                skill_name,
                agent_id,
                source_id,
            )
            if not disabled["success"]:
                results[skill_name] = {"success": False, "reason": "not_found"}
                continue

            deleted = await self.delete_skill(
                user_id,
                skill_name,
                agent_id,
                source_id,
            )
            if deleted:
                results[skill_name] = {"success": True}
                continue
            results[skill_name] = {"success": False, "reason": "not_found"}

        return results

    async def batch_enable_skills(
        self,
        user_id: str,
        skill_names: list[str],
        agent_id: str = "default",
        source_id: str | None = None,
        bbk_id: str = "",
    ) -> dict[str, Any]:
        """批量启用技能."""
        results: dict[str, Any] = {}
        for skill_name in skill_names:
            results[skill_name] = await self.enable_skill(
                user_id,
                skill_name,
                agent_id,
                source_id,
                bbk_id,
            )
        return results

    async def flush_skill_scan_history(self) -> None:
        """Wait for accepted scan history writes, if a writer is installed."""
        recorder = getattr(self, "skill_scan_history_recorder", None)
        if recorder is not None:
            await recorder.flush()

    async def batch_disable_skills(
        self,
        user_id: str,
        skill_names: list[str],
        agent_id: str = "default",
        source_id: str | None = None,
    ) -> dict[str, Any]:
        """批量禁用技能."""
        results: dict[str, Any] = {}
        for skill_name in skill_names:
            results[skill_name] = await self.disable_skill(
                user_id,
                skill_name,
                agent_id,
                source_id,
            )
        return results

    async def publish_skill(
        self,
        source_id: str,
        req: PublishSkillRequest,
        operator_id: str = "",
        operator_name: str = "",
    ) -> tuple[MarketItem, bool]:
        """上架技能。同名 → 续接到现有 MarketItem（R4）.

        Args:
            operator_id / operator_name: 真正点按钮的人（admin 的 X-User-Id），用于
                version 快照里的 created_by；未传时退化为 req.creator_*（向后兼容）。

        Returns:
            (MarketItem, version_unchanged): 商品条目与版本是否未变化的标志。

        如果请求中包含 skill_name，则从用户工作区复制整个技能目录到市场。
        否则使用 skill_json 和 skill_md 字段创建目录。
        """
        items = load_index(self.marketplace_root, source_id)
        existing = next((i for i in items if i.name == req.name), None)

        # R4: 同名 → 续接到现有 MarketItem，但需先确认覆盖意图
        # 未显式 overwrite 时抛冲突异常，由前端弹窗让用户确认
        if existing is not None and not req.overwrite:
            raise SkillNameConflictError(
                existing_item_id=existing.item_id,
                existing_name=existing.name,
                existing_creator_id=existing.creator_id,
                existing_creator_name=existing.creator_name,
                existing_version=existing.version,
            )

        item = _upsert_skill_item(items, existing, req)

        skill_dir = get_skill_dir(
            self.marketplace_root,
            source_id,
            item.item_id,
        )
        skill_dir.mkdir(parents=True, exist_ok=True)

        _copy_skill_files(req, skill_dir, self.swe_root, source_id)

        # 注：F1 修复——市场端版本号独立于用户 SKILL.md（spec R3）。
        # 此前这里会用 SKILL.md 中的 version 覆盖 item.version，破坏 R3。
        # 现保留 _upsert_skill_item 决定的 item.version（首发 1.0.0、续接 _bump_patch）。
        # 用户那一侧的 version 仅作为 source_user_version 写入快照元数据。

        # 创建版本快照
        # source_user_*：内容来源是 req.creator_*（PublishSkillRequest 显式指定）
        # created_by_*：操作者（admin），未传则与 source_user 相同（向后兼容）
        # source_user_version 优先使用请求中传入的值（前端从 manifest 获取），
        # 其次从 SKILL.md frontmatter 提取（兼容旧调用方或不传的场景）。
        source_user_version = req.source_user_version
        if not source_user_version:
            skill_md_path = skill_dir / "SKILL.md"
            if skill_md_path.exists():
                try:
                    source_user_version = _extract_version_md(
                        skill_md_path.read_text(encoding="utf-8"),
                    )
                except OSError:
                    pass

        version_svc = SkillVersionService(self.marketplace_root)
        version_unchanged = False
        try:
            snapshot = version_svc.create_version_snapshot(
                source_id=source_id,
                item_id=item.item_id,
                skill_dir=skill_dir,
                description="",  # F2 修复：留空，让 version_service 按"首次上传/diff 统计"自动生成；避免与头部版本号重复
                creator=operator_id or req.creator_id,
                creator_name=operator_name or req.creator_name,
                current_market_version=item.version,
                source_user_id=req.creator_id,
                source_user_name=req.creator_name,
                source_user_version=source_user_version,
            )
            # F1+F2：让 MarketItem.version 严格跟随 is_current 快照的 version_id。
            # 当 _derive_market_version_id 走到"内容未变 → 复用历史 version_id"
            # 分支时，item.version 之前已被 _upsert_skill_item bump 但应回滚；
            # 当走到"内容变 → bump"且 _bump_patch 与 _bump_version 因边界不同
            # 而结果不一致时，以快照的 version_id 为准。
            if snapshot.version_id and snapshot.version_id != item.version:
                # 版本被回滚 = R7 no-op（内容未变）
                version_unchanged = True
                item.version = snapshot.version_id
            save_index(self.marketplace_root, source_id, items)
        except ValueError as e:
            # 同 version_id 不同 signature 的罕见碰撞 → 回滚 items 并抛 409
            # 让前端可见，避免悄无声息地丢失同步动作（修问题 2）
            logger.warning(
                "Version snapshot conflict for skill %s: %s",
                item.item_id,
                e,
            )
            raise SkillVersionConflictError(str(e)) from e
        except Exception as e:
            # 其他异常：升到 ERROR 级，但仍持久化 item.version（保持原行为最小破坏）
            logger.error(
                "Failed to create version snapshot for skill %s: %s",
                item.item_id,
                e,
                exc_info=True,
            )
            save_index(self.marketplace_root, source_id, items)

        if self.db.is_connected:
            try:
                await self.db.execute(
                    _LOG_MARKET_OP_SQL,
                    (
                        source_id,
                        req.creator_id,
                        req.creator_name,
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

        # 同步写入 swe_marketplace_skills 表
        if self.db.is_connected:
            try:
                from market.marketplace.market_skill_registry import (
                    MarketSkillRegistry,
                )

                registry = MarketSkillRegistry(self.db)
                await registry.upsert_market_skill(
                    source_id=source_id,
                    item_id=item.item_id,
                    skill_id=item.skill_id,
                    skill_name=item.name,
                    cn_name=item.chinese_name,
                    include_in_statistics=item.include_in_statistics,
                    creator_id=item.creator_id,
                    creator_name=item.creator_name,
                    updator_id=operator_id or item.creator_id,
                    updator_name=operator_name or item.creator_name,
                )
            except Exception as e:
                logger.warning("Failed to upsert market skill: %s", e)

        return item, version_unchanged

    async def unpublish_skill(
        self,
        source_id: str,
        item_id: str,
        operator_id: str,
        operator_name: str,
    ) -> bool:
        """下架技能（设为 inactive）。返回 True 表示成功。"""
        items = load_index(self.marketplace_root, source_id)
        item = next(
            (
                i
                for i in items
                if i.item_id == item_id and i.item_type == "skill"
            ),
            None,
        )
        if item is None:
            return False
        item.status = "inactive"
        item.updated_at = datetime.now(timezone.utc).isoformat()
        save_index(self.marketplace_root, source_id, items)

        # 同步删除 swe_marketplace_skills 表中的记录
        if self.db.is_connected:
            try:
                await self.db.execute(
                    "DELETE FROM swe_marketplace_skills WHERE source_id = %s AND item_id = %s",
                    (source_id, item_id),
                )
            except Exception as e:
                logger.warning(
                    "Failed to delete from swe_marketplace_skills: %s",
                    e,
                )

        if self.db.is_connected:
            try:
                await self.db.execute(
                    _LOG_MARKET_OP_SQL,
                    (
                        source_id,
                        operator_id,
                        operator_name,
                        "unpublish",
                        "skill",
                        item_id,
                        item.name,
                        None,
                        None,
                        None,
                    ),
                )
            except Exception as e:
                logger.warning("Failed to log unpublish operation: %s", e)

        return True

    async def list_expert_items(
        self,
        source_id: str,
        user_bbk_id: str,
        category_id: Optional[int] = None,
        bbk_ids: Optional[list[str]] = None,
    ) -> list[MarketExpertResponse]:
        """列出市场社区专家."""
        items = load_index(self.marketplace_root, source_id)
        expert_items = [
            item
            for item in items
            if item.item_type == "expert" and item.status == "active"
        ]
        expert_items = _sort_items_by_updated_at_desc(expert_items)

        if category_id is not None:
            expert_items = [
                item
                for item in expert_items
                if item.category_id == category_id
            ]
        if bbk_ids is not None and len(bbk_ids) > 0:
            expert_items = [
                item
                for item in expert_items
                if item.bbk_ids and any(bbk in item.bbk_ids for bbk in bbk_ids)
            ]

        return [
            MarketExpertResponse(
                item_id=item.item_id,
                name=item.name,
                description=item.description,
                version=item.version,
                creator_id=item.creator_id,
                creator_name=_decode_creator_name(item.creator_name),
                category_id=item.category_id,
                bbk_ids=item.bbk_ids,
                status=item.status,
                created_at=item.created_at,
                updated_at=item.updated_at,
            )
            for item in expert_items
        ]

    async def get_expert_detail(
        self,
        source_id: str,
        item_id: str,
        user_bbk_id: str,
    ) -> MarketExpertDetail | None:
        """获取社区专家详情."""
        items = load_index(self.marketplace_root, source_id)
        item = next(
            (
                current
                for current in items
                if current.item_id == item_id and current.item_type == "expert"
            ),
            None,
        )
        if item is None or not _item_visible(item, user_bbk_id):
            return None

        version_svc = self._get_expert_version_service()
        versions = version_svc.list_versions(source_id, item_id)

        definition: dict[str, Any] = {}
        definition_path = get_expert_definition_path(
            self.marketplace_root,
            source_id,
            item_id,
        )
        if definition_path.exists():
            try:
                definition = tomllib.loads(
                    definition_path.read_text(encoding="utf-8"),
                )
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
                definition = {}

        return MarketExpertDetail(
            item_id=item.item_id,
            name=item.name,
            description=item.description,
            version=item.version,
            creator_id=item.creator_id,
            creator_name=_decode_creator_name(item.creator_name),
            category_id=item.category_id,
            bbk_ids=item.bbk_ids,
            status=item.status,
            created_at=item.created_at,
            updated_at=item.updated_at,
            versions=versions.get("versions", []),
            definition=definition,
        )

    @staticmethod
    def _parse_expert_publish_metadata(
        definition: dict[str, Any],
    ) -> dict[str, Any]:
        expert_name = str(definition.get("name", "")).strip()
        if not expert_name:
            raise ValueError("Expert name is required")
        creator_id = str(definition.get("creator_id", "")).strip()
        if not creator_id:
            raise ValueError("creator_id is required")
        category_id = definition.get("category_id")
        if category_id is not None and not isinstance(category_id, int):
            raise ValueError("category_id must be an integer")
        raw_bbk_ids = definition.get("bbk_ids", [])
        bbk_ids = (
            [str(value).strip() for value in raw_bbk_ids if str(value).strip()]
            if isinstance(raw_bbk_ids, list)
            else []
        )
        declared_skills, declared_mcps = _extract_expert_dependencies(
            definition,
        )
        return {
            "name": expert_name,
            "creator_id": creator_id,
            "creator_name": str(definition.get("creator_name", "")).strip(),
            "description": str(definition.get("description", "")).strip(),
            "category_id": category_id,
            "bbk_ids": bbk_ids,
            "declared_skills": declared_skills,
            "declared_mcps": declared_mcps,
        }

    @staticmethod
    def _scan_and_validate_expert_dependencies(
        source_dir: Path,
        declared_skills: list[str],
        declared_mcps: list[str],
    ) -> list[dict[str, Any]]:
        skills_root = source_dir / "skills"
        skill_dirs = (
            sorted(skills_root.iterdir()) if skills_root.is_dir() else []
        )
        scan_results: list[dict[str, Any]] = []
        for skill_dir in (path for path in skill_dirs if path.is_dir()):
            skill_name = skill_dir.name
            if not (skill_dir / "SKILL.md").is_file():
                raise ExpertDependencyError(
                    f"Missing declared dependency skill: {skill_name}",
                )
            scan_result = scan_skill_directory(
                skill_dir,
                skill_name=skill_name,
            )
            if scan_result is not None:
                scan_results.append(scan_result.to_dict())
        for skill_name in declared_skills:
            if not (skills_root / skill_name).is_dir():
                raise ExpertDependencyError(
                    f"Missing declared dependency skill: {skill_name}",
                )
        for mcp_name in declared_mcps:
            mcp_json = source_dir / "mcp" / mcp_name / "mcp.json"
            try:
                config = json.loads(mcp_json.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ExpertDependencyError(
                    f"Invalid bundled MCP config: {mcp_name}",
                ) from exc
            if not isinstance(config, dict):
                raise ExpertDependencyError(
                    f"Invalid bundled MCP config: {mcp_name}",
                )
            _normalize_expert_mcp_config(config, mcp_name)
        return scan_results

    def _upsert_expert_item(
        self,
        metadata: dict[str, Any],
        overwrite: bool,
        items: list[MarketItem],
    ) -> MarketItem:
        existing = next(
            (
                item
                for item in items
                if item.item_type == "expert" and item.name == metadata["name"]
            ),
            None,
        )
        if existing is not None and not overwrite:
            raise ExpertNameConflictError(
                existing_item_id=existing.item_id,
                existing_name=existing.name,
                existing_creator_id=existing.creator_id,
                existing_creator_name=existing.creator_name,
                existing_version=existing.version,
            )
        if existing is not None:
            return existing
        now = datetime.now(timezone.utc).isoformat()
        item = MarketItem(
            item_id=str(uuid.uuid4()),
            item_type="expert",
            name=metadata["name"],
            description=metadata["description"],
            version="1.0.0",
            creator_id=metadata["creator_id"],
            creator_name=metadata["creator_name"],
            category_id=metadata["category_id"],
            bbk_ids=metadata["bbk_ids"],
            status="active",
            created_at=now,
            updated_at=now,
        )
        items.append(item)
        return item

    @staticmethod
    def _update_expert_item(
        item: MarketItem,
        metadata: dict[str, Any],
        version: str,
        updated_at: str,
    ) -> None:
        item.version = version
        item.description = metadata["description"]
        item.creator_id = metadata["creator_id"]
        item.creator_name = metadata["creator_name"]
        item.category_id = metadata["category_id"]
        item.bbk_ids = metadata["bbk_ids"]
        item.status = "active"
        item.updated_at = updated_at

    async def _save_published_expert_version(
        self,
        source_id: str,
        items: list[MarketItem],
        item: MarketItem,
        expert_root: Path,
        metadata: dict[str, Any],
        scan_results: list[dict[str, Any]],
        operator_id: str,
        operator_name: str,
    ) -> tuple[MarketItem, bool]:
        now = datetime.now(timezone.utc).isoformat()
        version_svc = self._get_expert_version_service()
        signature = version_svc.calculate_signature(expert_root)
        (expert_root / "scan_result.json").write_text(
            json.dumps({"skills": scan_results}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        manifest = version_svc._load_versions_manifest(source_id, item.item_id)
        current_version = next(
            (version for version in manifest.versions if version.is_current),
            None,
        )
        if current_version and current_version.signature == signature:
            self._update_expert_item(
                item,
                metadata,
                current_version.version_id,
                now,
            )
            unchanged = True
        else:
            existing_ids = {
                version.version_id for version in manifest.versions
            }
            version_id = (
                "1.0.0"
                if not manifest.versions
                else _next_expert_version(item.version, existing_ids)
            )
            snapshot = version_svc.create_version_snapshot(
                source_id=source_id,
                item_id=item.item_id,
                source_dir=expert_root,
                version_id=version_id,
                expert_name=metadata["name"],
                creator=operator_id or metadata["creator_id"],
                creator_name=operator_name or metadata["creator_name"],
                description="",
                signature=signature,
            )
            self._update_expert_item(item, metadata, snapshot.version_id, now)
            unchanged = False
        save_index(self.marketplace_root, source_id, items)
        await self._log_expert_operation(
            source_id,
            operator_id,
            operator_name,
            "publish",
            item,
        )
        return item, unchanged

    async def publish_expert(
        self,
        source_id: str,
        source_dir: Path,
        operator_id: str = "",
        operator_name: str = "",
        overwrite: bool = False,
    ) -> tuple[MarketItem, bool]:
        """发布社区专家."""
        source_dir = Path(source_dir)
        metadata = self._parse_expert_publish_metadata(
            _read_expert_definition(source_dir),
        )
        scan_results = self._scan_and_validate_expert_dependencies(
            source_dir,
            metadata["declared_skills"],
            metadata["declared_mcps"],
        )
        items = load_index(self.marketplace_root, source_id)
        item = self._upsert_expert_item(metadata, overwrite, items)
        expert_root = get_expert_dir(
            self.marketplace_root,
            source_id,
            item.item_id,
        )
        _copy_expert_package(source_dir, expert_root)
        definition_path = expert_root / "definition.toml"
        definition_path.write_text(
            _without_community_toml(
                definition_path.read_text(encoding="utf-8"),
            ),
            encoding="utf-8",
        )
        (expert_root / "scan_result.json").unlink(missing_ok=True)
        return await self._save_published_expert_version(
            source_id,
            items,
            item,
            expert_root,
            metadata,
            scan_results,
            operator_id,
            operator_name,
        )

    def _load_profile_expert(
        self,
        user_id: str,
        agent_id: str,
        source_id: str,
        definition_id: str,
    ) -> tuple[Path, dict[str, Any], str, Path]:
        _validate_path_segment(definition_id, "definition_id")
        expert_dir = get_user_expert_dir(
            self.swe_root,
            user_id,
            agent_id,
            source_id,
        )
        definition_path = expert_dir / f"{definition_id}.toml"
        if not definition_path.is_file():
            raise ValueError("expert definition not found")
        try:
            definition_text = definition_path.read_text(encoding="utf-8")
            definition = tomllib.loads(definition_text)
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ValueError("expert definition is invalid") from exc
        return definition_path, definition, definition_text, expert_dir

    @staticmethod
    def _profile_definition_text(
        definition_text: str,
        user_id: str,
        creator_name: str,
        category_id: int | None,
        bbk_ids: list[str] | None,
    ) -> str:
        fields = [
            f"creator_id = {json.dumps(user_id, ensure_ascii=False)}",
            f"creator_name = {json.dumps(creator_name, ensure_ascii=False)}",
        ]
        if category_id is not None:
            fields.append(f"category_id = {category_id}")
        if bbk_ids:
            fields.append(
                f"bbk_ids = {json.dumps(bbk_ids, ensure_ascii=False)}",
            )
        for field in fields:
            field_name = field.split(" = ", 1)[0]
            pattern = rf"(?m)^{re.escape(field_name)}\s*=.*$"
            if re.search(pattern, definition_text):
                definition_text = re.sub(
                    pattern,
                    field,
                    definition_text,
                    count=1,
                )
            else:
                definition_text = field + "\n" + definition_text
        return definition_text

    @staticmethod
    def _copy_profile_skills(
        source_dir: Path,
        workspace_dir: Path,
        frozen_dir: Path,
        declared_skills: list[str],
    ) -> None:
        for skill_name in declared_skills:
            frozen_skill = frozen_dir / "skills" / skill_name
            source_skill = (
                frozen_skill
                if frozen_skill.is_dir()
                else workspace_dir / "skills" / skill_name
            )
            if not source_skill.is_dir():
                raise ExpertDependencyError(
                    f"Missing declared dependency skill: {skill_name}",
                )
            shutil.copytree(source_skill, source_dir / "skills" / skill_name)

    @staticmethod
    def _read_json_object(path: Path, error_message: str) -> dict[str, Any]:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExpertDependencyError(error_message) from exc
        return loaded if isinstance(loaded, dict) else {}

    @classmethod
    def _copy_profile_mcps(
        cls,
        source_dir: Path,
        workspace_dir: Path,
        frozen_dir: Path,
        declared_mcps: list[str],
    ) -> None:
        frozen_config = frozen_dir / "mcp" / "config.json"
        mcp_payload = (
            cls._read_json_object(frozen_config, "Invalid frozen MCP config")
            if frozen_config.is_file()
            else {}
        )
        if not declared_mcps:
            return
        agent_config_path = workspace_dir / "agent.json"
        agent_payload = (
            cls._read_json_object(
                agent_config_path,
                "Agent profile MCP config is invalid",
            )
            if agent_config_path.is_file()
            else {}
        )
        clients = (agent_payload.get("mcp") or {}).get("clients") or {}
        for mcp_name in declared_mcps:
            mcp_file = frozen_dir / "mcp" / mcp_name / "mcp.json"
            config = mcp_payload.get(mcp_name) or clients.get(mcp_name)
            if not isinstance(config, dict) and mcp_file.is_file():
                try:
                    config = json.loads(mcp_file.read_text(encoding="utf-8"))
                except (
                    OSError,
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                ) as exc:
                    raise ExpertDependencyError(
                        f"Invalid declared dependency MCP: {mcp_name}",
                    ) from exc
            if not isinstance(config, dict):
                raise ExpertDependencyError(
                    f"Missing declared dependency MCP: {mcp_name}",
                )
            target = source_dir / "mcp" / mcp_name
            target.mkdir()
            (target / "mcp.json").write_text(
                json.dumps(config, ensure_ascii=False),
                encoding="utf-8",
            )

    async def publish_expert_from_profile(
        self,
        source_id: str,
        user_id: str,
        agent_id: str,
        definition_id: str,
        *,
        category_id: int | None = None,
        bbk_ids: list[str] | None = None,
        creator_name: str = "",
        overwrite: bool = False,
    ) -> tuple[MarketItem, bool]:
        """Publish one Agent Profile expert without accepting arbitrary paths."""
        _, definition, definition_text, expert_dir = self._load_profile_expert(
            user_id,
            agent_id,
            source_id,
            definition_id,
        )
        declared_skills, declared_mcps = _extract_expert_dependencies(
            definition,
        )
        received_variant = isinstance(definition.get("community"), dict)
        workspace_dir = expert_dir.parent
        with tempfile.TemporaryDirectory(prefix="expert-publish-") as temp_dir:
            source_dir = Path(temp_dir)
            source_dir.joinpath("skills").mkdir()
            source_dir.joinpath("mcp").mkdir()
            definition_text = self._profile_definition_text(
                definition_text,
                user_id,
                creator_name,
                category_id,
                bbk_ids,
            )
            (source_dir / "definition.toml").write_text(
                definition_text,
                encoding="utf-8",
            )
            frozen_dir = expert_dir / f"{definition_id}.dependencies"
            self._copy_profile_skills(
                source_dir,
                workspace_dir,
                frozen_dir,
                declared_skills,
            )
            self._copy_profile_mcps(
                source_dir,
                workspace_dir,
                frozen_dir,
                declared_mcps,
            )
            return await self.publish_expert(
                source_id,
                source_dir,
                operator_id=user_id,
                operator_name=creator_name,
                # A received expert is a new source if re-shared.  It must
                # never overwrite the community item it originated from.
                overwrite=overwrite and not received_variant,
            )

    async def restore_expert_version(
        self,
        source_id: str,
        item_id: str,
        version_id: str,
        operator_id: str = "",
        operator_name: str = "",
    ) -> MarketItem:
        """恢复历史专家版本为当前版本."""
        items = load_index(self.marketplace_root, source_id)
        item = next(
            (
                current
                for current in items
                if current.item_id == item_id and current.item_type == "expert"
            ),
            None,
        )
        if item is None:
            raise ValueError(f"Expert item {item_id} not found")

        expert_root = get_expert_dir(self.marketplace_root, source_id, item_id)
        version_svc = self._get_expert_version_service()
        version_svc.restore_version(
            source_id,
            item_id,
            version_id,
            expert_root,
        )

        item.version = version_id
        item.status = "active"
        item.updated_at = datetime.now(timezone.utc).isoformat()
        save_index(self.marketplace_root, source_id, items)
        await self._log_expert_operation(
            source_id,
            operator_id,
            operator_name,
            "restore",
            item,
        )
        return item

    async def unpublish_expert(
        self,
        source_id: str,
        item_id: str,
        operator_id: str,
        operator_name: str,
    ) -> bool:
        """下架社区专家."""
        items = load_index(self.marketplace_root, source_id)
        item = next(
            (
                current
                for current in items
                if current.item_id == item_id and current.item_type == "expert"
            ),
            None,
        )
        if item is None:
            return False

        item.status = "inactive"
        item.updated_at = datetime.now(timezone.utc).isoformat()
        save_index(self.marketplace_root, source_id, items)
        await self._log_expert_operation(
            source_id,
            operator_id,
            operator_name,
            "unpublish",
            item,
        )
        return True

    def _expert_current_package(
        self,
        source_id: str,
        item_id: str,
    ) -> tuple[MarketItem, Path, str]:
        items = load_index(self.marketplace_root, source_id)
        item = next(
            (
                entry
                for entry in items
                if entry.item_id == item_id and entry.item_type == "expert"
            ),
            None,
        )
        if item is None or item.status != "active":
            raise ValueError(f"Expert item {item_id} is not active")
        root = get_expert_dir(self.marketplace_root, source_id, item_id)
        definition_path = root / "definition.toml"
        if not definition_path.is_file():
            raise ValueError(f"Expert definition {item_id} is missing")
        versions = self._get_expert_version_service().list_versions(
            source_id,
            item_id,
        )
        current = next(
            (
                entry
                for entry in versions["versions"]
                if entry.get("is_current")
            ),
            None,
        )
        fingerprint = str((current or {}).get("signature") or "")
        if not fingerprint:
            raise ExpertDependencyError(
                f"Expert package {item_id} has no current version signature",
            )
        current_signature = (
            self._get_expert_version_service().calculate_signature(root)
        )
        if current_signature != fingerprint:
            raise ExpertDependencyError(
                f"Expert package {item_id} failed integrity verification",
            )
        return item, root, fingerprint

    def _expert_item(self, source_id: str, item_id: str) -> MarketItem:
        item = next(
            (
                entry
                for entry in load_index(self.marketplace_root, source_id)
                if entry.item_id == item_id and entry.item_type == "expert"
            ),
            None,
        )
        if item is None:
            raise ValueError(f"Expert item {item_id} not found")
        return item

    def _find_received_expert(
        self,
        user_id: str,
        source_id: str,
        agent_id: str,
        item_id: str,
    ) -> tuple[Path, dict[str, str]] | None:
        root = get_user_expert_dir(self.swe_root, user_id, agent_id, source_id)
        if not root.exists():
            return None
        for path in root.glob("*.toml"):
            reference = _community_ref_from_toml(path)
            if reference and reference["item_id"] == item_id:
                return path, reference
        return None

    def _received_expert_paths(
        self,
        user_id: str,
        source_id: str,
        item_id: str,
    ) -> list[tuple[Path, str]]:
        """Find a received item across every Agent Profile for one user."""
        effective_user_id = resolve_effective_user_id(user_id, source_id)
        user_root = migrate_legacy_scope_dir_if_needed(
            self.swe_root,
            effective_user_id,
        )
        workspaces_root = user_root / "workspaces"
        if not workspaces_root.exists():
            return []
        matches: list[tuple[Path, str]] = []
        for profile_root in workspaces_root.iterdir():
            if not profile_root.is_dir():
                continue
            agents_root = profile_root / "agents"
            if not agents_root.exists():
                continue
            for definition_path in agents_root.glob("*.toml"):
                reference = _community_ref_from_toml(definition_path)
                if reference and reference["item_id"] == item_id:
                    matches.append((definition_path, profile_root.name))
        return matches

    def _release_expert_session_views(
        self,
        user_id: str,
        source_id: str,
        agent_id: str,
        definition_id: str,
    ) -> None:
        """Drop Chat-local views before a received expert is withdrawn."""
        expert_dir = get_user_expert_dir(
            self.swe_root,
            user_id,
            agent_id,
            source_id,
        )
        session_root = expert_dir.parent / ".expert_sessions"
        if not session_root.is_dir() or session_root.is_symlink():
            return
        target_name = str(definition_id)
        for chat_root in session_root.iterdir():
            if not chat_root.is_dir() or chat_root.is_symlink():
                continue
            target = chat_root / target_name
            if target.is_symlink():
                continue
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)

    def _received_expert_user_ids(self, source_id: str) -> list[str]:
        """Find local user scopes for all-user recall without relying on DB."""
        if not self.swe_root.exists():
            return []
        user_ids: set[str] = set()
        default_scope = f"default_{source_id}"
        for scope_dir in self.swe_root.iterdir():
            if not scope_dir.is_dir():
                continue
            if scope_dir.name == default_scope:
                user_ids.add("default")
                continue
            try:
                user_id, scope_source = decode_scope_id(scope_dir.name)
            except ValueError:
                continue
            if scope_source == source_id:
                user_ids.add(user_id)
        return sorted(user_ids)

    async def get_expert_distributions(
        self,
        source_id: str,
        item_id: str,
    ) -> list[DistributionRecord]:
        """Return users that currently hold a received copy of an expert."""
        self._expert_item(source_id, item_id)
        holder_ids = [
            user_id
            for user_id in self._received_expert_user_ids(source_id)
            if self._received_expert_paths(user_id, source_id, item_id)
        ]
        if not holder_ids:
            return []

        user_map: dict[str, dict[str, Any]] = {}
        if self.db.is_connected:
            try:
                placeholders = ",".join(["%s"] * len(holder_ids))
                rows = await self.db.fetch_all(
                    _QUERY_USERS_BY_TENANT_IDS_SQL.format(
                        placeholders=placeholders,
                    ),
                    (source_id, *holder_ids),
                )
                user_map = {row["tenant_id"]: row for row in rows}
            except Exception as exc:
                logger.warning("Failed to resolve expert holder info: %s", exc)

        return [
            DistributionRecord(
                target_user_id=user_id,
                target_user_name=user_map.get(user_id, {}).get(
                    "tenant_name",
                    "",
                )
                or "",
                target_bbk_id=user_map.get(user_id, {}).get("bbk_id", "")
                or "",
                distributed_at=None,
            )
            for user_id in holder_ids
        ]

    def _install_expert_for_user(
        self,
        source_id: str,
        item_id: str,
        user_id: str,
        agent_id: str,
        operator_id: str,
        *,
        update: bool,
    ) -> ExpertOperationResult:
        item, package_root, fingerprint = self._expert_current_package(
            source_id,
            item_id,
        )
        target_root = get_user_expert_dir(
            self.swe_root,
            user_id,
            agent_id,
            source_id,
        )
        target_root.mkdir(parents=True, exist_ok=True)
        target = self._resolve_expert_install_target(
            target_root,
            self._find_received_expert(user_id, source_id, agent_id, item_id),
            item.name,
            update,
            user_id,
        )
        if target.error is not None:
            return target.error
        source_definition, declared_skills, declared_mcps = (
            self._load_expert_package_for_install(package_root)
        )
        self._validate_expert_package_dependencies(
            package_root,
            declared_skills,
            declared_mcps,
        )
        temporary, temporary_root, dependency_root, backup_root = (
            self._stage_expert_install(
                package_root,
                target,
                source_definition,
                item_id,
                item.version,
                fingerprint,
            )
        )
        self._commit_expert_install(
            temporary,
            temporary_root,
            dependency_root,
            backup_root,
            target.definition_path,
        )
        return ExpertOperationResult(
            user_id=user_id,
            success=True,
            definition_id=target.definition_id,
        )

    @staticmethod
    def _resolve_expert_install_target(
        target_root: Path,
        received: tuple[Path, dict[str, str]] | None,
        expert_name: str,
        update: bool,
        user_id: str,
    ) -> _ExpertInstallTarget:

        if received is not None and not update:
            return _ExpertInstallTarget(
                definition_id="",
                definition_path=target_root / "unused.toml",
                enabled=False,
                error=ExpertOperationResult(
                    user_id=user_id,
                    success=False,
                    reason="expert already installed",
                ),
            )
        if received is not None:
            definition_path, _ = received
            try:
                payload = tomllib.loads(
                    definition_path.read_text(encoding="utf-8"),
                )
                enabled = bool(payload.get("enabled", False))
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
                enabled = False
            return _ExpertInstallTarget(
                definition_id=definition_path.stem,
                definition_path=definition_path,
                enabled=enabled,
                error=None,
            )
        definition_id = str(uuid.uuid4())
        from swe.app.subagents import builtin_definition_provider

        builtin_names = {
            definition.name
            for definition in builtin_definition_provider().list_definitions()
        }
        if expert_name in builtin_names:
            return _ExpertInstallTarget(
                definition_id="",
                definition_path=target_root / "unused.toml",
                enabled=True,
                error=ExpertOperationResult(
                    user_id=user_id,
                    success=False,
                    reason="expert name conflicts with builtin definition",
                ),
            )
        for path in target_root.glob("*.toml"):
            try:
                payload = tomllib.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
                continue
            if payload.get("name") == expert_name:
                return _ExpertInstallTarget(
                    definition_id="",
                    definition_path=target_root / "unused.toml",
                    enabled=True,
                    error=ExpertOperationResult(
                        user_id=user_id,
                        success=False,
                        reason="expert name conflicts with local definition",
                    ),
                )
        return _ExpertInstallTarget(
            definition_id=definition_id,
            definition_path=target_root / f"{definition_id}.toml",
            enabled=True,
            error=None,
        )

    @staticmethod
    def _load_expert_package_for_install(
        package_root: Path,
    ) -> tuple[str, list[str], list[str]]:
        try:
            source_definition = (package_root / "definition.toml").read_text(
                encoding="utf-8",
            )
            source_payload = tomllib.loads(source_definition)
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ExpertDependencyError(
                "Community expert definition is unreadable",
            ) from exc
        declared_skills, declared_mcps = _extract_expert_dependencies(
            source_payload,
        )
        return source_definition, declared_skills, declared_mcps

    @staticmethod
    def _validate_expert_package_dependencies(
        package_root: Path,
        declared_skills: list[str],
        declared_mcps: list[str],
    ) -> None:
        for skill_name in declared_skills:
            skill_root = package_root / "skills" / skill_name
            if (
                not skill_root.is_dir()
                or not (skill_root / "SKILL.md").is_file()
            ):
                raise ExpertDependencyError(
                    f"Missing declared dependency skill: {skill_name}",
                )
        for mcp_name in declared_mcps:
            config_path = package_root / "mcp" / mcp_name / "mcp.json"
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ExpertDependencyError(
                    f"Invalid bundled MCP config: {mcp_name}",
                ) from exc
            if not isinstance(config, dict):
                raise ExpertDependencyError(
                    f"Invalid bundled MCP config: {mcp_name}",
                )
            _normalize_expert_mcp_config(config, mcp_name)

    @staticmethod
    def _stage_expert_install(
        package_root: Path,
        target: _ExpertInstallTarget,
        source_definition: str,
        item_id: str,
        version: str,
        fingerprint: str,
    ) -> tuple[Path, Path, Path, Path]:
        definition_text = _community_toml(
            source_definition,
            item_id,
            version,
            fingerprint,
        )
        if "enabled =" in definition_text:
            definition_text = re.sub(
                r"(?m)^enabled\s*=\s*(true|false)\s*$",
                f"enabled = {'true' if target.enabled else 'false'}",
                definition_text,
            )
        else:
            definition_text = (
                f"enabled = {'true' if target.enabled else 'false'}\n"
                + definition_text
            )
        temporary = target.definition_path.with_name(
            f".{target.definition_path.name}.{uuid.uuid4().hex}.tmp",
        )
        temporary.write_text(definition_text, encoding="utf-8")
        dependency_root = (
            target.definition_path.parent
            / f"{target.definition_id}.dependencies"
        )
        temporary_root = dependency_root.with_name(
            f".{dependency_root.name}.source-{uuid.uuid4().hex}",
        )
        backup_root = dependency_root.with_name(
            f".{dependency_root.name}.backup-{uuid.uuid4().hex}",
        )
        try:
            temporary_root.mkdir(parents=True, exist_ok=True)
            for directory in ("skills", "mcp"):
                source = package_root / directory
                if source.exists():
                    shutil.copytree(source, temporary_root / directory)
            mcp_root = temporary_root / "mcp"
            if mcp_root.exists():
                mcp_payload: dict[str, Any] = {}
                for mcp_dir in mcp_root.iterdir():
                    config_path = mcp_dir / "mcp.json"
                    if not mcp_dir.is_dir() or not config_path.is_file():
                        continue
                    try:
                        config = json.loads(
                            config_path.read_text(encoding="utf-8"),
                        )
                    except (
                        OSError,
                        UnicodeDecodeError,
                        json.JSONDecodeError,
                    ) as exc:
                        raise ExpertDependencyError(
                            f"Invalid bundled MCP config: {mcp_dir.name}",
                        ) from exc
                    if not isinstance(config, dict):
                        raise ExpertDependencyError(
                            f"Invalid bundled MCP config: {mcp_dir.name}",
                        )
                    mcp_payload[mcp_dir.name] = _normalize_expert_mcp_config(
                        config,
                        mcp_dir.name,
                    )
                (mcp_root / "config.json").write_text(
                    json.dumps(mcp_payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            return temporary, temporary_root, dependency_root, backup_root
        except BaseException:
            temporary.unlink(missing_ok=True)
            shutil.rmtree(temporary_root, ignore_errors=True)
            raise

    @staticmethod
    def _commit_expert_install(
        temporary: Path,
        temporary_root: Path,
        dependency_root: Path,
        backup_root: Path,
        definition_path: Path,
    ) -> None:
        dependency_swapped = False
        try:
            if dependency_root.exists():
                os.replace(dependency_root, backup_root)
            os.replace(temporary_root, dependency_root)
            dependency_swapped = True
            os.replace(temporary, definition_path)
        except BaseException:
            if dependency_swapped and dependency_root.exists():
                shutil.rmtree(dependency_root, ignore_errors=True)
            if backup_root.exists():
                os.replace(backup_root, dependency_root)
            raise
        finally:
            temporary.unlink(missing_ok=True)
            if temporary_root.exists():
                shutil.rmtree(temporary_root, ignore_errors=True)
            if backup_root.exists():
                shutil.rmtree(backup_root, ignore_errors=True)

    async def install_expert(
        self,
        source_id: str,
        item_id: str,
        user_id: str,
        agent_id: str = "default",
        operator_id: str = "",
    ) -> ExpertOperationResult:
        result = self._install_expert_for_user(
            source_id,
            item_id,
            user_id,
            agent_id,
            operator_id,
            update=False,
        )
        if result.success:
            await self._log_expert_operation(
                source_id,
                operator_id,
                "",
                "receive",
                self._expert_item(source_id, item_id),
                target_user_id=user_id,
            )
            await self._trigger_agent_reload(user_id, agent_id, source_id)
        return result

    async def distribute_expert(
        self,
        source_id: str,
        item_id: str,
        operator_id: str,
        req: ExpertDistributionRequest,
    ) -> ExpertDistributionResponse:
        item, _, _ = self._expert_current_package(source_id, item_id)
        target_users = await self._resolve_target_users(
            source_id,
            DistributeRequest(
                target_type=req.target_type,
                target_values=req.target_values,
            ),
        )
        results: list[ExpertOperationResult] = []
        for user in target_users:
            try:
                result = self._install_expert_for_user(
                    source_id,
                    item_id,
                    user["tenant_id"],
                    "default",
                    operator_id,
                    update=True,
                )
            except Exception as exc:
                result = ExpertOperationResult(
                    user_id=user["tenant_id"],
                    success=False,
                    reason=str(exc),
                )
            results.append(result)
            await self._log_expert_operation(
                source_id,
                operator_id,
                "",
                "distribute",
                item,
                target_user_id=user["tenant_id"],
                target_user_name=user.get("tenant_name", ""),
                target_bbk_id=user.get("bbk_id", ""),
            )
            if result.success:
                await self._trigger_agent_reload(
                    user["tenant_id"],
                    "default",
                    source_id,
                )
        return ExpertDistributionResponse(
            item_id=item_id,
            distributed_count=sum(result.success for result in results),
            conflict_count=sum(not result.success for result in results),
            results=results,
        )

    async def recall_expert(
        self,
        source_id: str,
        item_id: str,
        operator_id: str,
        target_user_ids: list[str] | None = None,
    ) -> ExpertRecallResponse:
        item = self._expert_item(source_id, item_id)
        users = target_user_ids
        if users is None:
            db_users = [
                user["tenant_id"]
                for user in await self._resolve_target_users(
                    source_id,
                    DistributeRequest(target_type="all"),
                )
            ]
            users = sorted(
                set(db_users) | set(self._received_expert_user_ids(source_id)),
            )
        results: list[ExpertOperationResult] = []
        for user_id in users:
            matched_agent_ids: set[str] = set()
            try:
                matches = self._received_expert_paths(
                    user_id,
                    source_id,
                    item_id,
                )
                for definition_path, agent_id in matches:
                    matched_agent_ids.add(agent_id)
                    self._release_expert_session_views(
                        user_id,
                        source_id,
                        agent_id,
                        definition_path.stem,
                    )
                    definition_path.unlink(missing_ok=True)
                    dependency_root = definition_path.with_name(
                        f"{definition_path.stem}.dependencies",
                    )
                    if dependency_root.exists():
                        shutil.rmtree(dependency_root)
                removed = len(matches)
                result = ExpertOperationResult(
                    user_id=user_id,
                    success=bool(removed),
                    reason=None if removed else "received expert not found",
                )
            except Exception as exc:
                result = ExpertOperationResult(
                    user_id=user_id,
                    success=False,
                    reason=str(exc),
                )
            results.append(result)
            await self._log_expert_operation(
                source_id,
                operator_id,
                "",
                "recall",
                item,
                target_user_id=user_id,
            )
            if result.success:
                for agent_id in matched_agent_ids or {"default"}:
                    reloaded = await self._trigger_agent_reload(
                        user_id,
                        agent_id,
                        source_id,
                    )
                    if not reloaded:
                        result.success = False
                        result.reason = (
                            "agent reload failed; withdrawal is pending retry"
                        )
        return ExpertRecallResponse(
            item_id=item_id,
            recalled_count=sum(result.success for result in results),
            failed_count=sum(not result.success for result in results),
            results=results,
        )

    async def delete_market_skill(
        self,
        source_id: str,
        item_id: str,
        operator_id: str,
        operator_name: str,
    ) -> bool:
        """彻底删除市场技能（包括主目录和版本历史）。返回 True 表示成功。"""
        import shutil

        items = load_index(self.marketplace_root, source_id)
        item = next(
            (
                i
                for i in items
                if i.item_id == item_id and i.item_type == "skill"
            ),
            None,
        )
        if item is None:
            return False

        # 删除技能主目录
        skill_dir = self.marketplace_root / source_id / "skills" / item_id
        if skill_dir.exists():
            shutil.rmtree(skill_dir)

        # 删除版本历史目录
        versions_dir = (
            self.marketplace_root / source_id / "skill_versions" / item_id
        )
        if versions_dir.exists():
            shutil.rmtree(versions_dir)

        # 从索引中移除该技能
        items = [i for i in items if i.item_id != item_id]
        save_index(self.marketplace_root, source_id, items)

        # 同步删除 swe_marketplace_skills 表中的记录
        if self.db.is_connected:
            try:
                await self.db.execute(
                    "DELETE FROM swe_marketplace_skills WHERE source_id = %s AND item_id = %s",
                    (source_id, item_id),
                )
            except Exception as e:
                logger.warning(
                    "Failed to delete from swe_marketplace_skills: %s",
                    e,
                )

        if self.db.is_connected:
            try:
                await self.db.execute(
                    _LOG_MARKET_OP_SQL,
                    (
                        source_id,
                        operator_id,
                        operator_name,
                        "delete",
                        "skill",
                        item_id,
                        item.name,
                        None,
                        None,
                        None,
                    ),
                )
            except Exception as e:
                logger.warning("Failed to log delete operation: %s", e)

        return True

    async def list_skills(
        self,
        source_id: str,
        user_bbk_id: str,
        category_id: Optional[int] = None,
        bbk_ids: Optional[list[str]] = None,
    ) -> list[MarketSkillResponse]:
        """列出市场技能，可选按分类和分行过滤。"""
        items = load_index(self.marketplace_root, source_id)
        visible = [
            i for i in items if i.item_type == "skill" and i.status == "active"
        ]
        if category_id is not None:
            visible = [i for i in visible if i.category_id == category_id]
        # 按 bbk_ids 过滤（技能的 bbk_ids 与请求的 bbk_ids 有交集）
        if bbk_ids is not None and len(bbk_ids) > 0:
            visible = [
                i
                for i in visible
                if i.bbk_ids and any(b in i.bbk_ids for b in bbk_ids)
            ]

        result = []
        for item in visible:
            call_count, user_count = await self._get_stats(
                item.name,
                source_id,
            )
            result.append(
                MarketSkillResponse(
                    item_id=item.item_id,
                    name=item.name,
                    skill_id=item.skill_id,
                    chinese_name=item.chinese_name,
                    description=item.description,
                    version=item.version,
                    creator_id=item.creator_id,
                    creator_name=_decode_creator_name(item.creator_name),
                    category_id=item.category_id,
                    bbk_ids=item.bbk_ids,
                    status=item.status,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                    call_count=call_count,
                    user_count=user_count,
                    include_in_statistics=item.include_in_statistics,
                ),
            )
        return result

    async def get_skill_detail(
        self,
        source_id: str,
        item_id: str,
        user_bbk_id: str,
    ) -> Optional[MarketSkillDetail]:
        """获取技能详情（含调用客户明细）。"""
        item = self._get_visible_skill_item(source_id, item_id, user_bbk_id)
        if item is None:
            return None

        call_count, user_count = await self._get_stats(item.name, source_id)
        user_stats = await self._get_user_stats(item.name, source_id)

        return MarketSkillDetail(
            item_id=item.item_id,
            name=item.name,
            skill_id=item.skill_id,
            chinese_name=item.chinese_name,
            description=item.description,
            version=item.version,
            creator_id=item.creator_id,
            creator_name=_decode_creator_name(item.creator_name),
            category_id=item.category_id,
            bbk_ids=item.bbk_ids,
            status=item.status,
            created_at=item.created_at,
            updated_at=item.updated_at,
            call_count=call_count,
            user_count=user_count,
            user_stats=user_stats,
            include_in_statistics=item.include_in_statistics,
        )

    def _get_visible_skill_item(
        self,
        source_id: str,
        item_id: str,
        user_bbk_id: str,
    ) -> MarketItem | None:
        """获取当前用户可见的市场技能条目。"""
        items = load_index(self.marketplace_root, source_id)
        item = next(
            (
                entry
                for entry in items
                if entry.item_id == item_id and entry.item_type == "skill"
            ),
            None,
        )
        if item is None or not _item_visible(item, user_bbk_id):
            return None
        return item

    async def distribute_skill(
        self,
        source_id: str,
        item_id: str,
        operator_id: str,
        operator_name: str,
        req: DistributeRequest,
    ) -> DistributeResponse:
        """分发技能到目标用户工作目录，并写操作日志。

        自建技能（source=customized）不覆盖，返回冲突明细。
        """
        items = load_index(self.marketplace_root, source_id)
        item = next(
            (
                i
                for i in items
                if i.item_id == item_id and i.item_type == "skill"
            ),
            None,
        )
        if item is None:
            raise ValueError(f"Item {item_id} not found in source {source_id}")

        # 将技能名称规范化为目录名（保留中文等 Unicode 字符）
        safe_skill_name = normalize_skill_name(item.name)

        # 直接使用 MarketItem 中已保存的 skill_id 和 chinese_name
        skill_id = item.skill_id
        cn_name = item.chinese_name

        target_users = await self._resolve_target_users(source_id, req)
        count = 0
        conflicts: list[dict] = []
        results: list[DistributeTenantResult] = []

        for user in target_users:
            tenant_id = user["tenant_id"]
            try:
                result = copy_skill_to_user(
                    marketplace_root=self.marketplace_root,
                    source_id=source_id,
                    item_id=item_id,
                    swe_root=self.swe_root,
                    user_id=tenant_id,
                    skill_name=safe_skill_name,
                    original_name=item.name,
                    description=item.description,
                    distributed_by=operator_id,
                    version=item.version,
                    skill_id=skill_id,
                    cn_name=cn_name,
                )

                if result.get("status") == "conflict":
                    conflicts.append(
                        {
                            "user_id": tenant_id,
                            "skill_name": safe_skill_name,
                            "reason": result.get("reason", "unknown"),
                        },
                    )
                    results.append(
                        DistributeTenantResult(
                            user_id=tenant_id,
                            success=False,
                            status="conflict",
                            skill_name=safe_skill_name,
                            error=result.get("reason", "unknown"),
                        ),
                    )
                    continue

                # 注册技能到 manifest（使用返回的 metadata）
                metadata = result.get("metadata") or {}
                final_enabled = bool(result["final_enabled"])
                self.register_skill_in_manifest(
                    tenant_id,
                    safe_skill_name,
                    "default",
                    source_id,
                    enabled=final_enabled,
                    source=f"marketplace:{item_id}",
                    extra_metadata=metadata,
                    package_path=result.get("package_path"),
                )

                # 写入 swe_skills 表（分发时记录用户持有状态）
                inserted = await self.skill_registry.insert_skill(
                    skill_id=skill_id,
                    skill_name=safe_skill_name,
                    cn_name=cn_name,
                    tenant_id=tenant_id,
                    tenant_name=user.get("tenant_name", ""),
                    bbk_id=user.get("bbk_id", ""),
                    source=f"marketplace:{item_id}",
                    source_id=source_id,
                    enabled=final_enabled,
                    description=item.description,
                    version_text=item.version,
                )
                if not inserted:
                    logger.warning(
                        "分发成功但 swe_skills 写入失败: user=%s, skill=%s",
                        tenant_id,
                        safe_skill_name,
                    )
                if final_enabled:
                    await self._trigger_agent_reload(
                        tenant_id,
                        "default",
                        source_id,
                    )
                count += 1
                results.append(
                    DistributeTenantResult(
                        user_id=tenant_id,
                        success=True,
                        status="distributed",
                        skill_name=safe_skill_name,
                    ),
                )
            except Exception as e:
                logger.warning(
                    "Failed to copy skill to user %s: %s",
                    tenant_id,
                    e,
                )
                results.append(
                    DistributeTenantResult(
                        user_id=tenant_id,
                        success=False,
                        status="failed",
                        skill_name=safe_skill_name,
                        error=str(e),
                    ),
                )
                continue

            if self.db.is_connected:
                try:
                    await self.db.execute(
                        _LOG_MARKET_OP_SQL,
                        (
                            source_id,
                            operator_id,
                            operator_name,
                            "distribute",
                            "skill",
                            item_id,
                            item.name,
                            tenant_id,
                            user.get("tenant_name", ""),
                            user.get("bbk_id", ""),
                        ),
                    )
                except Exception as e:
                    logger.warning("Failed to log distribute operation: %s", e)

        return DistributeResponse(
            distributed_count=count,
            conflict_count=len(conflicts),
            failed_count=sum(
                1
                for item in results
                if not item.success and item.status != "conflict"
            ),
            conflicts=conflicts,
            results=results,
            item_id=item_id,
        )

    async def get_my_skills(
        self,
        source_id: str,
        user_id: str,
        agent_id: str = "default",
    ) -> list[MySkillItem]:
        """获取用户技能列表（我创建的 + 我接收的）。

        数据来源：
        - name、description：从 SKILL.md frontmatter 读取
        - source、distributed_by、received_version 等：从 workspace manifest 读取
        - 不再依赖技能目录内的 skill.json 文件
        """
        # 读取 workspace manifest 获取技能状态和元数据
        manifest = read_user_skill_manifest(
            self.swe_root,
            user_id,
            agent_id,
            source_id,
        )
        manifest_skills = manifest.get("skills", {})
        market_versions = self._get_active_market_versions(source_id)
        workspace_dir = get_user_skill_manifest_path(
            self.swe_root,
            user_id,
            agent_id,
            source_id,
        ).parent
        skill_dirs: dict[str, Path] = {}
        for skill_name, manifest_entry in sorted(manifest_skills.items()):
            if not isinstance(manifest_entry, dict):
                continue
            entry_for_resolution = dict(manifest_entry)
            entry_for_resolution.setdefault("enabled", True)
            try:
                skill_dir = resolve_registered_skill_path(
                    workspace_dir,
                    skill_name,
                    entry_for_resolution,
                ).path
            except ValueError:
                continue
            if skill_dir is None or not skill_dir.is_dir():
                continue
            skill_dirs[skill_name] = skill_dir

        active_skills_dir = get_user_skills_dir(
            self.swe_root,
            user_id,
            agent_id,
            source_id,
        )
        if active_skills_dir.is_dir():
            for skill_dir in active_skills_dir.iterdir():
                if (
                    skill_dir.is_dir()
                    and skill_dir.name not in manifest_skills
                ):
                    skill_dirs[skill_dir.name] = skill_dir

        return [
            self._build_my_skill_item(
                skill_dir,
                manifest_skills,
                market_versions,
            )
            for _, skill_dir in sorted(skill_dirs.items())
        ]

    def _get_active_market_versions(self, source_id: str) -> dict[str, str]:
        """读取当前来源下已发布技能的最新版本映射."""
        market_index = load_index(self.marketplace_root, source_id)
        versions: dict[str, str] = {}
        for item in market_index:
            if item.status != "active":
                continue
            for key in (item.item_id, item.skill_id, item.name):
                if key:
                    versions[key] = item.version
        return versions

    def _resolve_market_version(
        self,
        source: str,
        skill_id: str,
        skill_name: str,
        display_name: str,
        market_versions: dict[str, str],
    ) -> str | None:
        """Resolve the current market version from stable ids before names."""
        source_item_id = ""
        if source.startswith("marketplace:"):
            source_item_id = source.removeprefix("marketplace:")
        for key in (source_item_id, skill_id, skill_name, display_name):
            if key and key in market_versions:
                return market_versions[key]
        return None

    def _read_skill_frontmatter(
        self,
        skill_dir: Path,
        skill_name: str,
    ) -> tuple[str, str, str]:
        """读取技能 frontmatter 中的展示名称、描述和版本号."""
        skill_md_path = skill_dir / "SKILL.md"
        if not skill_md_path.exists():
            return skill_name, "", ""
        try:
            md_content = skill_md_path.read_text(encoding="utf-8")
        except Exception:  # pylint: disable=broad-except
            return skill_name, "", ""
        if not md_content.startswith("---"):
            return skill_name, "", ""
        name, desc = _parse_md_frontmatter(md_content, skill_name)
        version = _extract_version_from_frontmatter(md_content)
        return name, desc, version

    def _resolve_skill_display_fields(
        self,
        skill_dir: Path,
        skill_name: str,
        manifest_metadata: dict[str, Any],
    ) -> tuple[str, str, str]:
        """合并 manifest 与 frontmatter，得到展示名称、描述和版本号."""
        md_name, md_desc, md_version = self._read_skill_frontmatter(
            skill_dir,
            skill_name,
        )
        display_name = manifest_metadata.get("name") or md_name
        description = manifest_metadata.get("description") or md_desc
        # 版本号优先使用 manifest_metadata，其次 frontmatter
        version = manifest_metadata.get("version_text") or md_version
        return display_name, description, version

    def _resolve_skill_timestamps(
        self,
        manifest_entry: dict[str, Any],
        manifest_metadata: dict[str, Any],
    ) -> tuple[str | None, str | None]:
        """优先从运行时 manifest 读取时间字段，缺失时回退 metadata."""
        created_at = manifest_entry.get("created_at") or manifest_metadata.get(
            "created_at",
        )
        updated_at = manifest_entry.get("updated_at") or manifest_metadata.get(
            "updated_at",
        )
        return created_at, updated_at

    def _resolve_skill_id_cn_name(
        self,
        skill_dir: Path,
        skill_name: str,
        source: str,
        manifest_metadata: dict[str, Any],
    ) -> tuple[str, str]:
        """获取 skill_id 和 cn_name 字段.

        直接从 manifest_metadata 中读取，不再解析 SKILL.md。

        Args:
            skill_dir: 技能目录路径
            skill_name: 技能目录名
            source: 技能来源（customized / marketplace:xxx）
            manifest_metadata: workspace manifest 中的 metadata 字段

        Returns:
            (skill_id, cn_name) 元组
        """
        # 直接使用 manifest 中的数据
        skill_id = manifest_metadata.get("skill_id", "") or ""
        cn_name = manifest_metadata.get("cn_name", "") or ""

        # 如果 manifest 中没有 cn_name，使用 skill_name 作为 fallback
        if not cn_name:
            cn_name = skill_name

        return skill_id, cn_name

    def _build_my_skill_item(
        self,
        skill_dir: Path,
        manifest_skills: dict[str, Any],
        market_versions: dict[str, str],
    ) -> MySkillItem:
        """构建“我的技能”列表中的单个条目."""
        skill_name = skill_dir.name
        manifest_entry = manifest_skills.get(skill_name, {})
        manifest_metadata = manifest_entry.get("metadata", {})
        source = manifest_entry.get("source") or manifest_metadata.get(
            "source",
            "customized",
        )
        display_name, description, version = (
            self._resolve_skill_display_fields(
                skill_dir,
                skill_name,
                manifest_metadata,
            )
        )
        received_version = manifest_metadata.get("received_version")
        created_at, updated_at = self._resolve_skill_timestamps(
            manifest_entry,
            manifest_metadata,
        )
        skill_id, cn_name = self._resolve_skill_id_cn_name(
            skill_dir,
            skill_name,
            source,
            manifest_metadata,
        )
        market_version = self._resolve_market_version(
            source,
            skill_id,
            skill_name,
            display_name,
            market_versions,
        )
        is_received = source.startswith("marketplace:")
        has_update = (
            is_received
            and received_version is not None
            and market_version is not None
            and received_version != market_version
        )
        category_id = manifest_metadata.get("category_id")
        creator_name = manifest_metadata.get("creator_name")

        return MySkillItem(
            skill_name=skill_name,
            display_name=display_name,
            source=source,
            description=description,
            version=version or "1.0.0",
            received_version=received_version,
            market_version=market_version,
            distributed_by=manifest_metadata.get("distributed_by"),
            is_received=is_received,
            has_update=has_update,
            enabled=manifest_entry.get("enabled", True),
            category=str(category_id) if category_id else None,
            creator_name=_decode_creator_name(creator_name or ""),
            created_at=created_at,
            updated_at=updated_at,
            skill_id=skill_id,
            cn_name=cn_name,
        )

    async def _get_stats(
        self,
        skill_name: str,
        source_id: str,
    ) -> tuple[int, int]:
        if not self.db.is_connected:
            return 0, 0
        try:
            row = await self.db.fetch_one(
                _TRACING_STATS_SQL,
                (skill_name, source_id),
            )
            if row:
                return int(row.get("call_count", 0)), int(
                    row.get("user_count", 0),
                )
        except Exception as e:
            logger.warning("Failed to fetch stats for %s: %s", skill_name, e)
        return 0, 0

    async def _get_user_stats(
        self,
        skill_name: str,
        source_id: str,
    ) -> list[SkillUserStat]:
        if not self.db.is_connected:
            return []
        try:
            rows = await self.db.fetch_all(
                _TRACING_USER_STATS_SQL,
                (skill_name, source_id),
            )
            return [
                SkillUserStat(
                    user_id=r["user_id"],
                    user_name=_decode_creator_name(r.get("user_name", "")),
                    call_count=int(r["call_count"]),
                )
                for r in rows
            ]
        except Exception as e:
            logger.warning(
                "Failed to fetch user stats for %s: %s",
                skill_name,
                e,
            )
        return []

    async def _resolve_target_users(
        self,
        source_id: str,
        req: DistributeRequest,
    ) -> list[dict]:
        if not self.db.is_connected:
            # 数据库未连接时返回空信息
            if req.target_type == "user_id" and req.target_values:
                return [
                    {"tenant_id": uid, "tenant_name": "", "bbk_id": ""}
                    for uid in req.target_values
                ]
            return []
        try:
            if req.target_type == "all":
                return await self.db.fetch_all(
                    _QUERY_USERS_BY_SOURCE_SQL,
                    (source_id,),
                )
            if req.target_type == "bbk_id" and req.target_values:
                placeholders = ",".join(["%s"] * len(req.target_values))
                sql = _QUERY_USERS_BY_BBK_SQL.format(placeholders=placeholders)
                return await self.db.fetch_all(
                    sql,
                    (source_id, *req.target_values),
                )
            if req.target_type == "user_id" and req.target_values:
                # 手动输入用户 ID 时，也从数据库查询用户信息
                placeholders = ",".join(["%s"] * len(req.target_values))
                sql = _QUERY_USERS_BY_TENANT_IDS_SQL.format(
                    placeholders=placeholders,
                )
                rows = await self.db.fetch_all(
                    sql,
                    (source_id, *req.target_values),
                )
                # 创建映射，查询不到的用户保留空信息
                user_map = {row["tenant_id"]: row for row in rows}
                return [
                    user_map.get(
                        uid,
                        {"tenant_id": uid, "tenant_name": "", "bbk_id": ""},
                    )
                    for uid in req.target_values
                ]
        except Exception as e:
            logger.warning("Failed to resolve target users: %s", e)
            # user_id 模式下即使数据库查询失败，仍然可以按 ID 分发
            if req.target_type == "user_id" and req.target_values:
                return [
                    {"tenant_id": uid, "tenant_name": "", "bbk_id": ""}
                    for uid in req.target_values
                ]
        return []

    def _build_user_status_list(
        self,
        target_tenant_ids: list[str],
        user_skill_map: dict,
        user_info_map: dict,
        skill_name: str,
        source_id: str,
    ) -> tuple[list[dict], int, int, int]:
        """构建用户状态列表，返回 (状态列表, 文件I/O次数, customized数, 无记录数)."""
        from .fs import check_skill_status_in_manifest

        users_status: list[dict] = []
        file_io_count = 0
        customized_count = 0
        no_record_count = 0

        for tenant_id in target_tenant_ids:
            user_info = user_info_map.get(
                tenant_id,
                {"tenant_id": tenant_id, "tenant_name": None, "bbk_id": None},
            )
            skill_info = user_skill_map.get(tenant_id)

            if skill_info:
                source = skill_info.get("source", "")
                current_version = skill_info.get("version_text", "")

                if source.startswith("marketplace:"):
                    users_status.append(
                        {
                            "tenant_id": tenant_id,
                            "tenant_name": user_info.get("tenant_name"),
                            "bbk_id": user_info.get("bbk_id"),
                            "status": "update",
                            "current_version": current_version,
                        },
                    )
                elif source == "customized":
                    customized_count += 1
                    file_io_count += 1
                    manifest_status, manifest_version = (
                        check_skill_status_in_manifest(
                            self.swe_root,
                            tenant_id,
                            skill_name,
                            source_id,
                        )
                    )
                    users_status.append(
                        {
                            "tenant_id": tenant_id,
                            "tenant_name": user_info.get("tenant_name"),
                            "bbk_id": user_info.get("bbk_id"),
                            "status": manifest_status,
                            "current_version": manifest_version,
                        },
                    )
                else:
                    users_status.append(
                        {
                            "tenant_id": tenant_id,
                            "tenant_name": user_info.get("tenant_name"),
                            "bbk_id": user_info.get("bbk_id"),
                            "status": "first_time",
                            "current_version": None,
                        },
                    )
            else:
                no_record_count += 1
                file_io_count += 1
                manifest_status, manifest_version = (
                    check_skill_status_in_manifest(
                        self.swe_root,
                        tenant_id,
                        skill_name,
                        source_id,
                    )
                )
                users_status.append(
                    {
                        "tenant_id": tenant_id,
                        "tenant_name": user_info.get("tenant_name"),
                        "bbk_id": user_info.get("bbk_id"),
                        "status": manifest_status,
                        "current_version": manifest_version,
                    },
                )

        return users_status, file_io_count, customized_count, no_record_count

    async def get_distribution_preview(
        self,
        source_id: str,
        item_id: str,
        target_tenant_ids: list[str],
    ) -> dict:
        """获取技能分发预览，返回每个用户的技能持有状态.

        Args:
            source_id: 来源 ID
            item_id: 市场条目 ID
            target_tenant_ids: 目标用户 ID 列表

        Returns:
            包含 skill_version、users、distributed_user_ids 的字典
        """
        import time

        t_start = time.time()

        # 加载市场条目
        t0 = time.time()
        items = load_index(self.marketplace_root, source_id)
        item = next(
            (
                i
                for i in items
                if i.item_id == item_id and i.item_type == "skill"
            ),
            None,
        )
        if item is None:
            raise ValueError(f"Item {item_id} not found in source {source_id}")
        logger.info(
            "[PERF] 加载市场条目: %.2fs, item_id=%s",
            time.time() - t0,
            item_id,
        )

        skill_version = item.version
        skill_name = normalize_skill_name(item.name)

        # 查询用户技能状态
        users_status: list[dict] = []
        distributed_user_ids: list[str] = []

        if self.db.is_connected and target_tenant_ids:
            # 第1次数据库查询：用户技能状态
            t1 = time.time()
            placeholders = ",".join(["%s"] * len(target_tenant_ids))
            sql = _QUERY_USER_SKILL_STATUS_SQL.format(
                placeholders=placeholders,
            )
            rows = await self.db.fetch_all(
                sql,
                (skill_name, source_id, *target_tenant_ids),
            )
            logger.info(
                "[PERF] 查询用户技能状态: %.2fs, 用户数=%d, 返回=%d",
                time.time() - t1,
                len(target_tenant_ids),
                len(rows),
            )

            # 构建状态映射
            user_skill_map = {row["tenant_id"]: row for row in rows}

            # 第2次数据库查询：已分发用户
            t2 = time.time()
            dist_rows = await self.db.fetch_all(
                _QUERY_DISTRIBUTED_USERS_SQL,
                (skill_name, source_id),
            )
            distributed_user_ids = list(
                {
                    row["tenant_id"]
                    for row in dist_rows
                    if row["tenant_id"] in target_tenant_ids
                },
            )
            logger.info(
                "[PERF] 查询已分发用户: %.2fs, 返回=%d, 在目标中=%d",
                time.time() - t2,
                len(dist_rows),
                len(distributed_user_ids),
            )

            # 第3次数据库查询：用户基本信息
            t3 = time.time()
            user_sql = _QUERY_USERS_BY_TENANT_IDS_SQL.format(
                placeholders=placeholders,
            )
            user_rows = await self.db.fetch_all(
                user_sql,
                (source_id, *target_tenant_ids),
            )
            user_info_map = {row["tenant_id"]: row for row in user_rows}
            logger.info(
                "[PERF] 查询用户基本信息: %.2fs, 返回=%d",
                time.time() - t3,
                len(user_rows),
            )

            # 构建每个用户的状态（含文件I/O统计）
            t4 = time.time()
            (
                users_status,
                file_io_count,
                customized_count,
                no_record_count,
            ) = self._build_user_status_list(
                target_tenant_ids,
                user_skill_map,
                user_info_map,
                skill_name,
                source_id,
            )
            logger.info(
                "[PERF] 循环处理用户状态: %.2fs, 文件I/O=%d (customized=%d, 无记录=%d)",
                time.time() - t4,
                file_io_count,
                customized_count,
                no_record_count,
            )
        else:
            # 数据库未连接或无目标用户，返回基本信息
            for tenant_id in target_tenant_ids:
                users_status.append(
                    {
                        "tenant_id": tenant_id,
                        "tenant_name": None,
                        "bbk_id": None,
                        "status": "first_time",
                        "current_version": None,
                    },
                )

        logger.info(
            "[PERF] get_distribution_preview 总耗时: %.2fs, 用户数=%d",
            time.time() - t_start,
            len(target_tenant_ids),
        )

        return {
            "skill_version": skill_version,
            "users": users_status,
            "distributed_user_ids": distributed_user_ids,
        }

    def list_skill_files(
        self,
        user_id: str,
        skill_name: str,
        agent_id: str = "default",
        source_id: str | None = None,
    ) -> list[dict]:
        """列出技能文件树（不包含 skill.json）."""
        skill_dir = self.get_registered_skill_dir(
            user_id,
            skill_name,
            agent_id,
            source_id,
        )
        if skill_dir is None:
            return []
        return _build_file_tree_entries(
            skill_dir,
            hidden_files={"skill.json"},
        )

    def get_registered_skill_dir(
        self,
        user_id: str,
        skill_name: str,
        agent_id: str = "default",
        source_id: str | None = None,
    ) -> Path | None:
        """解析 manifest 注册技能在 active 或 disabled 根目录中的路径."""
        manifest = read_user_skill_manifest(
            self.swe_root,
            user_id,
            agent_id,
            source_id,
        )
        manifest_entry = manifest.get("skills", {}).get(skill_name)
        if not isinstance(manifest_entry, dict):
            active_skill_dir = (
                get_user_skills_dir(
                    self.swe_root,
                    user_id,
                    agent_id,
                    source_id,
                )
                / skill_name
            )
            return active_skill_dir if active_skill_dir.is_dir() else None
        entry_for_resolution = dict(manifest_entry)
        entry_for_resolution.setdefault("enabled", True)
        workspace_dir = get_user_skill_manifest_path(
            self.swe_root,
            user_id,
            agent_id,
            source_id,
        ).parent
        try:
            return resolve_registered_skill_path(
                workspace_dir,
                skill_name,
                entry_for_resolution,
            ).path
        except ValueError:
            return None

    def read_skill_file(
        self,
        user_id: str,
        skill_name: str,
        file_path: str,
        agent_id: str = "default",
        source_id: str | None = None,
    ) -> tuple[str | None, str]:
        """读取技能文件内容，返回 (content, file_type)."""
        skill_dir = self.get_registered_skill_dir(
            user_id,
            skill_name,
            agent_id,
            source_id,
        )
        if skill_dir is None:
            return None, "error"
        return _read_preview_file(skill_dir, file_path)

    def list_market_skill_files(
        self,
        source_id: str,
        item_id: str,
        user_bbk_id: str,
    ) -> list[dict] | None:
        """列出市场技能详情页的文件树。"""
        item = self._get_visible_skill_item(source_id, item_id, user_bbk_id)
        if item is None:
            return None

        skill_dir = get_skill_dir(
            self.marketplace_root,
            source_id,
            item.item_id,
        )
        return _build_file_tree_entries(skill_dir)

    def read_market_skill_file(
        self,
        source_id: str,
        item_id: str,
        file_path: str,
        user_bbk_id: str,
    ) -> tuple[str | None, str]:
        """读取市场技能详情页文件内容。"""
        item = self._get_visible_skill_item(source_id, item_id, user_bbk_id)
        if item is None:
            return None, "error"

        skill_dir = get_skill_dir(
            self.marketplace_root,
            source_id,
            item.item_id,
        )
        return _read_preview_file(skill_dir, file_path)

    def _bump_skill_version_in_frontmatter(
        self,
        skill_dir: Path,
    ) -> str:
        """Bump SKILL.md frontmatter 中的 version 字段，返回新版本号."""
        skill_md_path = skill_dir / "SKILL.md"
        if not skill_md_path.exists():
            return "1.0.1"

        try:
            md_content = skill_md_path.read_text(encoding="utf-8")
        except OSError:
            return "1.0.1"

        current_version = (
            _extract_version_from_frontmatter(md_content) or "1.0.0"
        )
        new_version = _bump_patch(current_version)

        # 替换 frontmatter 中的 version 行
        try:
            end_idx = md_content.index("---", 3)
        except ValueError:
            return new_version

        fm_text = md_content[3:end_idx]
        lines = fm_text.split("\n")
        replaced = False
        for i, line in enumerate(lines):
            if ":" in line:
                key, _ = line.split(":", 1)
                if key.strip().lower() == "version":
                    lines[i] = f"version: {new_version}"
                    replaced = True
                    break

        if not replaced:
            # frontmatter 中没有 version 行，追加
            lines.append(f"version: {new_version}")

        new_fm = "\n".join(lines)
        new_content = f"---\n{new_fm}\n---{md_content[end_idx + 3 :]}"

        try:
            skill_md_path.write_text(new_content, encoding="utf-8")
        except OSError as e:
            logger.warning("Failed to bump version in SKILL.md: %s", e)

        return new_version

    def _bump_skill_version_in_manifest(
        self,
        user_id: str,
        skill_name: str,
        new_version: str,
        agent_id: str = "default",
        source_id: str | None = None,
    ) -> None:
        """更新 manifest 中技能的 version_text 和 updated_at."""
        now = datetime.now(timezone.utc).isoformat()

        def _update(payload: dict) -> bool:
            entry = payload.get("skills", {}).get(skill_name)
            if entry is None:
                return False
            metadata = entry.get("metadata", {})
            metadata["version_text"] = new_version
            metadata["updated_at"] = now
            entry["metadata"] = metadata
            entry["updated_at"] = now
            return True

        mutate_user_skill_manifest(
            self.swe_root,
            user_id,
            agent_id,
            _update,
            source_id,
        )

    def _update_skill_in_manifest(
        self,
        user_id: str,
        skill_name: str,
        new_version: str,
        cn_name: str | None,
        agent_id: str = "default",
        source_id: str | None = None,
    ) -> None:
        """更新 manifest 中技能的 version_text、cn_name 和 updated_at."""
        now = datetime.now(timezone.utc).isoformat()

        def _update(payload: dict) -> bool:
            entry = payload.get("skills", {}).get(skill_name)
            if entry is None:
                return False
            metadata = entry.get("metadata", {})
            metadata["version_text"] = new_version
            metadata["updated_at"] = now
            if cn_name:
                metadata["cn_name"] = cn_name
            entry["metadata"] = metadata
            entry["updated_at"] = now
            return True

        mutate_user_skill_manifest(
            self.swe_root,
            user_id,
            agent_id,
            _update,
            source_id,
        )

    def _update_cn_name_in_frontmatter(
        self,
        skill_dir: Path,
        cn_name: str,
    ) -> None:
        """更新 SKILL.md frontmatter 中的 metadata.cn_name."""
        skill_md_path = skill_dir / "SKILL.md"
        if not skill_md_path.exists():
            return

        try:
            content = skill_md_path.read_text(encoding="utf-8")
            from ..utils.skill_md import parse_frontmatter
            import yaml

            fm = parse_frontmatter(content)
            metadata = fm.get("metadata", {})
            if isinstance(metadata, dict):
                metadata["cn_name"] = cn_name
                fm["metadata"] = metadata

            # 重新生成 frontmatter
            frontmatter_str = yaml.dump(
                fm,
                allow_unicode=True,
                sort_keys=False,
            )
            new_content = f"---\n{frontmatter_str}---\n"

            # 保留原有正文内容（frontmatter 之后的部分）
            lines = content.split("\n")
            body_start = 0
            for i, line in enumerate(lines):
                if i > 0 and line.strip() == "---":
                    body_start = i + 1
                    break

            if body_start < len(lines):
                body = "\n".join(lines[body_start:])
                new_content += body

            skill_md_path.write_text(new_content, encoding="utf-8")
            logger.info("Updated cn_name in SKILL.md frontmatter: %s", cn_name)
        except (OSError, yaml.YAMLError) as e:
            logger.warning("Failed to update cn_name in frontmatter: %s", e)

    def save_skill_file(
        self,
        user_id: str,
        skill_name: str,
        file_path: str,
        content: str,
        user_name: str | None = None,
        agent_id: str = "default",
        source_id: str | None = None,
        cn_name: str | None = None,
    ) -> tuple[bool, str | None]:
        """保存技能文件内容，可选更新中文名.

        返回:
            (是否成功, 新版本号或None)
        """
        skill_dir = self.get_registered_skill_dir(
            user_id,
            skill_name,
            agent_id,
            source_id,
        )
        if skill_dir is None:
            return False, None
        target = skill_dir / file_path

        try:
            target.resolve().relative_to(skill_dir.resolve())
        except ValueError:
            return False, None

        if not target.exists() or not target.is_file():
            return False, None

        # 读取现有内容，判断是否有变化
        try:
            existing_content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            existing_content = None

        content_changed = existing_content != content
        cn_name_changed = cn_name and self._check_cn_name_changed(
            skill_dir,
            cn_name,
        )

        # 内容和中文名都没变化，无需写入文件
        if not content_changed and not cn_name_changed:
            return (True, None)

        try:
            # 写入文件内容（如有变化）
            if content_changed:
                target.write_text(content, encoding="utf-8")
                logger.info(
                    "保存技能文件: user_id=%s, agent_id=%s, skill_name=%s, "
                    "file_path=%s, workspace=%s",
                    user_id,
                    agent_id,
                    skill_name,
                    file_path,
                    str(skill_dir),
                )

            current_time = datetime.now(timezone.utc).isoformat()

            # 更新 cn_name（如有变化）
            if cn_name_changed and cn_name:
                self._update_cn_name_in_frontmatter(skill_dir, cn_name)

            # bump SKILL.md frontmatter 中的 version 字段
            new_version = self._bump_skill_version_in_frontmatter(skill_dir)

            # 处理 skill.json：自动创建或更新
            skill_json_path = skill_dir / "skill.json"
            self._update_skill_json_file(
                skill_json_path,
                skill_name,
                new_version,
                cn_name,
                user_id,
                user_name,
                current_time,
            )

            # 同步 bump manifest 中的 version_text 和 cn_name
            self._update_skill_in_manifest(
                user_id,
                skill_name,
                new_version,
                cn_name,
                agent_id,
                source_id,
            )

            return (True, new_version)
        except Exception:
            return (False, None)

    def _check_cn_name_changed(self, skill_dir: Path, cn_name: str) -> bool:
        """检查 SKILL.md frontmatter 中的 cn_name 是否需要更新.

        Args:
            skill_dir: 技能目录路径
            cn_name: 新的中文名

        Returns:
            是否需要更新
        """
        skill_md_path = skill_dir / "SKILL.md"
        if not skill_md_path.exists():
            return False

        try:
            md_content = skill_md_path.read_text(encoding="utf-8")
            from ..utils.skill_md import parse_frontmatter

            fm = parse_frontmatter(md_content)
            metadata = fm.get("metadata", {})
            if isinstance(metadata, dict):
                existing_cn_name = metadata.get("cn_name", "")
                logger.info(
                    "cn_name check: existing=%s, new=%s, changed=%s",
                    existing_cn_name,
                    cn_name,
                    cn_name != existing_cn_name,
                )
                return cn_name != existing_cn_name
        except (OSError, UnicodeDecodeError):
            return True  # 无法读取，假定需要更新

        return False

    def _update_skill_json_file(
        self,
        skill_json_path: Path,
        skill_name: str,
        new_version: str,
        cn_name: str | None,
        user_id: str,
        user_name: str | None,
        current_time: str,
    ) -> None:
        """更新或创建 skill.json 文件.

        Args:
            skill_json_path: skill.json 文件路径
            skill_name: 技能名称
            new_version: 新版本号
            cn_name: 中文名（可选）
            user_id: 用户 ID
            user_name: 用户名（可选）
            current_time: 当前时间字符串
        """
        if skill_json_path.exists():
            # 更新现有 skill.json
            try:
                skill_data = json.loads(
                    skill_json_path.read_text(encoding="utf-8"),
                )
                skill_data["updated_at"] = current_time
                skill_data["version"] = new_version
                if cn_name:
                    skill_data["cn_name"] = cn_name
                skill_json_path.write_text(
                    json.dumps(skill_data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to update skill.json updated_at: %s", e)
        else:
            # 自动创建基础 skill.json
            base_skill_data = {
                "name": skill_name,
                "description": "",
                "version": new_version,
                "creator_id": user_id,
                "creator_name": user_name or "",
                "created_at": current_time,
                "source": "customized",
                "cn_name": cn_name or "",
            }
            try:
                skill_json_path.write_text(
                    json.dumps(base_skill_data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                logger.info("Auto-created skill.json for %s", skill_name)
            except OSError as e:
                logger.warning("Failed to auto-create skill.json: %s", e)

    async def delete_skill(
        self,
        user_id: str,
        skill_name: str,
        agent_id: str = "default",
        source_id: str | None = None,
    ) -> bool:
        """删除用户技能（同时从 manifest 移除条目并删除数据库记录）。"""
        import shutil

        manifest = read_user_skill_manifest(
            self.swe_root,
            user_id,
            agent_id,
            source_id,
        )
        manifest_entry = manifest.get("skills", {}).get(skill_name)
        if not isinstance(manifest_entry, dict):
            return False
        entry_for_resolution = dict(manifest_entry)
        entry_for_resolution.setdefault("enabled", True)
        workspace_dir = get_user_skill_manifest_path(
            self.swe_root,
            user_id,
            agent_id,
            source_id,
        ).parent
        try:
            skill_dir = resolve_registered_skill_path(
                workspace_dir,
                skill_name,
                entry_for_resolution,
            ).path
        except ValueError:
            return False
        if skill_dir is None:
            return False

        try:
            shutil.rmtree(skill_dir)
        except Exception:
            return False

        # 从 manifest 移除技能条目
        def _remove(payload: dict) -> bool:
            payload.get("skills", {}).pop(skill_name, None)
            return True

        mutate_user_skill_manifest(
            self.swe_root,
            user_id,
            agent_id,
            _remove,
            source_id,
        )

        # 删除数据库记录
        await self.skill_registry.delete_skill(
            user_id,
            skill_name,
            source_id or "",
        )

        return True

    def migrate_skill_json_to_manifest(
        self,
        user_id: str,
        agent_id: str = "default",
        source_id: str | None = None,
        delete_skill_json: bool = False,
    ) -> dict[str, Any]:
        """迁移技能目录内 skill.json 字段到 workspace manifest.

        将以下字段从 skills/<技能名>/skill.json 合并到
        workspaces/<agent_id>/skill.json:
        - creator_id
        - creator_name
        - bbk_id
        - distributed_by
        - received_version
        - category_id

        Args:
            user_id: 用户 ID.
            agent_id: Agent ID，默认为 "default".
            source_id: 来源 ID.
            delete_skill_json: 是否删除技能目录内的 skill.json 文件.

        Returns:
            迁移结果统计：{"migrated": int, "skipped": int, "errors": list}
        """
        skills_dir = get_user_skills_dir(
            self.swe_root,
            user_id,
            agent_id,
            source_id,
        )

        if not skills_dir.exists():
            return {"migrated": 0, "skipped": 0, "errors": []}

        migrated = 0
        skipped = 0
        errors: list[str] = []

        # 辅助函数：创建合并函数，避免循环变量闭包问题
        def _make_merge_func(
            skill_name_arg: str,
            extra_fields_arg: dict,
        ) -> Callable[[dict], bool]:
            def _merge(payload: dict) -> bool:
                skills_dict = payload.setdefault("skills", {})
                existing = skills_dict.get(skill_name_arg) or {}

                # 合并到 metadata 层
                metadata = existing.get("metadata") or {}
                for key, value in extra_fields_arg.items():
                    # 不覆盖已存在的字段
                    if key not in metadata:
                        metadata[key] = value
                existing["metadata"] = metadata

                skills_dict[skill_name_arg] = existing
                return True

            return _merge

        for skill_dir in skills_dir.iterdir():
            if not skill_dir.is_dir():
                continue

            skill_name = skill_dir.name
            skill_json_path = skill_dir / "skill.json"

            # 没有 skill.json 则跳过
            if not skill_json_path.exists():
                skipped += 1
                continue

            # 读取技能目录内的 skill.json
            try:
                skill_data = json.loads(
                    skill_json_path.read_text(encoding="utf-8"),
                )
            except (json.JSONDecodeError, OSError) as e:
                errors.append(f"{skill_name}: 读取 skill.json 失败 - {e}")
                continue

            # 提取需要迁移的字段
            extra_fields = {}
            for field in [
                "creator_id",
                "creator_name",
                "bbk_id",
                "distributed_by",
                "received_version",
                "category_id",
            ]:
                if field in skill_data:
                    extra_fields[field] = skill_data[field]

            # 没有额外字段则跳过
            if not extra_fields:
                skipped += 1
                continue

            # 使用辅助函数创建 merge 函数
            _merge = _make_merge_func(skill_name, dict(extra_fields))

            try:
                mutate_user_skill_manifest(
                    self.swe_root,
                    user_id,
                    agent_id,
                    _merge,
                    source_id,
                )
            except Exception as e:
                errors.append(f"{skill_name}: 写入 manifest 失败 - {e}")
                continue

            # 删除技能目录内的 skill.json（如果请求）
            if delete_skill_json:
                try:
                    skill_json_path.unlink()
                except OSError as e:
                    errors.append(f"{skill_name}: 删除 skill.json 失败 - {e}")

            migrated += 1

        return {
            "migrated": migrated,
            "skipped": skipped,
            "errors": errors,
        }

    # ============ MCP 服务方法 ============

    @staticmethod
    def _apply_publish_update(
        target: MarketItem,
        req: PublishMCPRequest,
        now: str,
    ) -> None:
        """将发布请求的字段写入已存在的市场条目并更新版本号。

        用于同名复用 item_id 的两种场景：同 creator 自更新 / overwrite 接管。

        F1 修复：市场版本号独立于用户工作区版本号（spec R3）。
        续接同名 MCP 时一律 _bump_patch；req.version（用户本地版本）只作为
        source_user_version 写入快照元数据，不再覆盖 target.version。
        """
        target.version = _bump_patch(target.version)
        target.client_key = req.client_key
        target.name = req.name
        target.chinese_name = req.chinese_name
        target.description = req.description
        target.guidance = req.guidance
        target.creator_id = req.creator_id
        target.creator_name = req.creator_name
        target.category_id = req.category_id
        target.bbk_ids = req.bbk_ids
        # 重新发布已下架 MCP 时，更新 created_at 为当前时间
        if target.status == "inactive":
            target.created_at = now
        target.status = "active"
        target.updated_at = now

    @staticmethod
    def _resolve_source_user(
        req: PublishMCPRequest,
        item: MarketItem,
    ) -> tuple[str, str, str]:
        """解析 source_user_* 字段（兼容新旧调用方）。

        语义：
        - 调用方显式提供 source_user_version → 信任原值
        - 未提供 → 退化为 creator 兜底

        Returns:
            (source_user_id, source_user_name, source_user_version)
        """
        raw_src_ver = getattr(req, "source_user_version", "")
        if raw_src_ver:
            return (
                getattr(req, "source_user_id", ""),
                getattr(req, "source_user_name", ""),
                raw_src_ver,
            )
        return (
            getattr(req, "source_user_id", "") or req.creator_id,
            getattr(req, "source_user_name", "") or req.creator_name,
            req.version or item.version,
        )

    def _resolve_mcp_version_by_signature(
        self,
        source_id: str,
        item: MarketItem,
        version_svc: "MCPVersionService",
        mcp_dir: Path,
    ) -> bool:
        """F2: 按签名+历史决定 item.version，返回 version_unchanged 标志。

        - 同内容再同步 → 复用历史 version_id（R7 no-op）
        - 内容变化但 item.version 撞历史 → 自动 bump 避开
        """
        manifest = version_svc._load_manifest(source_id, item.item_id)
        new_sig = version_svc._calculate_signature(mcp_dir)
        existing_ids = {v.version_id for v in manifest.versions}

        if not manifest.versions:
            return False

        sorted_versions = sorted(
            manifest.versions,
            key=lambda v: v.created_at,
            reverse=True,
        )
        last_version = sorted_versions[0]
        if last_version.signature == new_sig:
            # 内容未变 → 复用历史最新版的 version_id（让 R7 no-op 接管）
            item.version = last_version.version_id
            return True
        if item.version in existing_ids:
            # 内容变了但 item.version 已在历史中 → 在历史最新版上 _bump_patch
            candidate = _bump_patch(last_version.version_id)
            for _ in range(100):
                if candidate not in existing_ids:
                    break
                candidate = _bump_patch(candidate)
            item.version = candidate
        return False

    def _create_mcp_version_snapshot(
        self,
        source_id: str,
        item: MarketItem,
        version_svc: "MCPVersionService",
        mcp_dir: Path,
        operator_id: str,
        operator_name: str,
        source_user_id: str,
        source_user_name: str,
        source_user_version: str,
    ) -> bool:
        """创建 MCP 版本快照，处理冲突与异常。

        Returns:
            version_unchanged 标志（快照回滚版本号时为 True）。
        """
        try:
            snapshot = version_svc.create_version_snapshot(
                source_id=source_id,
                item_id=item.item_id,
                mcp_dir=mcp_dir,
                version_id=item.version,
                creator=operator_id,
                creator_name=operator_name,
                description="",  # F2 修复：留空避免与头部版本号重复展示
                source_user_id=source_user_id,
                source_user_name=source_user_name,
                source_user_version=source_user_version,
            )
            if snapshot.version_id and snapshot.version_id != item.version:
                # 快照回滚版本号 = R7 no-op
                item.version = snapshot.version_id
                return True
        except ValueError as e:
            logger.warning(
                "MCP version snapshot conflict for item %s: %s",
                item.item_id,
                e,
            )
            raise MCPVersionConflictError(str(e)) from e
        except Exception as e:  # pylint: disable=broad-except
            logger.error(
                "Failed to create MCP version snapshot for item %s: %s",
                item.item_id,
                e,
                exc_info=True,
            )
        return False

    async def publish_mcp(
        self,
        source_id: str,
        req: PublishMCPRequest,
    ) -> tuple[MarketItem, bool]:
        """发布 MCP 到市场（R4：按 name 续接，不再因同名拒绝）.

        Args:
            source_id: 来源 ID。
            req: 发布请求体（含 source_user_* / operator_* 字段，兼容旧调用方）。

        Returns:
            (MarketItem, version_unchanged): 商品条目与版本是否未变化的标志。
        """
        items = load_index(self.marketplace_root, source_id)

        # R4: 按 name 唯一查找已有条目（不再区分 creator）
        existing = next(
            (i for i in items if i.item_type == "mcp" and i.name == req.name),
            None,
        )

        # 未显式 overwrite 时抛冲突异常，由前端弹窗让用户确认
        if existing is not None and not req.overwrite:
            raise MCPNameConflictError(
                existing_item_id=existing.item_id,
                existing_name=existing.name,
                existing_creator_id=existing.creator_id,
                existing_creator_name=existing.creator_name,
                existing_version=existing.version,
            )

        now = datetime.now(timezone.utc).isoformat()
        if existing is not None:
            # 同名（已确认覆盖） → 续接到现有条目
            self._apply_publish_update(existing, req, now)
            item = existing
        else:
            # F1 修复：市场首发版本号固定为 1.0.0（spec R3，市场版本独立于用户工作区）。
            # req.version（用户本地版本）只作为 source_user_version 写入快照。
            initial_version = "1.0.0"
            item = MarketItem(
                item_id=str(uuid.uuid4()),
                item_type="mcp",
                client_key=req.client_key,
                name=req.name,
                chinese_name=req.chinese_name,
                description=req.description,
                guidance=req.guidance,
                version=initial_version,
                creator_id=req.creator_id,
                creator_name=req.creator_name,
                category_id=req.category_id,
                bbk_ids=req.bbk_ids,
                status="active",
                created_at=now,
                updated_at=now,
            )
            items.append(item)

        # 保存 MCP 配置文件
        mcp_config = {
            "client_key": req.client_key,
            "config": req.config,
        }
        save_mcp_config(
            self.marketplace_root,
            source_id,
            item.item_id,
            mcp_config,
        )

        # T9: 创建 MCP 版本快照（与 Skill 对称）
        # F3 修复：先建快照、成功后再 save_index；ValueError 转 MCPVersionConflictError
        # 由路由层转 409 让前端可见。
        from .mcp_version_service import MCPVersionService

        mcp_dir = get_mcp_dir(self.marketplace_root, source_id, item.item_id)
        version_svc = MCPVersionService(self.marketplace_root)

        source_user_id, source_user_name, source_user_version = (
            self._resolve_source_user(req, item)
        )
        # operator 未传时回退到 creator（保持 created_by 永远有值，便于 R8 回退）
        operator_id = getattr(req, "operator_id", "") or req.creator_id
        operator_name = getattr(req, "operator_name", "") or req.creator_name

        # F2: 按签名+历史决定版本号
        version_unchanged = self._resolve_mcp_version_by_signature(
            source_id,
            item,
            version_svc,
            mcp_dir,
        )

        snapshot_unchanged = self._create_mcp_version_snapshot(
            source_id,
            item,
            version_svc,
            mcp_dir,
            operator_id,
            operator_name,
            source_user_id,
            source_user_name,
            source_user_version,
        )
        if snapshot_unchanged:
            version_unchanged = True

        # 更新索引（在快照创建之后，以便 item.version 反映最终值）
        save_index(self.marketplace_root, source_id, items)

        # 记录操作日志
        if self.db.is_connected:
            try:
                await self.db.execute(
                    _LOG_MARKET_OP_SQL,
                    (
                        source_id,
                        req.creator_id,
                        req.creator_name,
                        "publish",
                        "mcp",
                        item.item_id,
                        item.name,
                        None,
                        None,
                        None,
                    ),
                )
            except Exception as e:
                logger.warning("Failed to log MCP publish operation: %s", e)

        return item, version_unchanged

    async def list_mcp_items(
        self,
        source_id: str,
        user_bbk_id: str,
        category_id: Optional[int] = None,
        bbk_ids: Optional[list[str]] = None,
    ) -> list[MarketMCPItem]:
        """列出市场 MCP 条目。

        Args:
            source_id: 来源 ID。
            user_bbk_id: 用户 bbk_id（保留参数兼容性，不再用于过滤）。
            category_id: 可选的分类 ID 过滤。
            bbk_ids: 可选的分行 ID 过滤（交集匹配）。

        Returns:
            MCP 条目列表（含调用统计）。
        """
        items = load_index(self.marketplace_root, source_id)
        mcp_items = [
            i for i in items if i.item_type == "mcp" and i.status == "active"
        ]
        mcp_items = _sort_items_by_updated_at_desc(mcp_items)

        if category_id is not None:
            mcp_items = [i for i in mcp_items if i.category_id == category_id]

        # 按 bbk_ids 过滤（MCP 的 bbk_ids 与请求的 bbk_ids 有交集）
        if bbk_ids is not None and len(bbk_ids) > 0:
            mcp_items = [
                i
                for i in mcp_items
                if i.bbk_ids and any(b in i.bbk_ids for b in bbk_ids)
            ]

        result = []
        for item in mcp_items:
            call_count, user_count = await self._get_mcp_stats(
                item.client_key,
                source_id,
            )
            result.append(
                MarketMCPItem(
                    item_id=item.item_id,
                    client_key=item.client_key,
                    name=item.name,
                    chinese_name=item.chinese_name,
                    description=item.description,
                    guidance=item.guidance,
                    version=item.version,
                    creator_id=item.creator_id,
                    creator_name=_decode_creator_name(item.creator_name),
                    category_id=item.category_id,
                    bbk_ids=item.bbk_ids,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                    call_count=call_count,
                    user_count=user_count,
                ),
            )
        return result

    async def get_mcp_detail(
        self,
        source_id: str,
        item_id: str,
        user_bbk_id: str,
    ) -> Optional[MarketMCPDetail]:
        """获取 MCP 详情（含配置和用户统计）。

        Args:
            source_id: 来源 ID。
            item_id: 条目 ID。
            user_bbk_id: 用户 bbk_id，用于权限过滤。

        Returns:
            MCP 详情，不存在或无权限返回 None。
        """
        items = load_index(self.marketplace_root, source_id)
        item = next(
            (
                i
                for i in items
                if i.item_id == item_id and i.item_type == "mcp"
            ),
            None,
        )
        if item is None or not _item_visible(item, user_bbk_id):
            return None

        # 加载 MCP 配置
        mcp_config = load_mcp_config(self.marketplace_root, source_id, item_id)
        if mcp_config is None:
            return None

        call_count, user_count = await self._get_mcp_stats(
            item.client_key,
            source_id,
        )
        user_stats = await self._get_mcp_user_stats(item.client_key, source_id)

        # 获取并脱敏敏感字段
        config_data = normalize_mcp_config_data(
            mcp_config.get("config", {}),
        )
        masked_env = {
            k: _mask_env_value(v)
            for k, v in config_data.get("env", {}).items()
        }
        masked_headers = {
            k: _mask_env_value(v)
            for k, v in config_data.get("headers", {}).items()
        }

        return MarketMCPDetail(
            item_id=item.item_id,
            client_key=item.client_key,
            name=item.name,
            chinese_name=item.chinese_name,
            description=item.description,
            guidance=item.guidance,
            version=item.version,
            creator_id=item.creator_id,
            creator_name=_decode_creator_name(item.creator_name),
            category_id=item.category_id,
            bbk_ids=item.bbk_ids,
            created_at=item.created_at,
            updated_at=item.updated_at,
            call_count=call_count,
            user_count=user_count,
            config=MCPConfigDetail(
                transport=config_data.get("transport", "stdio"),
                url=config_data.get("url", ""),
                headers=masked_headers,
                command=config_data.get("command", ""),
                args=config_data.get("args", []),
                env=masked_env,
                cwd=config_data.get("cwd", ""),
                lazy_load=config_data.get("lazy_load", False),
            ),
            user_stats=user_stats,
        )

    @staticmethod
    def _find_user_mcp_name_conflict(
        user_config_path: Path,
        mcp_name: str,
    ) -> str | None:
        """检查用户本地是否已有同名且用户自建的 MCP。

        市场分发的 MCP（source 以 "marketplace:" 开头）允许被覆盖分发；
        只有用户自己创建的 MCP 才视为冲突，拒绝分发以避免覆盖。

        Args:
            user_config_path: 用户 agent.json 路径。
            mcp_name: 待分发的 MCP 名称。

        Returns:
            冲突条目的 client_key；无冲突返回 None。
        """
        if not user_config_path.exists():
            return None
        try:
            raw = json.loads(
                user_config_path.read_text(encoding="utf-8"),
            )
        except (json.JSONDecodeError, OSError):
            return None

        clients = raw.get("mcp", {}).get("clients", {})
        for key, cfg in clients.items():
            if not isinstance(cfg, dict):
                continue
            if cfg.get("name") != mcp_name:
                continue
            # 市场分发的 MCP（source 以 "marketplace:" 开头）允许覆盖
            source = cfg.get("source", "")
            if isinstance(source, str) and source.startswith("marketplace:"):
                continue
            # 用户自建的 MCP，拒绝分发
            return key
        return None

    async def distribute_mcp(
        self,
        source_id: str,
        item_id: str,
        operator_id: str,
        operator_name: str,
        req: MCPDistributionRequest,
    ) -> MCPDistributionResponse:
        """分发 MCP 到目标租户。

        Args:
            source_id: 来源 ID。
            item_id: 条目 ID。
            operator_id: 操作者 ID。
            operator_name: 操作者名称。
            req: 分发请求体。

        Returns:
            分发结果（逐租户返回）。

        Raises:
            ValueError: 条目不存在。
        """
        items = load_index(self.marketplace_root, source_id)
        item = next(
            (
                i
                for i in items
                if i.item_id == item_id and i.item_type == "mcp"
            ),
            None,
        )
        if item is None:
            raise ValueError(
                f"MCP item {item_id} not found in source {source_id}",
            )

        # 批量查询用户信息（tenant_name, bbk_id）
        user_info_map: dict[str, dict] = {}
        if self.db.is_connected and req.target_tenant_ids:
            try:
                placeholders = ",".join(["%s"] * len(req.target_tenant_ids))
                sql = _QUERY_USERS_BY_TENANT_IDS_SQL.format(
                    placeholders=placeholders,
                )
                rows = await self.db.fetch_all(
                    sql,
                    (source_id, *req.target_tenant_ids),
                )
                for row in rows:
                    user_info_map[row["tenant_id"]] = {
                        "tenant_name": row.get("tenant_name", ""),
                        "bbk_id": row.get("bbk_id", ""),
                    }
            except Exception as e:
                logger.warning(
                    "Failed to query user info for MCP distribute: %s",
                    e,
                )

        results: list[MCPDistributionTenantResult] = []

        for tenant_id in req.target_tenant_ids:
            user_info = user_info_map.get(tenant_id, {})
            tenant_name = user_info.get("tenant_name", "")
            try:
                effective_user_id = resolve_effective_user_id(
                    tenant_id,
                    source_id,
                )
                user_root = migrate_legacy_scope_dir_if_needed(
                    self.swe_root,
                    effective_user_id,
                )
                user_config_path = (
                    user_root / "workspaces" / "default" / "agent.json"
                )
                bootstrapped = not user_config_path.exists()

                # 同名冲突检测：若用户本地已有同 name 且 creator_id 非空的 MCP
                # （即"是用户自己创建/管理的 MCP"），拒绝分发，避免覆盖。
                # creator_id 为空表示是早期未携带来源信息的市场分发，可被覆盖。
                conflict_client_key = self._find_user_mcp_name_conflict(
                    user_config_path,
                    item.name,
                )
                if conflict_client_key is not None:
                    results.append(
                        MCPDistributionTenantResult(
                            tenant_id=tenant_id,
                            tenant_name=tenant_name,
                            success=False,
                            error=(f"用户已有同名 MCP " f'"{item.name}"'),
                        ),
                    )
                    continue

                effective_client_key = copy_mcp_to_user(
                    marketplace_root=self.marketplace_root,
                    source_id=source_id,
                    item_id=item_id,
                    swe_root=self.swe_root,
                    user_id=tenant_id,
                    client_key=item.client_key,
                    distributed_by=operator_id,
                    version=item.version,
                    creator_id=item.creator_id,
                    creator_name=item.creator_name,
                    mcp_name=item.name,
                )

                # 获取用户信息（如果查询不到则为空）
                bbk_id = user_info.get("bbk_id", "")

                # 记录分发日志
                if self.db.is_connected:
                    try:
                        await self.db.execute(
                            _LOG_MARKET_OP_SQL,
                            (
                                source_id,
                                operator_id,
                                operator_name,
                                "distribute",
                                "mcp",
                                item_id,
                                item.name,
                                tenant_id,
                                tenant_name,
                                bbk_id,
                            ),
                        )
                    except Exception as e:
                        logger.warning(
                            "Failed to log MCP distribute operation: %s",
                            e,
                        )
                results.append(
                    MCPDistributionTenantResult(
                        tenant_id=tenant_id,
                        tenant_name=tenant_name,
                        success=True,
                        bootstrapped=bootstrapped,
                        default_agent_updated=[effective_client_key],
                    ),
                )
            except Exception as e:
                logger.warning(
                    "Failed to copy MCP to user %s: %s",
                    tenant_id,
                    e,
                )
                results.append(
                    MCPDistributionTenantResult(
                        tenant_id=tenant_id,
                        tenant_name=tenant_name,
                        success=False,
                        error=str(e),
                    ),
                )

        return MCPDistributionResponse(
            source_agent_id=item_id,
            results=results,
        )

    async def delete_mcp(
        self,
        source_id: str,
        item_id: str,
        operator_id: str = "",
        operator_name: str = "",
    ) -> bool:
        """删除市场 MCP 条目。

        Args:
            source_id: 来源 ID。
            item_id: 条目 ID。
            operator_id: 操作者 ID（可选）。
            operator_name: 操作者名称（可选）。

        Returns:
            True 表示删除成功，False 表示条目不存在。
        """
        items = load_index(self.marketplace_root, source_id)
        item = next(
            (
                i
                for i in items
                if i.item_id == item_id and i.item_type == "mcp"
            ),
            None,
        )
        if item is None:
            return False

        # 从索引中移除
        items.remove(item)
        save_index(self.marketplace_root, source_id, items)

        # 删除配置目录
        mcp_dir = get_mcp_dir(self.marketplace_root, source_id, item_id)
        if mcp_dir.exists():
            shutil.rmtree(mcp_dir)

        # 记录删除日志
        if self.db.is_connected:
            try:
                await self.db.execute(
                    _LOG_MARKET_OP_SQL,
                    (
                        source_id,
                        operator_id,
                        operator_name,
                        "delete",
                        "mcp",
                        item_id,
                        item.name,
                        None,
                        None,
                        None,
                    ),
                )
            except Exception as e:
                logger.warning("Failed to log MCP delete operation: %s", e)

        return True

    def update_mcp_metadata(
        self,
        *,
        source_id: str,
        item_id: str,
        chinese_name: str | None,
        description: str | None,
        guidance: str | None,
        bbk_ids: list[str],
    ) -> MarketItem:
        """仅更新 MCP 市场条目的展示元数据。"""
        items = load_index(self.marketplace_root, source_id)
        item = next(
            (
                i
                for i in items
                if i.item_id == item_id and i.item_type == "mcp"
            ),
            None,
        )
        if item is None:
            raise FileNotFoundError(f"MCP item '{item_id}' not found")

        item.chinese_name = chinese_name or ""
        item.description = description or ""
        item.guidance = guidance or ""
        item.bbk_ids = bbk_ids
        item.updated_at = datetime.now(timezone.utc).isoformat()
        save_index(self.marketplace_root, source_id, items)
        return item

    def _update_market_item_cn_name(
        self,
        source_id: str,
        item_id: str,
        chinese_name: str,
    ) -> MarketItem:
        """更新 index.json 中技能条目的 chinese_name."""
        items = load_index(self.marketplace_root, source_id)
        item = next(
            (
                i
                for i in items
                if i.item_id == item_id and i.item_type == "skill"
            ),
            None,
        )
        if item is None:
            raise ValueError(f"Skill item '{item_id}' not found")

        item.chinese_name = chinese_name
        item.updated_at = datetime.now(timezone.utc).isoformat()
        save_index(self.marketplace_root, source_id, items)
        return item

    def _check_cn_name_exists_in_frontmatter(self, skill_dir: Path) -> bool:
        """检查 SKILL.md frontmatter 中是否存在 cn_name 字段."""
        skill_md_path = skill_dir / "SKILL.md"
        if not skill_md_path.exists():
            return False

        try:
            content = skill_md_path.read_text(encoding="utf-8")
            fm = parse_frontmatter(content)
            metadata = fm.get("metadata", {})
            return "cn_name" in metadata
        except (OSError, UnicodeDecodeError):
            return False

    def _update_frontmatter_cn_name_if_exists(
        self,
        skill_dir: Path,
        cn_name: str,
    ) -> bool:
        """条件更新 SKILL.md frontmatter，只有存在 cn_name 时才更新."""
        if self._check_cn_name_exists_in_frontmatter(skill_dir):
            self._update_cn_name_in_frontmatter(skill_dir, cn_name)
            return True
        return False

    def _update_skill_manifest_cn_name_only(
        self,
        user_id: str,
        skill_name: str,
        cn_name: str,
        agent_id: str = "default",
        source_id: str | None = None,
    ) -> bool:
        """仅更新 manifest 中的 cn_name 字段."""
        now = datetime.now(timezone.utc).isoformat()
        manifest_path = get_user_skill_manifest_path(
            self.swe_root,
            user_id,
            agent_id,
            source_id,
        )

        def _update(payload: dict) -> bool:
            entry = payload.get("skills", {}).get(skill_name)
            if entry is None:
                return False
            metadata = entry.get("metadata", {})
            metadata["cn_name"] = cn_name
            metadata["updated_at"] = now
            entry["metadata"] = metadata
            entry["updated_at"] = now
            return True

        updated = mutate_user_skill_manifest(
            self.swe_root,
            user_id,
            agent_id,
            _update,
            source_id,
        )
        if updated:
            logger.info(
                "Updated user skill manifest cn_name: "
                "user=%s source=%s agent=%s skill=%s path=%s cn_name=%s",
                user_id,
                source_id,
                agent_id,
                skill_name,
                manifest_path,
                cn_name,
            )
        else:
            logger.warning(
                "Skipped user skill manifest cn_name update: "
                "user=%s source=%s agent=%s skill=%s path=%s "
                "reason=skill_entry_not_found",
                user_id,
                source_id,
                agent_id,
                skill_name,
                manifest_path,
            )
        return updated

    def _sync_cn_name_to_user_workspace(
        self,
        tenant_id: str,
        skill_name: str,
        cn_name: str,
        source_id: str,
    ) -> bool:
        """同步更新单个用户 workspace 的技能名称文件."""
        try:
            skills_dir = get_user_skills_dir(
                self.swe_root,
                tenant_id,
                "default",
                source_id,
            )
            skill_dir = skills_dir / skill_name
            if not skill_dir.exists():
                logger.warning(
                    "Skipped user skill file cn_name sync: "
                    "user=%s source=%s skill=%s path=%s "
                    "reason=skill_dir_not_found",
                    tenant_id,
                    source_id,
                    skill_name,
                    skill_dir,
                )
                return False

            # 更新 manifest
            manifest_updated = self._update_skill_manifest_cn_name_only(
                tenant_id,
                skill_name,
                cn_name,
                "default",
                source_id,
            )
            # 条件更新 SKILL.md
            frontmatter_updated = self._update_frontmatter_cn_name_if_exists(
                skill_dir,
                cn_name,
            )
            logger.info(
                "Synced user skill cn_name files: "
                "user=%s source=%s skill=%s skill_dir=%s "
                "manifest_updated=%s frontmatter_updated=%s cn_name=%s",
                tenant_id,
                source_id,
                skill_name,
                skill_dir,
                manifest_updated,
                frontmatter_updated,
                cn_name,
            )
            return manifest_updated
        except Exception as e:
            logger.warning(
                "Failed to sync cn_name to user %s: %s",
                tenant_id,
                e,
            )
            return False

    async def update_skill_cn_name(
        self,
        source_id: str,
        item_id: str,
        skill_id: str,
        skill_name: str,
        chinese_name: str,
        sync_to_users: bool,
        target_user_ids: list[str],
    ) -> dict:
        """更新市场技能中文名，可选同步用户空间."""
        # 1. 更新市场条目
        item = self._update_market_item_cn_name(
            source_id,
            item_id,
            chinese_name,
        )

        # 2. 同步更新 swe_marketplace_skills 表
        if self.db.is_connected:
            await self.db.execute(
                """UPDATE swe_marketplace_skills
                SET cn_name = %s, updated_at = NOW()
                WHERE source_id = %s AND item_id = %s""",
                (chinese_name, source_id, item_id),
            )

        # 3. 若 sync_to_users=True，同步用户空间
        synced_users = 0
        errors = []
        distribution_count = 0

        if sync_to_users:
            # 获取已分发用户列表
            distributions = await self.get_distributions(
                source_id,
                item_id,
                "skill",
            )
            distribution_count = len(distributions)
            users_to_sync = target_user_ids or [
                d.target_user_id for d in distributions
            ]

            for user_id in users_to_sync:
                # 更新数据库
                if self.skill_registry.is_connected():
                    await self.skill_registry.update_cn_name_by_skill_id(
                        skill_id,
                        user_id,
                        chinese_name,
                    )
                # 更新用户 workspace 文件
                success = self._sync_cn_name_to_user_workspace(
                    user_id,
                    skill_name,
                    chinese_name,
                    source_id,
                )
                if success:
                    synced_users += 1
                else:
                    errors.append(
                        {
                            "user_id": user_id,
                            "reason": "workspace sync failed",
                        },
                    )

        return {
            "success": True,
            "market_updated": True,
            "synced_users": synced_users,
            "skipped_users": (
                distribution_count - synced_users if sync_to_users else 0
            ),
            "errors": errors,
        }

    async def _get_mcp_stats(
        self,
        client_key: str,
        source_id: str,
    ) -> tuple[int, int]:
        """获取 MCP 调用统计。

        Args:
            client_key: MCP 客户端标识。
            source_id: 来源 ID。

        Returns:
            (调用次数, 用户数)。
        """
        if not self.db.is_connected:
            return 0, 0
        try:
            row = await self.db.fetch_one(
                _TRACING_STATS_MCP_SQL,
                (client_key, source_id),
            )
            if row:
                return int(row.get("call_count", 0)), int(
                    row.get("user_count", 0),
                )
        except Exception as e:
            logger.warning("Failed to get MCP stats for %s: %s", client_key, e)
        return 0, 0

    async def _get_mcp_user_stats(
        self,
        client_key: str,
        source_id: str,
    ) -> list[MCPUserStat]:
        """获取 MCP 用户统计明细。

        Args:
            client_key: MCP 客户端标识。
            source_id: 来源 ID。

        Returns:
            用户统计列表（最多 100 条）。
        """
        if not self.db.is_connected:
            return []
        try:
            rows = await self.db.fetch_all(
                _TRACING_USER_STATS_MCP_SQL,
                (client_key, source_id),
            )
            return [
                MCPUserStat(
                    user_id=r["user_id"],
                    user_name=_decode_creator_name(r.get("user_name", "")),
                    call_count=int(r["call_count"]),
                )
                for r in rows
            ]
        except Exception as e:
            logger.warning(
                "Failed to get MCP user stats for %s: %s",
                client_key,
                e,
            )
        return []

    # ============ 撤回服务方法 ============

    async def get_distributions(
        self,
        source_id: str,
        item_id: str,
        item_type: str,
        skill_name: str | None = None,
    ) -> list[DistributionRecord]:
        """查询分发记录.

        Args:
            source_id: 来源 ID.
            item_id: 条目 ID.
            item_type: 条目类型（skill 或 mcp）.
            skill_name: 技能名称（可选，用于查询当前实际持有的用户）.

        Returns:
            分发记录列表.
        """
        if not self.db.is_connected:
            return []
        try:
            # 如果提供了 skill_name，查询 swe_skills 表获取当前实际持有技能的用户
            if skill_name and item_type == "skill":
                # 规范化 skill_name，与 swe_skills 表存储格式一致
                normalized_skill_name = normalize_skill_name(skill_name)
                rows = await self.db.fetch_all(
                    _QUERY_DISTRIBUTED_USERS_SQL,
                    (normalized_skill_name, source_id),
                )
                return [
                    DistributionRecord(
                        target_user_id=r["tenant_id"],
                        target_user_name=r.get("tenant_name") or "",
                        target_bbk_id=r.get("bbk_id") or "",
                        distributed_at=None,
                    )
                    for r in rows
                ]
            # 否则查询操作日志表
            rows = await self.db.fetch_all(
                _QUERY_DISTRIBUTIONS_SQL,
                (source_id, item_id, item_type),
            )
            return [
                DistributionRecord(
                    target_user_id=r["target_user_id"],
                    target_user_name=r.get("target_user_name") or "",
                    target_bbk_id=r.get("target_bbk_id") or "",
                    distributed_at=(
                        r.get("created_at").isoformat()
                        if r.get("created_at")
                        else None
                    ),
                )
                for r in rows
            ]
        except Exception as e:
            logger.warning(
                "Failed to get distributions for %s: %s",
                item_id,
                e,
            )
        return []

    def _build_recall_response(
        self,
        item_id: str,
        recalled_count: int,
        results: list[RecallResultItem],
    ) -> RecallResponse:
        """统一组装撤回响应."""
        return RecallResponse(
            recalled_count=recalled_count,
            failed_count=len(results) - recalled_count,
            results=results,
            item_id=item_id,
        )

    async def _query_user_info_map(
        self,
        source_id: str,
        user_ids: list[str],
        item_type: str,
    ) -> dict[str, dict[str, str]]:
        """批量查询用户名称和机构信息."""
        if not self.db.is_connected or not user_ids:
            return {}
        try:
            placeholders = ",".join(["%s"] * len(user_ids))
            sql = _QUERY_USERS_BY_TENANT_IDS_SQL.format(
                placeholders=placeholders,
            )
            rows = await self.db.fetch_all(sql, (source_id, *user_ids))
        except Exception as e:
            logger.warning(
                "Failed to query user info for %s recall: %s",
                item_type,
                e,
            )
            return {}

        user_info_map: dict[str, dict[str, str]] = {}
        for row in rows:
            user_info_map[row["tenant_id"]] = {
                "tenant_name": row.get("tenant_name", ""),
                "bbk_id": row.get("bbk_id", ""),
            }
        return user_info_map

    def _resolve_users_to_recall(
        self,
        target_user_ids: list[str] | None,
        dist_map: dict[str, DistributionRecord],
    ) -> list[str]:
        """优先使用显式指定用户，否则回退到分发记录中的用户."""
        if target_user_ids:
            return target_user_ids
        return list(dist_map.keys())

    def _resolve_recall_target_identity(
        self,
        user_id: str,
        dist_map: dict[str, DistributionRecord],
        user_info_map: dict[str, dict[str, str]],
    ) -> tuple[str, str]:
        """优先使用分发记录中的用户信息，缺失时回退数据库补充."""
        dist = dist_map.get(user_id)
        target_user_name = (
            dist.target_user_name if dist and dist.target_user_name else ""
        )
        target_bbk_id = (
            dist.target_bbk_id if dist and dist.target_bbk_id else ""
        )
        if target_user_name or target_bbk_id:
            return target_user_name, target_bbk_id

        user_info = user_info_map.get(user_id, {})
        return (
            user_info.get("tenant_name", ""),
            user_info.get("bbk_id", ""),
        )

    async def _log_recall_operation(
        self,
        source_id: str,
        operator_id: str,
        operator_name: str,
        item_type: str,
        item_id: str,
        item_name: str,
        user_id: str,
        target_user_name: str,
        target_bbk_id: str,
    ) -> None:
        """记录撤回操作日志，日志失败不影响主流程."""
        if not self.db.is_connected:
            return
        try:
            await self.db.execute(
                _LOG_MARKET_OP_SQL,
                (
                    source_id,
                    operator_id,
                    operator_name,
                    "recall",
                    item_type,
                    item_id,
                    item_name,
                    user_id,
                    target_user_name,
                    target_bbk_id,
                ),
            )
        except Exception as e:
            logger.warning("Failed to log recall operation: %s", e)

    async def _execute_recall_for_users(
        self,
        source_id: str,
        item_id: str,
        item_name: str,
        item_type: str,
        operator_id: str,
        operator_name: str,
        user_ids: list[str],
        dist_map: dict[str, DistributionRecord],
        user_info_map: dict[str, dict[str, str]],
        recall_one: Callable[[str], Any],
        warning_message: str,
    ) -> RecallResponse:
        """执行按用户维度的撤回流程并汇总结果."""
        results: list[RecallResultItem] = []
        recalled_count = 0

        for user_id in user_ids:
            target_user_name, target_bbk_id = (
                self._resolve_recall_target_identity(
                    user_id,
                    dist_map,
                    user_info_map,
                )
            )
            try:
                failure_reason = await recall_one(user_id)
                if failure_reason:
                    results.append(
                        RecallResultItem(
                            user_id=user_id,
                            success=False,
                            reason=failure_reason,
                        ),
                    )
                    continue

                await self._log_recall_operation(
                    source_id,
                    operator_id,
                    operator_name,
                    item_type,
                    item_id,
                    item_name,
                    user_id,
                    target_user_name,
                    target_bbk_id,
                )
                results.append(
                    RecallResultItem(user_id=user_id, success=True),
                )
                recalled_count += 1
            except Exception as e:
                logger.warning(warning_message, user_id, e)
                results.append(
                    RecallResultItem(
                        user_id=user_id,
                        success=False,
                        reason=str(e),
                    ),
                )

        return self._build_recall_response(item_id, recalled_count, results)

    def _remove_skill_manifest_entry(
        self,
        user_id: str,
        skill_name: str,
        source_id: str | None,
        agent_id: str = "default",
    ) -> None:
        """从运行时 manifest 中移除技能记录."""

        def _remove(payload: dict, _name: str = skill_name) -> bool:
            payload.get("skills", {}).pop(_name, None)
            return True

        mutate_user_skill_manifest(
            self.swe_root,
            user_id,
            agent_id,
            _remove,
            source_id,
        )

    def _skill_source_matches(
        self,
        user_id: str,
        skill_name: str,
        source_id: str | None,
        expected_source_prefix: str,
        agent_id: str = "default",
    ) -> bool:
        """检查技能来源是否属于当前市场条目."""
        manifest = read_user_skill_manifest(
            self.swe_root,
            user_id,
            agent_id,
            source_id,
        )
        skill_entry = manifest.get("skills", {}).get(skill_name, {})
        source = skill_entry.get("source", "") or skill_entry.get(
            "metadata",
            {},
        ).get("source", "")
        return source.startswith(expected_source_prefix)

    async def _recall_skill_from_user(
        self,
        user_id: str,
        source_id: str,
        skill_name: str,
        expected_source_prefix: str | None = None,
        reload_source_id: str | None = None,
    ) -> str | None:
        """撤回单个用户的技能，失败时返回原因."""
        if expected_source_prefix and not self._skill_source_matches(
            user_id,
            skill_name,
            source_id,
            expected_source_prefix,
        ):
            return "not_from_this_marketplace"

        deleted = await self.delete_skill(
            user_id,
            skill_name,
            "default",
            source_id,
        )
        if not deleted:
            return "skill_not_found"
        await self._trigger_agent_reload(
            user_id,
            "default",
            reload_source_id,
        )
        return None

    def _get_user_agent_config_path(
        self,
        user_id: str,
        source_id: str | None,
        agent_id: str = "default",
    ) -> Path:
        """获取用户 agent 配置路径."""
        effective_user_id = resolve_effective_user_id(user_id, source_id)
        user_root = migrate_legacy_scope_dir_if_needed(
            self.swe_root,
            effective_user_id,
        )
        return user_root / "workspaces" / agent_id / "agent.json"

    def _load_user_agent_config(
        self,
        user_config_path: Path,
    ) -> dict[str, Any] | None:
        """读取用户 agent 配置，解析失败时返回空值."""
        try:
            return json.loads(user_config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    async def _recall_mcp_from_user(
        self,
        user_id: str,
        source_id: str,
        mcp_name: str,
        expected_source_prefix: str | None = None,
        reload_source_id: str | None = None,
    ) -> str | None:
        """撤回单个用户的 MCP，失败时返回原因.

        使用 mcp_name（市场内唯一）作为身份标识，遍历用户配置中
        所有 MCP 条目按 name 字段匹配，与 _find_user_mcp_name_conflict
        及 copy_mcp_to_user 的逻辑保持一致。

        Args:
            user_id: 用户 ID。
            source_id: 来源 ID。
            mcp_name: 待撤回的 MCP 名称（市场内唯一）。
            expected_source_prefix: 可选，验证 source 前缀。
            reload_source_id: 可选，触发重载的 source_id。

        Returns:
            失败原因字符串；成功返回 None。
        """
        user_config_path = self._get_user_agent_config_path(user_id, source_id)
        if not user_config_path.exists():
            return "agent_config_not_found"

        user_config = self._load_user_agent_config(user_config_path)
        if user_config is None:
            return "invalid_agent_config"

        mcp_section = user_config.get("mcp")
        if not isinstance(mcp_section, dict):
            return "mcp_not_found"
        mcp_clients = mcp_section.get("clients")
        if not isinstance(mcp_clients, dict):
            return "mcp_not_found"

        # 按 name 字段遍历查找（mcp_name 在市场内唯一）
        target_key: str | None = None
        for key, cfg in mcp_clients.items():
            if not isinstance(cfg, dict):
                continue
            if cfg.get("name") == mcp_name:
                target_key = key
                break

        if target_key is None:
            return "mcp_not_found"

        if expected_source_prefix:
            source = mcp_clients.get(target_key, {}).get("source", "")
            if not source.startswith(expected_source_prefix):
                return "not_from_this_marketplace"

        mcp_clients.pop(target_key, None)
        mcp_section["clients"] = mcp_clients
        user_config["updated_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_write_json(user_config_path, user_config)
        await self._trigger_agent_reload(
            user_id,
            "default",
            reload_source_id,
        )
        return None

    def _require_market_item(
        self,
        source_id: str,
        item_id: str,
        item_type: str,
        item_label: str,
    ) -> MarketItem:
        """按类型读取市场条目，不存在时抛出异常."""
        item = next(
            (
                market_item
                for market_item in load_index(self.marketplace_root, source_id)
                if market_item.item_id == item_id
                and market_item.item_type == item_type
            ),
            None,
        )
        if item is None:
            raise ValueError(f"{item_label} item {item_id} not found")
        return item

    async def recall_skill(
        self,
        source_id: str,
        item_id: str | None,
        operator_id: str,
        operator_name: str,
        req: RecallRequest,
    ) -> RecallResponse:
        """撤回已分发的技能.

        Args:
            source_id: 来源 ID.
            item_id: 条目 ID（可选，按名称撤回时不需要）.
            operator_id: 操作者 ID.
            operator_name: 操作者名称.
            req: 撤回请求体.

        Returns:
            撤回结果.
        """
        if req.skill_name:
            if not req.target_user_ids:
                return self._build_recall_response("", 0, [])

            user_info_map = await self._query_user_info_map(
                source_id,
                req.target_user_ids,
                "skill",
            )
            safe_skill_name = normalize_skill_name(req.skill_name)
            return await self._execute_recall_for_users(
                source_id=source_id,
                item_id="",
                item_name=req.skill_name,
                item_type="skill",
                operator_id=operator_id,
                operator_name=operator_name,
                user_ids=req.target_user_ids,
                dist_map={},
                user_info_map=user_info_map,
                recall_one=lambda user_id: self._recall_skill_from_user(
                    user_id,
                    source_id,
                    safe_skill_name,
                    reload_source_id=source_id,
                ),
                warning_message="Failed to recall skill from user %s: %s",
            )

        if not item_id:
            raise ValueError("item_id or skill_name is required")

        item = self._require_market_item(source_id, item_id, "skill", "Skill")
        distributions = await self.get_distributions(
            source_id,
            item_id,
            "skill",
        )
        dist_map = {d.target_user_id: d for d in distributions}
        users_to_recall = self._resolve_users_to_recall(
            req.target_user_ids,
            dist_map,
        )

        if not users_to_recall:
            return self._build_recall_response(item_id, 0, [])

        user_info_map = await self._query_user_info_map(
            source_id,
            users_to_recall,
            "skill",
        )
        safe_skill_name = normalize_skill_name(item.name)
        expected_source_prefix = None
        if not req.force:
            expected_source_prefix = f"marketplace:{item_id}"

        return await self._execute_recall_for_users(
            source_id=source_id,
            item_id=item_id,
            item_name=item.name,
            item_type="skill",
            operator_id=operator_id,
            operator_name=operator_name,
            user_ids=users_to_recall,
            dist_map=dist_map,
            user_info_map=user_info_map,
            recall_one=lambda user_id: self._recall_skill_from_user(
                user_id,
                source_id,
                safe_skill_name,
                expected_source_prefix,
                None,
            ),
            warning_message="Failed to recall skill from user %s: %s",
        )

    async def recall_mcp(
        self,
        source_id: str,
        item_id: str | None,
        operator_id: str,
        operator_name: str,
        req: RecallRequest,
    ) -> RecallResponse:
        """撤回已分发的 MCP.

        Args:
            source_id: 来源 ID.
            item_id: 条目 ID（可选，按名称撤回时不需要）.
            operator_id: 操作者 ID.
            operator_name: 操作者名称.
            req: 撤回请求体.

        Returns:
            撤回结果.
        """
        if req.mcp_name:
            mcp_name = req.mcp_name  # narrowed from str | None
            if not req.target_user_ids:
                return self._build_recall_response("", 0, [])

            user_info_map = await self._query_user_info_map(
                source_id,
                req.target_user_ids,
                "mcp",
            )
            return await self._execute_recall_for_users(
                source_id=source_id,
                item_id="",
                item_name=mcp_name,
                item_type="mcp",
                operator_id=operator_id,
                operator_name=operator_name,
                user_ids=req.target_user_ids,
                dist_map={},
                user_info_map=user_info_map,
                recall_one=lambda user_id: self._recall_mcp_from_user(
                    user_id,
                    source_id,
                    mcp_name=mcp_name,
                    reload_source_id=source_id,
                ),
                warning_message="Failed to recall MCP from user %s: %s",
            )

        if not item_id:
            raise ValueError("item_id or mcp_name is required")

        item = self._require_market_item(source_id, item_id, "mcp", "MCP")
        distributions = await self.get_distributions(source_id, item_id, "mcp")
        dist_map = {d.target_user_id: d for d in distributions}
        users_to_recall = self._resolve_users_to_recall(
            req.target_user_ids,
            dist_map,
        )

        if not users_to_recall:
            return self._build_recall_response(item_id, 0, [])

        user_info_map = await self._query_user_info_map(
            source_id,
            users_to_recall,
            "mcp",
        )
        expected_source_prefix = None
        if not req.force:
            expected_source_prefix = f"marketplace:{item_id}"

        return await self._execute_recall_for_users(
            source_id=source_id,
            item_id=item_id,
            item_name=item.name,
            item_type="mcp",
            operator_id=operator_id,
            operator_name=operator_name,
            user_ids=users_to_recall,
            dist_map=dist_map,
            user_info_map=user_info_map,
            recall_one=lambda user_id: self._recall_mcp_from_user(
                user_id,
                source_id,
                mcp_name=item.name,
                expected_source_prefix=expected_source_prefix,
                reload_source_id=None,
            ),
            warning_message="Failed to recall MCP from user %s: %s",
        )
