# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=line-too-long
"""The shell command tool with tenant path boundary enforcement."""

import asyncio
import ast
from contextlib import (
    AbstractContextManager,
    ExitStack,
    asynccontextmanager,
    contextmanager,
    nullcontext,
)
import locale
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Iterator, Optional

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from ..tool_failure import ToolExecutionError
from ...app.runner.tool_output_frames import (
    ToolOutputSource,
    emit_tool_output_text,
)
from ...envs.runtime import build_runtime_env
from ...output_workers import run_output_work
from ...runtime_workers import run_runtime_state_work
from ...runtime_invocation_claims import apply_runtime_claim_env
from ...security.tenant_path_boundary import (
    is_path_within_tenant_with_base,
    get_current_tenant_root,
    get_current_tool_base_dir,
    TenantPathBoundaryError,
)
from ...security.python_runtime_path_guard import (
    is_safe_active_skill_name,
    prepare_python_runtime_path_guard_env,
)
from ...security.process_limits import (
    CurrentProcessLimitPolicy,
    resolve_current_process_limit_policy,
)
from .file_io import (
    is_created_workspace_skill_write_target,
)
from ..skill_context_manager import get_skill_context_manager
from .shell_output_spool import (
    ShellOutputStorageError,
    create_shell_output_spool,
    get_active_shell_output_spool,
    run_shell_spool_work,
    await_shell_cleanup_task,
)

_SHELL_PRESERVED_BOUNDARY_ENV_KEYS = frozenset(
    {
        "SWE_WORKING_DIR",
        "SWE_SECRET_DIR",
    },
)
_SHELL_SPOOL_BATCH_BYTES = 64 * 1024

# Commands that take string arguments which may look like paths
# but should NOT be treated as file paths
_STRING_ARG_COMMANDS = frozenset(
    {
        "echo",
        "/bin/echo",
        "/usr/bin/echo",
        "printf",
        "/usr/bin/printf",
    },
)

# Interpreter commands that have dangerous -c/-e code execution flags
# Only for these commands do we reject -c/-e flags
_INTERPRETER_COMMANDS = frozenset(
    {
        # Python code execution temporarily allowed
        # "python", "python3", "python2",
        # "/usr/bin/python", "/usr/bin/python3", "/usr/bin/python2",
        # "/usr/local/bin/python", "/usr/local/bin/python3",
        "node",
        "/usr/bin/node",
        "/usr/local/bin/node",
        "nodejs",
        "/usr/bin/nodejs",
        "ruby",
        "/usr/bin/ruby",
        "/usr/local/bin/ruby",
        "perl",
        "/usr/bin/perl",
        "/usr/local/bin/perl",
        "bash",
        "/bin/bash",
        "/usr/bin/bash",
        "sh",
        "/bin/sh",
        "/usr/bin/sh",
        "zsh",
        "/bin/zsh",
        "/usr/bin/zsh",
        "ksh",
        "/bin/ksh",
        "/usr/bin/ksh",
        "dash",
        "/bin/dash",
        "/usr/bin/dash",
    },
)

_PYTHON_COMMAND_BASENAMES = frozenset(
    {
        "python",
        "python2",
        "python3",
        "pypy",
        "pypy3",
    },
)

_PYTHON_OPTIONS_WITH_VALUE = frozenset({"-W", "-X"})

_SUBPROCESS_THREAD_LIMIT_DEFAULTS = {
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
}

_PYTHON_PATH_CALL_ARG_INDICES = {
    "open": (0,),
    "io.open": (0,),
    "os.open": (0,),
    "os.stat": (0,),
    "os.listdir": (0,),
    "os.remove": (0,),
    "os.unlink": (0,),
    "os.rmdir": (0,),
    "os.mkdir": (0,),
    "os.makedirs": (0,),
    "os.scandir": (0,),
    "os.rename": (0, 1),
    "os.replace": (0, 1),
    "shutil.copy": (0, 1),
    "shutil.copy2": (0, 1),
    "shutil.copyfile": (0, 1),
    "shutil.copytree": (0, 1),
    "shutil.move": (0, 1),
    "Path": (0,),
    "pathlib.Path": (0,),
    "PurePath": (0,),
    "pathlib.PurePath": (0,),
}

