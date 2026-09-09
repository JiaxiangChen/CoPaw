# Model-Level Runtime Config Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist generation, length, and reasoning configuration by tenant Provider/model and apply it to every model invocation.

**Architecture:** Add a typed `model_configs` dictionary to Provider persistence and expose narrow model-config APIs. The factory resolves the selected model's configuration once, converts it to generation arguments, and overlays the effective input budget on the Agent runtime. Console model management edits capabilities and parameters; Chat edits only supported thinking values.

**Tech Stack:** Python/Pydantic/FastAPI/pytest; React/TypeScript/Vitest.

---

### Task 1: Provider model configuration persistence and API

**Files:** `src/swe/providers/provider.py`, `src/swe/providers/provider_catalog_service.py`, `src/swe/providers/provider_manager.py`, `src/swe/app/routers/providers.py`, `tests/unit/providers/`, `tests/integration/`

- [x] Write failing tests for validation, update, delete cleanup, legacy `generate_kwargs` omission, and distribution payloads.
- [x] Add typed per-model configuration and targeted read/write operations.
- [x] Run focused pytest tests and commit.

### Task 2: Runtime configuration application

**Files:** `src/swe/agents/model_factory.py`, `src/swe/agents/react_agent.py`, snapshot call paths, `tests/unit/agents/`, `tests/integrated/critical_paths/`

- [x] Write failing tests proving generation fields and provider-specific output limit mapping reach every factory-created model, and input budget overrides Agent defaults.
- [x] Resolve model config in the factory; apply it before wrappers and preserve it in frozen Provider snapshots.
- [x] Run focused pytest tests and commit.

### Task 3: Console editing and Chat thinking controls

**Files:** `console/src/api/types/provider.ts`, `console/src/api/modules/provider.ts`, model-management and Chat selector components, tests.

- [x] Write failing component/API tests for model config controls and removal of `generate_kwargs` editor.
- [x] Add model management configuration dialog and current-model Chat thinking controls with immediate persistence.
- [x] Run focused Vitest tests/build and commit.

### Task 4: Cross-cutting verification and review

**Files:** changed files and `CONTEXT.md`

- [x] Run relevant Python and Console suites, formatting/linting, and graph change analysis.
- [x] Request subagent specification and quality reviews; fix all findings.
- [x] Commit final fixes and report evidence against every goal requirement.
