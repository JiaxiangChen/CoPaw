# Expert Community Target Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Expert Community distribution and withdrawal use the Application Marketplace target-selection interaction, limited to the current source scope.

**Architecture:** Extend the existing target modal with an expert distribution mode that omits skill preview data, add an expert withdrawal modal backed by a new received-copy listing endpoint, and keep the existing expert service target semantics and manager authorization. The expert UI will pass explicit user IDs only; institution selection is expanded by `TenantSelector` into users.

**Tech Stack:** React, TypeScript, Ant Design, FastAPI, Pydantic, pytest, Vitest.

---

### Task 1: Add expert received-copy listing contract

**Files:**
- Modify: `market/src/market/marketplace/service.py`
- Modify: `market/src/market/app/routers/experts_market.py`
- Modify: `console/src/api/modules/market.ts`
- Test: `tests/unit/market/test_experts_market.py`

- [ ] **Step 1: Write the failing route/API test**

Add a manager-only `GET /market/experts/{item_id}/distributions` test that returns user IDs and institution metadata for both actively received expert copies and administrator-distributed copies.

- [ ] **Step 2: Run the focused test and verify it fails**

Run: `venv/bin/python -m pytest tests/unit/market/test_experts_market.py -k distributions -v`

Expected: FAIL because the expert distributions route/service method does not exist.

- [ ] **Step 3: Implement the smallest service and route contract**

Expose the existing received-copy scan as a list of `DistributionRecord`-shaped rows, deduplicated by user and scoped by `source_id` plus `item_id`; require `X-Manager: true` in the route. Add `marketApi.getExpertDistributions`.

- [ ] **Step 4: Run the focused test and verify it passes**

Run: `venv/bin/python -m pytest tests/unit/market/test_experts_market.py -k distributions -v`

Expected: PASS.

### Task 2: Reuse distribution target selection for experts

**Files:**
- Modify: `console/src/pages/Market/DistributeTargetModal.tsx`
- Modify: `console/src/pages/ExpertCommunity/index.tsx`
- Test: `console/src/pages/ExpertCommunity/expertCommunity.test.ts`

- [ ] **Step 1: Write the failing UI behavior test**

Assert that opening expert distribution renders `TenantSelector`, does not render skill preview content, submits `target_type: "user_id"` with selected tenant IDs, and does not expose an all-users option.

- [ ] **Step 2: Run the focused test and verify it fails**

Run: `pnpm test:run console/src/pages/ExpertCommunity/expertCommunity.test.ts`

Expected: FAIL because the page currently submits `target_type: "all"` immediately and has no target modal.

- [ ] **Step 3: Implement the shared expert distribution mode**

Add `"expert"` to the modal type, skip skill preview fetching/rendering for expert mode, call `marketApi.distributeExpert` with explicit selected IDs, and replace the Expert Community direct action with modal state/open/close/success handling. Keep manager-only visibility.

- [ ] **Step 4: Run the focused test and verify it passes**

Run: `pnpm test:run console/src/pages/ExpertCommunity/expertCommunity.test.ts`

Expected: PASS.

### Task 3: Reuse target selection for expert withdrawal

**Files:**
- Create: `console/src/pages/ExpertCommunity/ExpertRecallModal.tsx`
- Modify: `console/src/pages/ExpertCommunity/index.tsx`
- Test: `console/src/pages/ExpertCommunity/expertCommunity.test.ts`

- [ ] **Step 1: Write the failing withdrawal interaction test**

Assert that the withdrawal modal loads only actual expert holders, allows institution/user selection through `TenantSelector`, submits selected user IDs to `marketApi.recallExpert`, and renders partial-failure user reasons.

- [ ] **Step 2: Run the focused test and verify it fails**

Run: `pnpm test:run console/src/pages/ExpertCommunity/expertCommunity.test.ts`

Expected: FAIL because the modal and expert distribution-list API are absent.

- [ ] **Step 3: Implement the modal and page wiring**

Use `marketApi.getExpertDistributions` on open, derive holder IDs, render the shared selector constrained to those holders (no manual IDs), submit `{ target_user_ids }`, and show success/partial-failure feedback before refreshing the page. Replace both card and detail-drawer recall callbacks with modal opening.

- [ ] **Step 4: Run frontend checks**

Run: `pnpm test:run console/src/pages/ExpertCommunity/expertCommunity.test.ts` and `pnpm exec tsc --noEmit`.

Expected: PASS with no TypeScript errors.

### Task 4: Verify regressions and graph impact

**Files:**
- Test only; no additional source changes expected.

- [ ] **Step 1: Run backend expert tests**

Run: `venv/bin/python -m pytest tests/unit/market/test_experts_market.py tests/unit/market/test_expert_service.py -v`

- [ ] **Step 2: Run relevant frontend tests**

Run: `pnpm test:run console/src/pages/Market/MarketSkills.test.ts console/src/pages/ExpertCommunity/expertCommunity.test.ts`

- [ ] **Step 3: Run graph change analysis before completion**

Run: `node .gitnexus/run.cjs detect-changes --scope all`

Expected: cleanly reports the changed Expert Community, Market modal, route, and service surfaces without unexpected unrelated modules.