_PYTHON_SCAN_MAX_FILES = 128
_PYTHON_SCAN_MAX_BYTES = 512 * 1024
_DISALLOWED_SHELL_ENV_PATH_VARS = frozenset(
    {"HOME", "PWD", "OLDPWD", "TMPDIR", "TEMP", "TMP"},
)
_DISALLOWED_SHELL_ENV_PATH_PATTERN = re.compile(
    r"(?<!\\)\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|"
    r"(?P<bare>[A-Za-z_][A-Za-z0-9_]*)\b)",
)
_RAW_WINDOWS_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_:])(?:[A-Za-z]:[\\/][^\s\"'`|;&<>]*|"
    r"\\\\[^\s\"'`|;&<>]+)",
)
_DISALLOWED_SYSTEM_PATH_PREFIXES = (
    "/opt/",
    "/etc/",
    "/root/",
    "/proc/",
    "/sys/",
    "/dev/",
)
_ALLOWED_SYSTEM_PATH_TOKENS = frozenset({"/dev/null"})

_SHELL_SLOT_CONDITION = asyncio.Condition()
_SHELL_SLOT_COUNTS: dict[str, int] = {}

_SHELL_WRITE_DESTINATION_COMMANDS = frozenset(
    {
        "cp",
        "install",
        "mkdir",
        "mv",
        "touch",
        "unzip",
    },
)
_SHELL_REDIRECT_OPERATORS = frozenset(
    {">", ">>", "1>", "1>>", "2>", "2>>", "&>", "&>>"},
)


def _is_path_like(token: str) -> bool:
    """Check if a token looks like a file path.

    Args:
        token: The token to check.

    Returns:
        True if the token looks like a path.
    """
    return token.startswith(("/", "\\", "./", "../", "~")) or bool(
        re.match(
            r"^[A-Za-z]:[\\/]",
            token,
        ),
    )


def _find_disallowed_shell_env_path_reference(command: str) -> Optional[str]:
    """Return a disallowed shell path variable reference if one is present."""
    for match in _DISALLOWED_SHELL_ENV_PATH_PATTERN.finditer(command):
        var_name = match.group("braced") or match.group("bare") or ""
        if var_name in _DISALLOWED_SHELL_ENV_PATH_VARS:
            return match.group(0)
    return None


def _is_allowed_system_path_token(token: str) -> bool:
    """Return True for narrow system path exceptions used as IO sinks."""
    return token in _ALLOWED_SYSTEM_PATH_TOKENS


def _extract_raw_windows_path_tokens(command: str) -> list[str]:
    """Extract Windows absolute paths before POSIX shlex can drop backslashes."""
    return [
        match.group(0) for match in _RAW_WINDOWS_PATH_PATTERN.finditer(command)
    ]


def _has_code_exec_flag(token: str) -> bool:
    """Check if a token contains code execution flags (-c or -e).

    Handles both standalone flags (-c, -e) and combined flags (-lc, -ec, -ce).
    Only checks the flag part, not whether the command is an interpreter.

    Args:
        token: The token to check (e.g., "-c", "-lc", "--eval").

    Returns:
        True if the token contains -c or -e as code execution flags.
    """
    # Long-form flags that are always code execution
    if token in ("--eval", "--exec", "--command"):
        return True

    # Short-form flags: -c, -e, or combined like -lc, -ec, -ce
    if token.startswith("-") and len(token) > 1:
        # Check if 'c' or 'e' appears in the combined flag
        # But exclude special cases like --option (already handled above)
        flag_body = token[1:]  # Remove leading -
        if "c" in flag_body or "e" in flag_body:
            return True

    return False


def _extract_path_tokens(command: str) -> tuple[list[str], bool]:
    """Extract path tokens from shell command.

    Implements a "path-first" validation strategy:
    - Any token that looks like a path (/..., ./..., ../..., ~...) is validated
    - Only exempt: echo/printf commands (their non-flag args are treated as strings)
    - Interpreter commands with -c/-e flags are flagged for rejection

    Args:
        command: The shell command string.

    Returns:
        Tuple of (file_paths, has_code_exec) where:
        - file_paths: List of explicit file path tokens found
        - has_code_exec: True if interpreter command contains code execution flags
    """
    file_paths = []
    has_code_exec = False

    # Split command into tokens for better parsing
    try:
        tokens = shlex.split(command)
    except ValueError:
        # If shlex fails, fall back to simple parsing
        tokens = command.split()

    if not tokens:
        return file_paths, has_code_exec

    # Check command type
    cmd_name = tokens[0]
    is_exempt_cmd = cmd_name in _STRING_ARG_COMMANDS
    is_interpreter = cmd_name in _INTERPRETER_COMMANDS

    if not is_exempt_cmd:
        file_paths.extend(_extract_raw_windows_path_tokens(command))

    i = 0
    while i < len(tokens):
        token = tokens[i]

        # Check for code execution flags - but ONLY for interpreter commands
        if is_interpreter and _has_code_exec_flag(token):
            has_code_exec = True
            i += 1
            continue

        # Check for path-like tokens
        if _is_path_like(token):
            # For exempt commands (echo/printf), only treat as path if not
            # preceded by a flag (to handle: echo -n "/etc/hosts")
            if is_exempt_cmd:
                if i > 0:
                    prev = tokens[i - 1]
                    if prev.startswith("-"):
                        # This is likely a flag argument, skip
                        pass
                    else:
                        file_paths.append(token)
                else:
                    # First token after command name - for echo/printf this is text
                    pass
            else:
                # Non-exempt command: any path-like token is a file path
                if token not in file_paths:
                    file_paths.append(token)

        i += 1

    return file_paths, has_code_exec


def _is_python_command(command_name: str) -> bool:
    """Return True when the command token names a Python interpreter."""
    basename = Path(command_name).name
    return (
        basename in _PYTHON_COMMAND_BASENAMES
        or basename.startswith("python3.")
        or basename.startswith("python2.")
    )


def _call_name(node: ast.AST) -> Optional[str]:
    """Return dotted call name for simple name/attribute calls."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        if parent:
            return f"{parent}.{node.attr}"
        return node.attr
    return None


def _collect_string_constants(tree: ast.AST) -> dict[str, str]:
    """Collect simple string assignments for static path checks."""
    constants: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Constant) or not isinstance(
            node.value.value,
            str,
        ):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                constants[target.id] = node.value.value
    return constants


def _static_string_value(
    node: ast.AST,
    constants: dict[str, str],
) -> Optional[str]:
    """Resolve literal strings and simple string constants."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def _find_disallowed_system_path_literal(
    tree: ast.AST,
    base_dir: Path,
) -> Optional[str]:
    """查找源码中静态出现的系统路径字面量。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(
            node.value,
            str,
        ):
            continue
        if node.value.startswith(("http://", "https://")):
            continue
        if _is_allowed_system_path_token(node.value):
            continue
        for prefix in _DISALLOWED_SYSTEM_PATH_PREFIXES:
            if prefix in node.value:
                if is_path_within_tenant_with_base(
                    node.value,
                    base_dir=base_dir,
                ):
                    continue
                return node.value

    return None


def _find_outside_python_call_path(
    tree: ast.AST,
    constants: dict[str, str],
    base_dir: Path,
) -> Optional[str]:
    """查找 Python 文件访问调用中越出租户边界的静态路径。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        call_name = _call_name(node.func)
        if call_name not in _PYTHON_PATH_CALL_ARG_INDICES:
            continue

        for arg_index in _PYTHON_PATH_CALL_ARG_INDICES[call_name]:
            if arg_index >= len(node.args):
                continue
            path_value = _static_string_value(node.args[arg_index], constants)
            if path_value is None:
                continue
            if not is_path_within_tenant_with_base(
                path_value,
                base_dir=base_dir,
            ):
                return path_value

    return None


