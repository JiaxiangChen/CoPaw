#!/usr/bin/env bash
# Read-only checks using the same Python environment and settings as SWE.
set -euo pipefail

if [[ "${1:-}" == "--help" ]]; then
    echo "Usage: PYTHON_BIN=venv/bin/python $0 <storage-tenant-directory-name> <source-id>"
    exit 0
fi

if [[ $# -ne 2 || -z "$1" || -z "$2" ]]; then
    echo "Usage: PYTHON_BIN=venv/bin/python $0 <storage-tenant-directory-name> <source-id>" >&2
    exit 2
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
exec "$PYTHON_BIN" - "$1" "$2" <<'PY'
import sys
from pathlib import Path

from swe.constant import WORKING_DIR, SECRET_DIR
from swe.app.workspace.bootstrap_state import inspect_bootstrap_readiness
from swe.app.workspace.source_template_provisioner import (
    inspect_source_template_readiness,
)

base = Path(WORKING_DIR)
tenant_dir = base / sys.argv[1]
source_id = sys.argv[2]

print("WORKING_DIR:", base)
print("SECRET_DIR:", SECRET_DIR)

checks = [
    ("用户目录", tenant_dir, inspect_bootstrap_readiness(tenant_dir)),
    (
        "source 模板",
        base / f"default_{source_id}",
        inspect_source_template_readiness(base, source_id),
    ),
]
for name, directory, state in checks:
    print(f"\n[{name}] {directory}")
    print("ready:", state.ready, "reason:", state.reason)
    print("缺失路径:")
    for path in state.missing_paths:
        print(" ", path)
    print("无效 JSON/配置路径:")
    for path in state.invalid_json_paths:
        print(" ", path)
PY