def _scan_python_source_for_outside_path(
    source: str,
    base_dir: Path,
) -> Optional[str]:
    """Find static Python file-access paths that escape tenant boundary."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    system_path = _find_disallowed_system_path_literal(tree, base_dir)
    if system_path:
        return system_path

    constants = _collect_string_constants(tree)
    outside_path = _find_outside_python_call_path(tree, constants, base_dir)
    if outside_path:
        return outside_path

    return None


def _resolve_command_path(token: str, base_dir: Path) -> Path:
    """Resolve a command path token the same way the shell runtime cwd would."""
    path_obj = Path(os.path.expanduser(token))
    if not path_obj.is_absolute():
        path_obj = base_dir / path_obj
    return path_obj.resolve()


def _iter_python_source_files(script_path: Path) -> list[Path]:
    """Return Python source files to scan for a script or package directory."""
    if script_path.is_file():
        return [script_path]
    if not script_path.is_dir():
        return []

    files: list[Path] = []
    for path in script_path.rglob("*.py"):
        files.append(path)
        if len(files) >= _PYTHON_SCAN_MAX_FILES:
            break
    return files


def _find_python_code_source(
    tokens: list[str],
    base_dir: Path,
) -> tuple[str, Optional[str | Path]]:
    """Extract Python source mode from command tokens.

    Returns ("code", source) for ``python -c``, ("path", path) for script or
    directory execution, ("path_outside", token) for a script path that resolves
    outside the tenant, and ("none", None) when there is nothing static to scan.
    """
    i = 1
    while i < len(tokens):
        token = tokens[i]

        if token == "-c":
            if i + 1 < len(tokens):
                return "code", tokens[i + 1]
            return "none", None
        if token.startswith("-c") and len(token) > 2:
            return "code", token[2:]
        if token == "-m" or token.startswith("-m"):
            return "none", None
        if token == "--":
            i += 1
            break
        if token in _PYTHON_OPTIONS_WITH_VALUE:
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        break

    if i >= len(tokens):
        return "none", None

    script_token = tokens[i]
    if script_token == "-":
        return "none", None
    if not is_path_within_tenant_with_base(script_token, base_dir=base_dir):
        return "path_outside", script_token
    return "path", _resolve_command_path(script_token, base_dir)


def _validate_python_script_contents(
    command: str,
    base_dir: Path,
) -> Optional[str]:
    """Validate static Python script/code paths before running Python."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    if not tokens or not _is_python_command(tokens[0]):
        return None

    source_type, source = _find_python_code_source(tokens, base_dir)
    if source_type == "code" and isinstance(source, str):
        outside_path = _scan_python_source_for_outside_path(source, base_dir)
        if outside_path:
            return (
                "Error: Python code contains path outside the allowed "
                "workspace or system path string: "
                f"'{outside_path}'"
            )
        return None

    if source_type == "path_outside":
        return (
            "Error: Python script path outside the allowed workspace: "
            f"'{source}'"
        )

    if source_type != "path" or not isinstance(source, Path):
        return None

    for script_path in _iter_python_source_files(source):
        if not is_path_within_tenant_with_base(script_path, base_dir=base_dir):
            return (
                "Error: Python script path outside the allowed workspace: "
                f"'{script_path}'"
            )

        try:
            if script_path.stat().st_size > _PYTHON_SCAN_MAX_BYTES:
                continue
            script_source = script_path.read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            continue

        outside_path = _scan_python_source_for_outside_path(
            script_source,
            base_dir,
        )
        if outside_path:
            return (
                "Error: Python script contains path outside the allowed "
                "workspace or system path string: "
                f"'{outside_path}'"
            )

    return None


def _resolve_shell_path_token(token: str, base_dir: Path) -> Path:
    path_obj = Path(os.path.expanduser(token))
    if not path_obj.is_absolute():
        path_obj = base_dir / path_obj
    return path_obj.resolve(strict=False)


def _active_workspace_skill_write_roots(base_dir: Path) -> tuple[Path, ...]:
    current_skill = get_skill_context_manager().current_skill
    if not current_skill:
        return ()

    workspace_dir = get_current_tool_base_dir().resolve(strict=False)
    try:
        workspace_dir.relative_to(get_current_tenant_root().resolve())
    except ValueError:
        workspace_dir = base_dir.resolve(strict=False)

    if not is_safe_active_skill_name(current_skill):
        return (
            workspace_dir / "skills",
            workspace_dir / ".disabled_skills",
        )

    return (
        workspace_dir / "skills" / current_skill,
        workspace_dir / ".disabled_skills" / current_skill,
    )


def _append_shell_redirect_targets(
    tokens: list[str],
    destinations: list[str],
) -> None:
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in _SHELL_REDIRECT_OPERATORS:
            if i + 1 < len(tokens):
                destinations.append(tokens[i + 1])
            i += 2
            continue
        for op in sorted(_SHELL_REDIRECT_OPERATORS, key=len, reverse=True):
            if token.startswith(op) and len(token) > len(op):
                destinations.append(token[len(op) :])
                break
        i += 1


def _append_unzip_destinations(
    tokens: list[str],
    destinations: list[str],
) -> None:
    i = 1
    while i < len(tokens):
        token = tokens[i]
        if token == "-d" and i + 1 < len(tokens):
            destinations.append(tokens[i + 1])
            i += 2
            continue
        if token.startswith("-d") and len(token) > 2:
            destinations.append(token[2:])
        i += 1


def _append_copy_move_destinations(
    tokens: list[str],
    destinations: list[str],
) -> None:
    operands = [
        token for token in tokens[1:] if token and not token.startswith("-")
    ]
    if len(operands) >= 2:
        destinations.append(operands[-1])


def _append_multi_target_destinations(
    tokens: list[str],
    destinations: list[str],
) -> None:
    destinations.extend(
        token for token in tokens[1:] if token and not token.startswith("-")
    )


def _append_paths_after_first_operand(
    tokens: list[str],
    destinations: list[str],
) -> None:
    seen_first_operand = False
    for token in tokens[1:]:
        if not token or token.startswith("-"):
            continue
        if not seen_first_operand:
            seen_first_operand = True
            continue
        destinations.append(token)


def _append_remove_destinations(
    tokens: list[str],
    destinations: list[str],
) -> None:
    _append_multi_target_destinations(tokens, destinations)


def _extract_shell_write_destinations(command: str) -> list[str]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return []

    destinations: list[str] = []
    command_name = Path(tokens[0]).name
    if command_name == "unzip":
        _append_unzip_destinations(tokens, destinations)
    elif command_name in {"cp", "mv", "install"}:
        if command_name == "mv":
            _append_multi_target_destinations(tokens, destinations)
        else:
            _append_copy_move_destinations(tokens, destinations)
    elif command_name in {"chmod", "chown", "lchown"}:
        _append_paths_after_first_operand(tokens, destinations)
    elif command_name in {"mkdir", "touch"}:
        _append_multi_target_destinations(tokens, destinations)
    elif command_name in {"rm", "rmdir"}:
        _append_remove_destinations(tokens, destinations)
    elif command_name in _SHELL_WRITE_DESTINATION_COMMANDS:
        _append_copy_move_destinations(tokens, destinations)
    _append_shell_redirect_targets(tokens, destinations)
    return destinations


def _validate_workspace_skill_write_targets(
    command: str,
    base_dir: Path,
) -> Optional[str]:
    workspace_dir = get_current_tool_base_dir().resolve(strict=False)
    try:
        workspace_dir.relative_to(get_current_tenant_root().resolve())
    except ValueError:
        workspace_dir = base_dir.resolve(strict=False)

    current_skill = get_skill_context_manager().current_skill
    active_skill_roots = _active_workspace_skill_write_roots(base_dir)
    for destination in _extract_shell_write_destinations(command):
        if not destination or destination in {".", ".."}:
            continue
        resolved = _resolve_shell_path_token(destination, base_dir)
        if any(
            resolved == root or resolved.is_relative_to(root)
            for root in active_skill_roots
        ):
            if not is_safe_active_skill_name(current_skill):
                return (
                    "Error: Shell command writes directly into the workspace "
                    "skill directory. Use the skill import or edit APIs so "
                    f"security scanning runs: '{destination}'"
                )
            if is_created_workspace_skill_write_target(
                resolved,
                workspace_dir,
                current_skill=current_skill,
            ):
                continue
            return (
                "Error: Shell command writes directly into the workspace "
                "skill directory. Use the skill import or edit APIs so "
                f"security scanning runs: '{destination}'"
            )
        if not any(
            resolved == workspace_dir / root_name
            or resolved.is_relative_to(workspace_dir / root_name)
            for root_name in ("skills", ".disabled_skills")
        ):
            continue
        if is_created_workspace_skill_write_target(
            resolved,
            workspace_dir,
            current_skill=current_skill,
        ):
            continue
        return (
            "Error: Shell command writes directly into the workspace "
            "skill directory. Use the skill import or edit APIs so "
            f"security scanning runs: '{destination}'"
        )
    return None


def _validate_shell_paths(command: str, base_dir: Path) -> Optional[str]:
    """Validate that all explicit file paths in the command are within tenant boundary.

    Args:
        command: The shell command to validate.
        base_dir: The base directory for resolving relative paths (typically the cwd).

    Returns:
        Error message if any path escapes the tenant boundary, None otherwise.
    """
    env_path_ref = _find_disallowed_shell_env_path_reference(command)
    if env_path_ref:
        return (
            "Error: Shell command references disallowed environment path "
            f"variable: '{env_path_ref}'. Use an explicit workspace-relative "
            "path instead."
        )

    file_paths, has_code_exec = _extract_path_tokens(command)

    # Reject commands with code execution flags (-c, -e, etc.)
    if has_code_exec:
        return (
            "Error: Shell commands with code execution flags (-c, -e, etc.) "
            "are not allowed for security reasons."
        )

    for token in file_paths:
        # Skip checking if it's clearly not a path
        if not token or token in (".", ".."):
            continue
        if _is_allowed_system_path_token(token):
            continue

        # Check if the path is within tenant boundary, using base_dir for relative paths
        if not is_path_within_tenant_with_base(token, base_dir=base_dir):
            return (
                f"Error: Shell command contains path outside the allowed workspace: "
                f"'{token}'"
            )

    python_error = _validate_python_script_contents(command, base_dir)
    if python_error:
        return python_error

    skill_write_error = _validate_workspace_skill_write_targets(
        command,
        base_dir,
    )
    if skill_write_error:
        return skill_write_error

    return None


def _resolve_cwd(cwd: Optional[Path | str]) -> Path:
    """Resolve and validate the working directory against tenant boundary.

    Args:
        cwd: The requested working directory, or None to default to the current
             agent workspace when available, otherwise the tenant workspace root.

    Returns:
        The resolved working directory path.

    Raises:
        TenantPathBoundaryError: If the cwd is outside the tenant workspace
                                 or tenant context is missing.
    """
    tenant_root = get_current_tenant_root()

    if cwd is None:
        return get_current_tool_base_dir()

    # Resolve the cwd and validate it's within tenant root
    resolved_cwd = Path(cwd).expanduser().resolve()
    try:
        resolved_cwd.relative_to(tenant_root.resolve())
    except ValueError as exc:
        raise TenantPathBoundaryError(
            f"Working directory '{cwd}' is outside the tenant workspace boundary.",
            resolved_path=resolved_cwd,
        ) from exc

    return resolved_cwd


def _kill_process_tree_win32(pid: int) -> None:
    """Kill a process and all its descendants on Windows via taskkill.

    Uses ``taskkill /F /T`` which forcefully terminates the entire process
    tree, including grandchild processes that ``Popen.kill()`` would miss.
    """
    try:
        subprocess.call(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except Exception:
        pass


def _collapse_embedded_newlines(cmd: str) -> str:
    r"""Replace embedded newline characters with spaces in a command string.

    LLMs produce tool-call arguments in JSON where ``\n`` is parsed as an
    actual newline character.  In the original shell command the user
    intended the *literal* two-character sequence ``\n`` (e.g. inside a
    ``--content`` flag), but after JSON decoding it becomes a real line
    break.  When passed to a shell:

    * **Windows** ``cmd.exe`` truncates the command at the first newline.
    * **Unix** ``sh -c`` treats an unquoted newline as a command separator,
      so only the first "line" is executed with its arguments.

    Collapsing these newlines to spaces is a safe default because:

    1. For the bug case (JSON artefact) it prevents truncation.
    2. For intentional multi-line scripts on Windows the ``cmd /D /S /C``
       wrapper *already* breaks at newlines, so this is no worse.
    3. On Unix, callers should prefer ``&&`` / ``;`` over raw newlines for
       multi-command sequences; a stray newline inside an argument is
      almost certainly a JSON artefact.
    """
    if "\n" not in cmd:
        return cmd
    return cmd.replace("\r\n", " ").replace("\n", " ")


def _sanitize_win_cmd(cmd: str) -> str:
    """Fix common LLM escaping artefacts for Windows ``cmd.exe``.

    LLMs sometimes produce commands with backslash-escaped double quotes
    (``\\"``) — valid in bash/JSON but meaningless to ``cmd.exe``.  When
    *every* double-quote in the command is preceded by a backslash, it is
    almost certainly a double-escape artefact, so we strip them.
    """
    if '\\"' in cmd and '"' not in cmd.replace('\\"', ""):
        return cmd.replace('\\"', '"')
    return cmd


def _read_temp_file(path: str) -> str:
    """Read a temporary output file and return its decoded content."""
    try:
        with open(path, "rb") as f:
            return smart_decode(f.read())
    except OSError:
        return ""


# pylint: disable=too-many-branches, too-many-statements
def _execute_subprocess_sync(
    cmd: str,
    cwd: str,
    timeout: int,
    env: dict | None = None,
) -> tuple[int, str, str]:
    """Execute subprocess synchronously in a thread.

    This function runs in a separate thread to avoid Windows asyncio
    subprocess limitations.

    stdout/stderr are redirected to temporary files instead of pipes.
    On Windows, child processes inherit pipe handles and keep them open
    even after the parent exits, which causes ``communicate()`` to block
    until *all* holders close (e.g. a Chrome process launched via
    ``Start-Process``).  With temp-file redirection, ``proc.wait()``
    only waits for the direct child (``cmd.exe``) to exit, so commands
    that spawn background processes return immediately.

    .. note::

       Callers must pre-process *cmd* through
       :func:`_collapse_embedded_newlines` before passing it here.
       ``execute_shell_command`` already does this.

    Args:
        cmd (`str`):
            The shell command to execute (must not contain embedded
            newlines — see note above).
        cwd (`str`):
            The working directory for the command execution.
        timeout (`int`):
            The maximum time (in seconds) allowed for the command to run.
        env (`dict | None`):
            Environment variables for the subprocess.

    Returns:
        `tuple[int, str, str]`:
            A tuple containing the return code, standard output, and
            standard error of the executed command. If timeout occurs, the
            return code will be -1 and stderr will contain timeout information.
    """
    stdout_path: str | None = None
    stderr_path: str | None = None
    stdout_file = None
    stderr_file = None

    try:
        cmd = _sanitize_win_cmd(cmd)
        wrapped = f'cmd /D /S /C "{cmd}"'

        stdout_fd, stdout_path = tempfile.mkstemp(prefix="swe_out_")
        stderr_fd, stderr_path = tempfile.mkstemp(prefix="swe_err_")
        stdout_file = os.fdopen(stdout_fd, "wb")
        stderr_file = os.fdopen(stderr_fd, "wb")

        proc = subprocess.Popen(  # pylint: disable=consider-using-with
            wrapped,
            shell=False,
            stdout=stdout_file,
            stderr=stderr_file,
            text=False,
            cwd=cwd,
            env=env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )

        # Parent copies are no longer needed — the child inherited its own
        # handles via CreateProcess.  Closing here avoids holding the files
        # open longer than necessary.
        stdout_file.close()
        stdout_file = None
        stderr_file.close()
        stderr_file = None

        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_tree_win32(proc.pid)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass

        stdout_str = _read_temp_file(stdout_path)
        stderr_str = _read_temp_file(stderr_path)

        if timed_out:
            timeout_msg = (
                f"Command execution exceeded the timeout of {timeout} seconds."
            )
            if stderr_str:
                stderr_str = f"{stderr_str}\n{timeout_msg}"
            else:
                stderr_str = timeout_msg
            return -1, stdout_str, stderr_str

        returncode = proc.returncode if proc.returncode is not None else -1
        return returncode, stdout_str, stderr_str

    except Exception as e:
        return -1, "", str(e)
    finally:
        for f in (stdout_file, stderr_file):
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass
        for path in (stdout_path, stderr_path):
            if path is not None:
                try:
                    os.unlink(path)
                except OSError:
                    pass


def _tool_text_response(text: str) -> ToolResponse:
    """Build a plain-text tool response."""
    return ToolResponse(
        content=[
            TextBlock(
                type="text",
                text=text,
            ),
        ],
    )


def _raise_shell_error(error_type: str, detail: str) -> None:
    raise ToolExecutionError(error_type=error_type, detail=detail)


def _is_shell_timeout_failure(returncode: int, stderr_lower: str) -> bool:
    timeout_markers = (
        "timeouterror",
        "connecttimeout",
        "read timed out",
        "connection timed out",
    )
    return returncode in {-1, 28} or any(
        marker in stderr_lower for marker in timeout_markers
    )


def _is_process_limit_signal(
    returncode: int,
    process_limits_enforced: bool,
) -> bool:
    if not process_limits_enforced or returncode >= 0:
        return False
    process_limit_signals = {
        getattr(signal, signal_name)
        for signal_name in ("SIGKILL", "SIGXCPU")
        if hasattr(signal, signal_name)
    }
    return abs(returncode) in process_limit_signals


def _is_memory_limit_failure(
    stderr_str: str,
    memory_limit_enforced: bool,
) -> bool:
    memory_limit_markers = (
        "MemoryError",
        "Cannot allocate memory",
        "Killed",
    )
    return memory_limit_enforced and any(
        marker in stderr_str for marker in memory_limit_markers
    )


def _classify_shell_failure(
    returncode: int,
    stderr_str: str,
    *,
    process_limits_enforced: bool = False,
    memory_limit_enforced: bool = False,
) -> str:
    stderr_lower = stderr_str.lower()
    if _is_shell_timeout_failure(returncode, stderr_lower):
        return "tool_timeout"
    if "outside the allowed workspace" in stderr_str:
        return "permission_denied"
    if _is_process_limit_signal(returncode, process_limits_enforced):
        return "process_limit_exceeded"
    if _is_memory_limit_failure(stderr_str, memory_limit_enforced):
        return "process_limit_exceeded"
    return "shell_command_failed"


def _format_process_limit_diagnostic(
    response_text: str,
    policy: CurrentProcessLimitPolicy,
) -> str:
    if not policy.diagnostic or policy.should_enforce:
        return response_text
    return (
        f"{response_text}\n" f"[process-limit diagnostic]\n{policy.diagnostic}"
    )


def _shell_slot_key(policy: CurrentProcessLimitPolicy) -> str:
    return policy.tenant_id or "default"


@asynccontextmanager
async def _tenant_shell_execution_slot(
    policy: CurrentProcessLimitPolicy,
) -> AsyncIterator[None]:
    """Hold one process-local tenant shell execution slot when configured."""
    max_concurrent = policy.shell_max_concurrent
    if not policy.enabled or policy.scope != "shell" or max_concurrent is None:
        yield
        return

    key = _shell_slot_key(policy)
    timeout = policy.shell_acquire_timeout_seconds
    acquired = False
    try:
        async with _SHELL_SLOT_CONDITION:
            try:
                await asyncio.wait_for(
                    _SHELL_SLOT_CONDITION.wait_for(
                        lambda: _SHELL_SLOT_COUNTS.get(key, 0)
                        < max_concurrent,
                    ),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                _raise_shell_error(
                    "shell_concurrency_limit_exceeded",
                    (
                        f"Tenant {key} has no available shell execution "
                        f"slots (max {max_concurrent}) within {timeout} "
                        "seconds."
                    ),
                )
            _SHELL_SLOT_COUNTS[key] = _SHELL_SLOT_COUNTS.get(key, 0) + 1
            acquired = True
        yield
    finally:
        if acquired:
            async with _SHELL_SLOT_CONDITION:
                current = _SHELL_SLOT_COUNTS.get(key, 0)
                if current <= 1:
                    _SHELL_SLOT_COUNTS.pop(key, None)
                else:
                    _SHELL_SLOT_COUNTS[key] = current - 1
                _SHELL_SLOT_CONDITION.notify_all()


def _prepare_subprocess_env() -> dict[str, str]:
    """Prepare subprocess environment with tenant env and active Python PATH."""
    env = build_runtime_env(
        preserve_boundary_env_keys=_SHELL_PRESERVED_BOUNDARY_ENV_KEYS,
    )
    env = apply_runtime_claim_env(env)
    python_bin_dir = str(Path(sys.executable).parent)
    existing_path = env.get("PATH", "")
    env["PATH"] = (
        python_bin_dir + os.pathsep + existing_path
        if existing_path
        else python_bin_dir
    )
    for key, value in _SUBPROCESS_THREAD_LIMIT_DEFAULTS.items():
        env.setdefault(key, value)
    return env


@dataclass(frozen=True)
class PreparedShellCommand:
    """Shell 工具共享的已校验启动参数。"""

    command: str
    working_dir: Path
    env: dict[str, str]
    python_runtime_guard: AbstractContextManager[None]


@contextmanager
def _python_runtime_guard_context(
    guard: AbstractContextManager[str],
) -> Iterator[None]:
    with guard:
        yield


def prepare_shell_command(
    command: str,
    cwd: Optional[Path | str] = None,
) -> PreparedShellCommand:
    """归一化并校验 Shell 命令，生成可执行启动参数。"""
    raw_cmd = (command or "").strip()
    cmd = (
        _collapse_embedded_newlines(raw_cmd)
        if sys.platform == "win32"
        else raw_cmd
    )

    from ...security.shell_installation_policy import shell_installation_denial

    policy_denial = shell_installation_denial(cmd)
    if policy_denial:
        _raise_shell_error("shell_policy_denied", policy_denial)

    try:
        working_dir = _resolve_cwd(cwd)
    except TenantPathBoundaryError as e:
        _raise_shell_error("permission_denied", f"Error: {e}")

    path_error = _validate_shell_paths(cmd, base_dir=working_dir)
    if path_error:
        error_type = (
            "permission_denied"
            if "outside the allowed workspace" in path_error
            else "invalid_arguments"
        )
        _raise_shell_error(error_type, path_error)

    env = _prepare_subprocess_env()
    python_runtime_guard = prepare_python_runtime_path_guard_env(
        env,
        tenant_root=get_current_tenant_root(),
        base_dir=working_dir,
        active_skill_base_dir=get_current_tool_base_dir(),
    )

    return PreparedShellCommand(
        command=cmd,
        working_dir=working_dir,
        env=env,
        python_runtime_guard=_python_runtime_guard_context(
            python_runtime_guard,
        ),
    )


@asynccontextmanager
async def _prepared_shell_execution(
    command: str,
    cwd: Path | str | None,
) -> AsyncIterator[tuple[PreparedShellCommand, CurrentProcessLimitPolicy]]:
    """Own preparation and guard files through cancellation and execution."""
    from .shell_interceptor import intercept_command

    command, _was_intercepted = await intercept_command(command)
    resources = ExitStack()

    def prepare() -> tuple[PreparedShellCommand, CurrentProcessLimitPolicy]:
        prepared = prepare_shell_command(command, cwd)
        resources.enter_context(prepared.python_runtime_guard)
        policy = resolve_current_process_limit_policy("shell")
        return prepared, policy

    preparation = asyncio.create_task(run_runtime_state_work(prepare))
    try:
        yield await asyncio.shield(preparation)
    finally:

        async def cleanup() -> None:
            # A cancelled caller still owns guard files created by its worker.
            await asyncio.gather(preparation, return_exceptions=True)
            await run_runtime_state_work(resources.close)

        await await_shell_cleanup_task(asyncio.create_task(cleanup()))


def _format_shell_response(
    returncode: int,
    stdout_str: str,
    stderr_str: str,
) -> str:
    """Format shell execution output for a tool response."""
    if returncode == 0:
        response_text = (
            stdout_str or "Command executed successfully (no output)."
        )
        if stderr_str:
            response_text += f"\n[stderr]\n{stderr_str}"
        return response_text

    response_parts = [f"Command failed with exit code {returncode}."]
    if stdout_str:
        response_parts.append(f"\n[stdout]\n{stdout_str}")
    if stderr_str:
        response_parts.append(f"\n[stderr]\n{stderr_str}")
    return "".join(response_parts)


async def _terminate_unix_process_group(
    proc: asyncio.subprocess.Process,
    pgid: int | None = None,
) -> None:
    """Terminate a Unix subprocess group, escalating to SIGKILL if needed."""
    if pgid is None:
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    if not await _wait_for_unix_process_group_exit(pgid, proc, timeout=2):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return
        await _wait_for_unix_process_group_exit(pgid, proc, timeout=2)


def _unix_process_group_exists(pgid: int) -> bool:
    """Return whether any process is still present in the process group."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    return True


async def _wait_for_unix_process_group_exit(
    pgid: int,
    proc: asyncio.subprocess.Process,
    timeout: float,
) -> bool:
    """Wait for the spawned process group to disappear."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while _unix_process_group_exists(pgid):
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        delay = min(0.05, remaining)
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(delay)
    return True


async def _wait_for_unix_process_exit(
    proc: asyncio.subprocess.Process,
    timeout: float,
) -> None:
    """Wait until the shell process exits without waiting for pipe EOF."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while proc.returncode is None:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        await asyncio.sleep(min(0.05, remaining))


async def _drain_unix_subprocess_output(
    proc: asyncio.subprocess.Process,
) -> tuple[bytes, bytes]:
    """Drain any remaining subprocess output after termination."""
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=1)
    except asyncio.TimeoutError:
        return b"", b""


async def _handle_unix_subprocess_timeout(
    proc: asyncio.subprocess.Process,
    timeout: int,
) -> tuple[int, str, str]:
    """Handle Unix subprocess timeout and collect best-effort output."""
    stderr_suffix = (
        f"⚠️ TimeoutError: The command execution exceeded "
        f"the timeout of {timeout} seconds. "
        f"Please consider increasing the timeout value if this command "
        f"requires more time to complete."
    )
    try:
        await _terminate_unix_process_group(proc)
        stdout, stderr = await _drain_unix_subprocess_output(proc)
        stdout_str = smart_decode(stdout)
        stderr_str = smart_decode(stderr)
        if stderr_str:
            stderr_str += f"\n{stderr_suffix}"
        else:
            stderr_str = stderr_suffix
        return -1, stdout_str, stderr_str
    except (ProcessLookupError, OSError):
        try:
            proc.kill()
            await proc.wait()
        except (ProcessLookupError, OSError):
            pass
        return -1, "", stderr_suffix


async def _wait_for_spooled_process_exit(
    proc: asyncio.subprocess.Process,
    timeout: float,
    readers: tuple[asyncio.Task[None], ...],
) -> None:
    """Notice output storage errors even when the command is still running."""
    waiter = asyncio.create_task(_wait_for_unix_process_exit(proc, timeout))
    pending = {waiter, *readers}
    try:
        while waiter in pending:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                if task is waiter:
                    task.result()
                elif isinstance(task.exception(), ShellOutputStorageError):
                    task.result()
    finally:
        if not waiter.done():
            waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def _drain_shell_readers(
    tasks: list[Awaitable[Any]],
    timeout: float | None,
) -> None:
    results = await asyncio.wait_for(
        asyncio.gather(*tasks, return_exceptions=True),
        timeout=timeout,
    )
    for result in results:
        if isinstance(result, ShellOutputStorageError):
            raise result


async def _discard_failed_shell_pipes(
    proc: asyncio.subprocess.Process,
    readers: tuple[asyncio.Task[None], ...],
) -> None:
    """Release failed readers' pipe buffers without collecting their output."""

    async def discard(stream: asyncio.StreamReader) -> None:
        while await stream.read(_SHELL_SPOOL_BATCH_BYTES):
            pass

    for stream, reader in zip((proc.stdout, proc.stderr), readers):
        if stream is None or reader.cancelled():
            continue
        if not isinstance(reader.exception(), ShellOutputStorageError):
            continue
        try:
            await asyncio.wait_for(discard(stream), timeout=1)
        except (asyncio.TimeoutError, OSError):
            pass


async def _execute_unix_subprocess(
    cmd: str,
    working_dir: Path,
    timeout: int,
    env: dict[str, str],
    preexec_fn: Callable[[], None] | None = None,
) -> tuple[int, str, str]:
    """Execute a shell command on Unix-like platforms."""
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    spool = get_active_shell_output_spool()

    def _decode_live_chunk(chunk: bytes) -> str:
        try:
            return chunk.decode("utf-8")
        except UnicodeDecodeError:
            encoding = locale.getpreferredencoding(False) or "utf-8"
            return chunk.decode(encoding, errors="replace")

    async def _read_stream(
        stream: asyncio.StreamReader | None,
        source: ToolOutputSource,
        chunks: list[bytes],
    ) -> None:
        if stream is None:
            return
        loop = asyncio.get_running_loop()
        drain_remaining = 1.0
        pending = bytearray()

        async def flush_pending() -> None:
            if spool is not None and pending:
                batch = bytes(pending)
                pending.clear()
                await spool.append(source, batch)

        try:
            while True:
                if spool is None:
                    chunk = await stream.read(4096)
                else:
                    draining = proc.returncode is not None
                    read_started = loop.time()
                    try:
                        chunk = await asyncio.wait_for(
                            stream.read(4096),
                            timeout=max(0, drain_remaining) if draining else 1,
                        )
                    except asyncio.TimeoutError:
                        if proc.returncode is not None:
                            break
                        continue
                    if draining:
                        # Bound pipe reads, excluding worker and emitter waits.
                        drain_remaining -= loop.time() - read_started
                if not chunk:
                    break
                if spool is None:
                    chunks.append(chunk)
                else:
                    available = _SHELL_SPOOL_BATCH_BYTES - len(pending)
                    if len(chunk) >= available:
                        batch = bytes(pending) + chunk[:available]
                        pending.clear()
                        # Retain the remainder before the cancellable write.
                        pending.extend(chunk[available:])
                        await spool.append(source, batch)
                    else:
                        pending.extend(chunk)
                await emit_tool_output_text(source, _decode_live_chunk(chunk))
        finally:
            await flush_pending()

    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        bufsize=0,
        cwd=str(working_dir),
        env=env,
        preexec_fn=preexec_fn,
        start_new_session=True,
    )

    if not all(hasattr(proc, attr) for attr in ("stdout", "stderr", "wait")):
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
        returncode = proc.returncode if proc.returncode is not None else -1
        stdout_str, stderr_str = await run_output_work(
            _decode_shell_streams,
            [stdout],
            [stderr],
        )
        return returncode, stdout_str, stderr_str

    try:
        process_group_id = os.getpgid(proc.pid)
    except ProcessLookupError:
        process_group_id = proc.pid

    stdout_task = asyncio.create_task(
        _read_stream(proc.stdout, "stdout", stdout_chunks),
    )
    stderr_task = asyncio.create_task(
        _read_stream(proc.stderr, "stderr", stderr_chunks),
    )
    wait_task: asyncio.Task[int] | None = None

    try:
        if spool is not None:
            await _wait_for_spooled_process_exit(
                proc,
                timeout,
                (stdout_task, stderr_task),
            )
        else:
            await _wait_for_unix_process_exit(proc, timeout=timeout)
        returncode = proc.returncode if proc.returncode is not None else -1
        await _terminate_unix_process_group(proc, process_group_id)
        try:
            await _drain_shell_readers(
                [stdout_task, stderr_task],
                # Readers bound pipe waits separately from spool worker waits.
                timeout=None if spool is not None else 1,
            )
        except asyncio.TimeoutError:
            pass
        if spool:
            stdout_str, stderr_str = await run_shell_spool_work(
                spool.decode_streams,
            )
        else:
            stdout_str, stderr_str = await run_output_work(
                _decode_shell_streams,
                stdout_chunks,
                stderr_chunks,
            )
        return returncode, stdout_str, stderr_str
    except ShellOutputStorageError as exc:
        assert spool is not None

        async def cleanup_storage_failure() -> None:
            try:
                await _terminate_unix_process_group(proc, process_group_id)
            finally:
                await asyncio.gather(
                    stdout_task,
                    stderr_task,
                    return_exceptions=True,
                )
                await _discard_failed_shell_pipes(
                    proc,
                    (stdout_task, stderr_task),
                )

        await await_shell_cleanup_task(
            asyncio.create_task(cleanup_storage_failure()),
        )
        raise await run_shell_spool_work(
            spool.storage_failure,
            "write",
            proc.returncode,
        ) from exc
    except asyncio.TimeoutError:
        stderr_suffix = (
            f"⚠️ TimeoutError: The command execution exceeded "
            f"the timeout of {timeout} seconds. "
            f"Please consider increasing the timeout value if this command "
            f"requires more time to complete."
        )
        try:
            await _terminate_unix_process_group(proc, process_group_id)
        except (ProcessLookupError, OSError):
            try:
                proc.kill()
                await proc.wait()
            except (ProcessLookupError, OSError):
                pass
        timeout_tasks: list[Awaitable[Any]] = [stdout_task, stderr_task]
        if spool is None:
            wait_task = asyncio.create_task(proc.wait())
            timeout_tasks.append(wait_task)
        try:
            await _drain_shell_readers(
                timeout_tasks,
                timeout=None if spool is not None else 1,
            )
        except ShellOutputStorageError as exc:
            assert spool is not None
            await await_shell_cleanup_task(
                asyncio.create_task(
                    _discard_failed_shell_pipes(
                        proc,
                        (stdout_task, stderr_task),
                    ),
                ),
            )
            raise await run_shell_spool_work(
                spool.storage_failure,
                "write",
                proc.returncode,
            ) from exc
        except asyncio.TimeoutError:
            pass
        if spool:
            stdout_str, stderr_str = await run_shell_spool_work(
                spool.decode_streams,
                stderr_suffix,
            )
        else:
            stdout_str, stderr_str = await run_output_work(
                _decode_shell_streams,
                stdout_chunks,
                stderr_chunks,
                stderr_suffix,
            )
        await emit_tool_output_text("stderr", stderr_suffix)
        return -1, stdout_str, stderr_str
    except asyncio.CancelledError:

        async def cleanup_cancelled_process() -> None:
            await _terminate_unix_process_group(proc, process_group_id)
            await asyncio.gather(
                stdout_task,
                stderr_task,
                return_exceptions=True,
            )
            if spool is not None:
                await _discard_failed_shell_pipes(
                    proc,
                    (stdout_task, stderr_task),
                )

        await await_shell_cleanup_task(
            asyncio.create_task(cleanup_cancelled_process()),
        )
        raise
    finally:
        for task in (stdout_task, stderr_task, wait_task):
            if task is not None and not task.done():
                task.cancel()


async def _execute_platform_subprocess(
    cmd: str,
    working_dir: Path,
    timeout: int,
    env: dict[str, str],
    preexec_fn: Callable[[], None] | None = None,
) -> tuple[int, str, str]:
    """Execute a shell command on the active platform."""
    if sys.platform == "win32":
        # Windows: use thread pool to avoid asyncio subprocess limitations
        return await asyncio.to_thread(
            _execute_subprocess_sync,
            cmd,
            str(working_dir),
            timeout,
            env,
        )
    return await _execute_unix_subprocess(
        cmd,
        working_dir,
        timeout,
        env,
        preexec_fn=preexec_fn,
    )


# pylint: disable=too-many-branches, too-many-statements
async def execute_shell_command(
    command: str,
    timeout: int = 60,
    cwd: Optional[Path | str] = None,
) -> ToolResponse:
    """Execute a shell command and return its output.

    Platform shells: Windows uses cmd.exe; Linux/macOS use /bin/sh or /bin/bash.

    IMPORTANT: Always consider the operating system before choosing commands.

    Args:
        command (`str`):
            The shell command to execute.
        timeout (`int`, defaults to `60`):
            The maximum time (in seconds) allowed for the command to run.
            Default is 60 seconds.
        cwd (`Optional[Path]`, defaults to `None`):
            The working directory for the command execution.
            If None, defaults to the current agent workspace when available and
            otherwise falls back to the tenant workspace root.

    Returns:
        `ToolResponse`:
            The tool response containing the return code, standard output, and
            standard error of the executed command. If timeout occurs, the
            return code will be -1 and stderr will contain timeout information.
    """

    async with _prepared_shell_execution(command, cwd) as (
        prepared,
        process_limit_policy,
    ):
        preexec_fn = process_limit_policy.build_preexec_fn()
        spool = None
        try:
            spool = (
                await run_output_work(
                    create_shell_output_spool,
                    smart_decode,
                    _format_shell_response,
                )
                if sys.platform != "win32"
                else None
            )
            async with _tenant_shell_execution_slot(process_limit_policy):
                with spool.activate() if spool else nullcontext():
                    (
                        returncode,
                        stdout_str,
                        stderr_str,
                    ) = await _execute_platform_subprocess(
                        prepared.command,
                        prepared.working_dir,
                        timeout,
                        prepared.env,
                        preexec_fn=preexec_fn,
                    )

                    response_text = await (
                        run_shell_spool_work if spool else run_output_work
                    )(
                        _prepare_shell_response,
                        returncode,
                        stdout_str,
                        stderr_str,
                        process_limit_policy,
                    )

            return _tool_text_response(response_text)

        except Exception as e:
            if isinstance(e, ToolExecutionError):
                raise
            _raise_shell_error(
                "unexpected_tool_error",
                f"Error: Shell command execution failed due to \n{e}",
            )
        finally:
            if spool is not None:
                await spool.aclose()


def _decode_shell_streams(
    stdout_chunks: list[bytes],
    stderr_chunks: list[bytes],
    stderr_suffix: str = "",
) -> tuple[str, str]:
    """Join and decode complete output outside the event-loop thread."""
    stdout = smart_decode(b"".join(stdout_chunks))
    stderr = smart_decode(b"".join(stderr_chunks))
    if stderr_suffix:
        stderr = f"{stderr}\n{stderr_suffix}" if stderr else stderr_suffix
    return stdout, stderr


def _prepare_shell_response(
    returncode: int,
    stdout: str,
    stderr: str,
    policy: CurrentProcessLimitPolicy,
) -> str:
    """Format and classify data-size-dependent output in the output worker."""
    spool = get_active_shell_output_spool()
    if spool is None:
        text = _format_process_limit_diagnostic(
            _format_shell_response(returncode, stdout, stderr),
            policy,
        )
    else:
        text, stderr = spool.prepare_response(
            returncode,
            stdout,
            stderr,
            _format_process_limit_diagnostic("", policy),
        )
    if returncode != 0:
        _raise_shell_error(
            _classify_shell_failure(
                returncode,
                stderr,
                process_limits_enforced=policy.should_enforce,
                memory_limit_enforced=policy.should_enforce_memory_limit,
            ),
            text,
        )
    return text


def smart_decode(data: bytes) -> str:
    try:
        decoded_str = data.decode("utf-8")
    except UnicodeDecodeError:
        encoding = locale.getpreferredencoding(False) or "utf-8"
        decoded_str = data.decode(encoding, errors="replace")

    return decoded_str.strip("\n")
