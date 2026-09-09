# CoPaw Console Design System

This document is the single design source of truth for new and modified user-facing UI under `console/`. Git history and archived OpenSpec changes preserve prior decisions; do not create competing project design manuals or vendor third-party `DESIGN.md` files.

## Document Status

- Status: white-first embedded baseline under calibration.
- Reference implementation: global Header/navigation and `/models`.
- Strategic product context: root `PRODUCT.md` describes users, purpose, and anti-references for AI-assisted UI work; this file remains the Console UI visual authority.
- Ownership: update this document in the same OpenSpec change whenever an accepted UI change introduces or revises a reusable visual rule.
- Adoption: new and modified UI follows this document; untouched legacy UI migrates through separate changes.

## Scope And Adoption

- Apply these rules to every new Console surface and to the visible region of any existing surface being changed.
- Adopt the system incrementally. Do not globally restyle untouched legacy pages as a side effect of another change.
- UI work may reorganize layout, extract presentational components, consolidate tokens, and move local inline styles into Less/CSS Modules.
- UI work must not change API contracts, request parameters, route paths, permission checks, iframe messages, Zustand state meaning, event-handler outcomes, validation, error handling, or business operation semantics unless a separate approved requirement says so.
- The actively governed Console design baseline is light. Existing dark-mode code and compatibility styles may remain, but new UI work should not extend dark-theme behavior unless a separate approved requirement calls for it.

## Reference Direction

CoPaw does not adopt named third-party product styles as visual authorities. The Console uses its own white-first embedded management language, tuned for Chinese AI operations, host-product integration, compact workflows, configured logos, and the independent Conversation Workspace identity.

AI-assisted UI work may use `ui-ux-pro-max` to explore candidate visual directions, `frontend-design` to evaluate and refine design fundamentals, and, when available and warranted by task complexity, Impeccable to implement, critique, and polish the selected direction. These skills are advisory layers rather than visual authorities, and none of them replaces this document.

The Conversation Workspace keeps `#3769FC` as its fixed emphasis color unless a later approved chat-specific change revises it. Non-chat management surfaces may treat that color as a reference rather than a mandatory page-level identity, while preserving shared semantic behavior and accessibility.

When outside examples or prior explorations inform a decision, rewrite the durable rule as a CoPaw-specific principle before it becomes authoritative. Archived OpenSpec changes preserve historical exploration; this document remains the day-to-day design source of truth.

## Product Character

The Console should feel integrated, calm, precise, and quietly capable. Management surfaces should use white-first embedding, near-white functional grouping, practical whitespace, readable hierarchy, and compact discoverable operations without creating a competing visual brand inside the host product.

The target is advanced enterprise SaaS product UI: familiar enough to trust immediately, refined enough to avoid commodity admin-panel sloppiness, and restrained enough that repeated operational work stays comfortable. Product confidence comes from hierarchy, alignment, durable states, and predictable controls rather than spectacle.

Avoid decorative gradients, oversized cards, heavy shadows, excessive rounded containers, large saturated accent areas, low-density marketing layouts, generic "AI dashboard" hero composition, and visuals that only look good with perfect demo data.

## Enterprise SaaS Quality Bar

Future Console UI should pass these product checks before being treated as complete:

- **Task-first composition**: the first viewport exposes the current task, current state, and primary operation. Do not lead management surfaces with marketing hero copy, large illustrations, or feature explanations.
- **Earned familiarity**: use standard SaaS affordances for navigation, filters, tables, forms, menus, dialogs, tabs, status badges, and pagination. Do not invent controls for flavor.
- **Medium-high density**: prefer compact rows, clear grouping, and efficient desktop width usage. Increase whitespace only when it improves scanning or reduces decision load.
- **Stable vocabulary**: the same operation type should use the same button hierarchy, icon scale, control height, state treatment, and confirmation pattern across pages.
- **Visible operations**: primary actions stay visible and named. Secondary actions may be quieter, but required operations must not be hidden behind hover-only controls.
- **State completeness**: any new or revised workflow accounts for loading, empty, error, disabled, unavailable, permission-limited, in-progress, success, and destructive states when applicable.
- **Real-data resilience**: designs must survive long Chinese names, long English identifiers, provider URLs, IDs, empty values, large counts, many rows, and narrow embedded containers.
- **No AI slop**: avoid one-note palettes, purple/blue gradient drama, nested cards, vague "insight" panels, decorative bokeh/orbs, oversized rounded pills, and generic icon-card grids that do not map to real operations.

Good Console screens should feel like reliable tools used by people with work to finish: quiet, precise, and a little sharper than expected.

## AI-Assisted UI Workflow

For non-trivial visible UI optimization or redesign, use the following workflow as a risk-based default. Not every task requires every stage; the stages used may iterate, but later stages must reconcile their output with this document rather than silently replacing an accepted CoPaw rule.

1. **Explore with `ui-ux-pro-max`**: generate candidate directions for style, color, typography, layout, component treatment, and relevant anti-patterns. Select the `react` stack and translate recommendations into the existing React and Ant Design architecture; do not introduce an HTML + Tailwind rewrite from the skill's default stack.
2. **Converge with `frontend-design`**: evaluate the candidates against visual hierarchy, typography, spacing rhythm, layout, component states, responsive behavior, and accessibility. Use these principles to improve a direction, not to bootstrap a competing generic token system.
3. **Implement and polish with Impeccable when useful and available**: for complex visible UI or pre-ship refinement, use it to implement the selected direction and catch generic AI patterns or incomplete finish work.
4. **Review with `copaw-f2e-review`**: verify React and TypeScript quality, state and API contracts, accessibility, real-data resilience, and CoPaw-specific behavior.
5. **Verify with `browser-qa`**: test the rendered result against this document's Verification scenarios, including the required desktop host sizes and `hideMenu=true` where applicable; exercise interactions and keyboard states, and inspect browser errors before delivery.

When the user has provided or confirmed a clear visual direction, the exploration and convergence stages may be omitted. Small mechanical changes with an already-specified result may shorten the workflow further when their risk does not justify every stage. Missing optional skills must not block normal development or delivery; project review and browser verification provide the required fallback.

Workflow safeguards:

- Treat automated design checks as advisory signals for nested cards, generic gradients, low-contrast text, over-rounded containers, cramped spacing, skipped heading structure, small touch targets, text overflow, unstable responsive layouts, and incomplete operational states.
- Record recurring valid findings as updates to this document or central design tokens when they reveal a reusable rule.
- Do not persist or commit skill-generated design systems, context documents, or other competing project authorities unless an accepted change explicitly replaces this document's source-of-truth strategy.
- Treat `colorize`, `bolder`, `quieter`, `delight`, `animate`, `overdrive`, and similar identity-shifting commands as exploratory unless the user has explicitly approved that visual direction through the repository workflow.
- If a skill flags a deliberate CoPaw choice, such as platform font stacks, white-first management surfaces, neutral text roles, compact operational density, or `#3769FC`, this document wins; document reusable clarifications here rather than relying on informal memory.
- Automatic skill hooks and project-local tool configuration are optional. Adding them requires an accepted change that explains the files added, approval steps, expected scope, and rollback path.

## Theme Architecture

The visual system has three layers:

1. **Base foundation**: spacing, radii, elevation, motion, semantic status, and accessibility behavior shared where appropriate.
2. **Management Console theme**: white-first embedded canvas, white operational surfaces, near-white functional panels, blue actions, and multilingual management typography.
3. **Conversation Workspace theme**: existing blue emphasis, existing typography, and conversation-specific presentation.

Use semantic roles rather than page-specific literal colors. New non-chat pages must be able to adopt the Management Console theme without depending on `/models` classes. Do not replace the global `body` font or a single application-wide primary color in a way that leaks management styling into chat or untouched legacy pages.

The global Header and navigation use the Management Console theme even when displayed beside chat. The chat content region and independent conversation sidebar remain in the Conversation Workspace theme.

## Foundations

### Typography

The current baseline uses platform fonts so it works without downloading font assets or depending on a runtime font CDN.

```css
--console-font-ui: "Microsoft YaHei", "PingFang SC", "Helvetica Neue",
  sans-serif;

--console-font-editorial: Georgia, "Songti SC", "SimSun", serif;

--console-font-mono: SFMono-Regular, Consolas, "Liberation Mono", monospace;
```

- UI role: navigation, breadcrumbs, controls, forms, buttons, tables, cards, metadata, and operational section headings.
- Editorial role: limited to approved welcome, featured guidance, or other content-led surfaces. Do not use editorial/serif typography for management page titles, forms, tables, cards, buttons, dense lists, or other operational workflows.
- Technical role: provider IDs, URLs, paths, code, logs, and machine-oriented values.
- Preferred UI weights: 400, 500, and 600. Preferred editorial weights: 500 and 600.
- A later typography-specific change may introduce locally hosted Poppins/Noto Sans SC and Lora/Noto Serif SC assets. Until then, do not add a font CDN or assume those families are present.
- Visible page title when needed: 18-22px / 26-30px, weight 600. A compact breadcrumb-only management header may omit a duplicate visible title while retaining a semantic heading.
- Section title: 16px / 24px, weight 600.
- Body: 14px / 22px, weight 400.
- Compact control or metadata: 12-13px / 18-20px, weight 500 when needed for Windows clarity.
- Avoid weights below 400 for Chinese UI and avoid relying on macOS-only thin-font rendering.

### Color Roles

| Role                     | Initial value | Usage                                              |
| ------------------------ | ------------- | -------------------------------------------------- |
| Management canvas        | `#FFFFFF`     | Embedded page background                           |
| Surface                  | `#FFFFFF`     | Cards, dialogs, and interactive panels             |
| Subtle surface           | `#F7F9FC`     | Secondary groups, controls, inactive regions       |
| Navigation surface       | `#FFFFFF`     | Low-distraction global navigation                  |
| Border                   | `#E5E7EB`     | Default separators and component borders           |
| Strong border            | `#D0D7E2`     | Focused group boundaries                           |
| Primary text             | `#111827`     | Titles and main content                            |
| Secondary text           | `#4B5563`     | Descriptions and labels                            |
| Muted text               | `#8A94A6`     | Supporting metadata and placeholders               |
| Management primary       | `#3769FC`     | Default management actions and focused emphasis    |
| Management primary hover | `#2957DC`     | Hover/active management action state               |
| Primary soft             | `#EEF4FF`     | Selection and low-emphasis blue feedback           |
| Conversation primary     | `#3769FC`     | Chat selection, composer, and conversation actions |
| Success                  | `#2F7D5B`     | Available and successful states                    |
| Warning                  | `#A56A24`     | Partial and caution states                         |
| Error                    | `#B94A4F`     | Destructive and failed states                      |

Blue is the current reference emphasis across management and conversation surfaces, but each theme retains separate semantic variables and component scopes. Conversation primary remains fixed at `#3769FC`. Non-chat management surfaces may adapt their color balance and scoped visual expression to the workflow; `#3769FC` is a default reference rather than a mandatory page-level primary. Reusable changes to the Management primary or other cross-page semantic roles must update the central tokens, this table, and affected surfaces together. Saturated color is reserved for primary actions, focus, and concise selection; large surfaces use white, with near-white gray-blue reserved for functional grouping.

`console/src/config/consoleDesignTokens.ts` is the normal palette-edit entry point. Change semantic token values there, keep role names stable, update this table, and verify affected surfaces. Do not tune the theme by scattering hexadecimal colors across page styles.

### Spacing

Use a 4px base rhythm. Preferred steps: `4, 8, 12, 16, 20, 24, 32, 40`.

- Dense control gaps: 8px.
- Related content groups: 12-16px.
- Section separation: 20-32px.
- Desktop page gutters: 20-32px, selected according to content density and available shell width.
- Do not add a card merely to create spacing.

### Radius, Borders, And Elevation

- Small controls: 6px.
- Inputs and buttons: 8px.
- Content cards and dialogs: 12px. Larger marquee or configuration panels may use 16px.
- Pill radius is reserved for badges and compact filters.
- Prefer neutral one-pixel borders and surface contrast over shadows.
- Default shadow: `0 1px 2px rgba(35, 31, 27, 0.04)`.
- Raised overlay shadow: `0 14px 36px rgba(35, 31, 27, 0.12)`.
- Hover must not move layout. Use border, background, color, or subtle shadow transitions of 150-220ms.

### Icons

- Keep AgentScope icons for AgentScope-specific concepts.
- Use `@ant-design/icons` by default for generic interface icons in Ant Design-based management surfaces.
- Use `lucide-react` only where already established in the changed surface, or when Ant Design does not provide a suitable glyph.
- Do not mix icon families casually within the same component group.
- Functional icons must not be emoji.
- Use consistent 16-18px navigation/control icons and align them to the text baseline.

### Interaction And Accessibility

- All clickable controls need hover, visible keyboard focus, disabled, and in-progress states.
- Do not hide primary actions behind hover-only UI.
- Labels must remain associated with form controls; placeholders are not labels.
- Dynamic loading, success, warning, and failure states must remain distinguishable without relying only on color.
- Use blue focus rings with sufficient contrast on Management Console surfaces; preserve conversation-specific focus behavior inside chat.
- Respect `prefers-reduced-motion` for newly introduced non-essential animation.

## Global Shell And Navigation

- The Header and global navigation use white, low-distraction surfaces with restrained text hierarchy, light boundaries, and blue reserved for concise interaction emphasis.
- The current page owns visual attention. Navigation uses pale blue hover and selection surfaces, moderate text weight, and restrained icon color.
- All first-level navigation entries, including expandable groups and direct root links, use the same UI font, 14px size, 600 weight, and 36px line box. Second-level entries also use 14px but rely on 400 weight, indentation, quieter color, and selection treatment to preserve hierarchy.
- Global navigation may appear beside the blue Conversation Workspace without changing either theme's identity.
- Preserve menu structure, ordering, labels, routes, permissions, expanded groups, and collapse behavior.
- Preserve the `hideMenu` contract: embedded hosts may remove both Header and global Sidebar.
- Preserve source-specific logo assets and dimensions. Only the layout around the logo may change.
- The navigation collapse control remains visible and discoverable without becoming a dominant accent.

## Conversation Workspace

The Conversation Workspace is intentionally outside the current management-theme migration. It keeps `#3769FC`, its existing typography, content surfaces, independent conversation sidebar, composer, and conversation-specific presentation.

Visual priority for future chat work remains:

1. Conversation, generated content, and current-task execution content.
2. Composer, send state, attachments, and model context.
3. My Tasks and History lists used for switching context.
4. Tool-call details, progress details, and generated files.
5. Featured cases and onboarding guidance.
6. Global navigation.

- Preserve the existing conversation sidebar width until a later approved chat migration changes it.
- Preserve its collapse behavior.
- In ordinary chats, open generated-file and HTML previews in a non-modal right-side panel on desktop so the conversation remains visible and interactive. The panel may use a full-width overlay when the host is too narrow to keep both surfaces usable, and explicit full-screen preview remains available as a secondary action. Scheduled-task HTML results retain the existing centered modal presentation.
- Future chat redesigns may reuse base accessibility and spacing roles while evolving its visual theme independently from management pages.
- Future chat redesigns must work both with and without the global navigation.

### Chat Dictation

- The new-chat and active-chat composers share an always-discoverable microphone control immediately before Send. This short dictation workflow is independent of the existing allowlisted recording item in the quick menu.
- During dictation, replace the quick-action row with Cancel, a neutral dotted audio waveform, and Stop. Keep Send visible but disabled until recognition ends. Show the recognition preview above the waveform; Stop appends it to the existing draft without submitting, while Cancel discards only this dictation.
- Waveform history follows actual microphone amplitude, with quiet audio rendered as dots and speech as rounded vertical marks. Under reduced motion, retain static dots and textual recording state. Controls retain the Conversation Workspace focus color.
- Permission requests, unavailable browsers, recognition failures, cancellation, and stopping must have explicit states. Release microphone tracks on stop, cancellation, errors, composer disablement, and conversation changes.

### Content-Only Conversation Workspace

A content-only Conversation Workspace keeps the existing conversation title, routed message content, message-level actions, typography, and `#3769FC` emphasis. It omits global navigation, the conversation task/history sidebar and collapsed toolbar, model selection, the independent generated-files entry/list, and composer/upload surfaces. Omitted surfaces are not focusable and reserve no width, padding, border, or shell-colored gap; message-level file controls remain part of the conversation content.

## Management Console

Management pages use medium-high information density with white-first host integration, near-white functional grouping, and white operational surfaces. Prefer compact controls, efficient use of desktop width, clear section hierarchy, practical whitespace, and blue primary actions.

Choose one of three page patterns:

1. **Standard management page**: compact breadcrumb when route hierarchy is meaningful, otherwise a compact page heading; optional description, filter/action bar, table or compact card grid.
2. **List-detail page**: stable left list and flexible right detail panel.
3. **Dashboard page**: filter bar, concise metrics, charts, and supporting tables.

Shared rules:

- Use one compact page-level breadcrumb with a small contextual icon when route hierarchy is meaningful. Otherwise use a compact page heading. Do not show both when they repeat the same current page title.
- Keep primary actions near the page or section title; group low-frequency actions in a visible more-actions menu.
- Use the available desktop width with moderate gutters. Bound prose and form fields locally rather than centering the entire page inside a narrow container.
- Cards should communicate a distinct item, status, or action group. Avoid wrapping every section in a decorative card.
- Let the white page canvas integrate with the host and connect related sections. Reserve near-white subtle surfaces for functional grouping and white surfaces for actual interactive panels, dialogs, and distinct item cards; do not stack a white page container, white section container, and white child card around the same content.
- Use the UI font for operational interfaces and reserve the editorial font only for approved content-led moments.
- Empty, loading, error, disabled, unavailable, and in-progress states use consistent spacing, icon scale, title, explanation, and recovery-action placement.
- Select, dropdown, and menu overlays on Management Console surfaces use white elevated panels, near-white hover states, and `Primary soft` selected states instead of neutral gray selection fills.
- Data tables and dense lists should keep row rhythm stable under hover, selection, loading, and inline action states. Use truncation, wrapping, tooltips, or detail expansion intentionally; never let a long value push primary operations off screen.
- Forms should use visible labels, compact helper text, inline validation near the field, and preserved user input after recoverable errors. Avoid placeholder-only labels.
- Filters and search controls should sit close to the result set they affect, expose active filter state, and provide a clear reset path when no results are returned.

## Production Hardening

Designs that only work with ideal content are not complete. New and modified UI must be hardened against the data and conditions users actually produce.

### Dynamic Text And Data

- Long names, URLs, model IDs, provider IDs, paths, tenant labels, and generated titles need explicit overflow behavior.
- Single-line metadata may truncate with an accessible full-value affordance when the complete value matters.
- Multi-line descriptions should wrap without breaking cards, tables, or action alignment.
- Empty, null, unknown, unavailable, and pending values should use consistent copy and muted visual treatment rather than leaving blank holes.
- Large counts, high row totals, and many filter options should preserve layout stability and provide search, pagination, grouping, or progressive disclosure as appropriate.

### Internationalized Content

- Chinese UI text must remain readable at documented sizes and weights on Windows and macOS.
- English identifiers, mixed CJK/Latin strings, punctuation, and technical values must not create horizontal page overflow.
- Avoid fixed text containers that only fit short English labels. Use flexible widths, `min-width: 0` in flex/grid layouts, and wrapping where needed.
- Icon-only controls require accessible names and visible affordances; text labels are preferred for primary or risky operations.

### Operational States

- Loading states should preserve surrounding layout and communicate what is loading. Prefer skeletons or stable placeholders for content regions over isolated spinners.
- Empty states should explain what is missing and expose the next valid action when one exists.
- Error states should state what failed, keep user input when possible, and provide retry, correction, or navigation recovery.
- Permission-limited states should explain access constraints without presenting unusable primary actions as if they were available.
- Destructive and irreversible operations require clear confirmation, explicit target naming, and consistent button hierarchy.

### Responsive And Embedded Behavior

- Management pages must remain usable in embedded host containers and with `hideMenu=true` when the route supports it.
- No page-level horizontal overflow is allowed at the required desktop verification sizes.
- Collapsed navigation, narrow content columns, and long text must not overlap page actions, tabs, filter bars, dialogs, or table controls.
- Hover, focus, selected, expanded, and loading states must not resize fixed-format controls or shift surrounding layout.

## Model Management Reference Pattern

The `/models` page is the first complete white-first embedded Management Console reference.

- Default LLM configuration is a compact, clearly labelled 16px-radius panel using the near-white subtle surface and a neutral hairline border.
- The page uses open canvas-level sections separated by spacing or a quiet rule rather than enclosing both major sections in large white cards.
- Provider results use a wrapping equal-width card row. Cards share the available row width, add columns when their practical minimum width fits, and wrap only when needed; an incomplete final row expands to avoid a conspicuous empty column.
- Each provider card retains icon, name, ID, status, Base URL or equivalent connection summary, model count, and operations.
- Provider cards use a compact identity header, one continuous aligned summary list, a white surface with a neutral hairline boundary, a standard 12px content-card radius, and a quiet unfilled action row.
- Model and Settings actions remain directly visible as equal-height, low-emphasis icon-and-text actions. Destructive or low-frequency actions may use an icon-only more-actions menu aligned with them.
- Primary page actions use the Management primary token. Secondary and card-level actions remain visually quieter.
- Breadcrumbs, controls, cards, and section titles use the UI font; Provider IDs and URLs use the technical font.
- Dialogs use consistent header, body spacing, field rhythm, notices, list rows, and footer alignment.
- Provider and model operations keep their current handlers, validation, confirmation, and result semantics.

## Verification

For major desktop migrations, inspect:

- Embedded host containers corresponding to `1280x720`, `1440x900`, and `1920x1080` viewport sizes.
- `hideMenu=true` with the page filling the host content region and no shell-colored gaps.
- Windows Chrome and Edge using the declared platform font stack; retain readable macOS fallbacks.
- No external font-network requests.
- No unintended clipping, overlap, inaccessible actions, or horizontal page overflow.
- Normal, hover, focus, loading, empty, error, disabled, unavailable, and in-progress states.
- A representative chat route for unchanged blue emphasis, typography, conversation sidebar, and layout.
- Representative untouched legacy pages after shared-token changes when the global shell is visible during development.
- Existing build, lint, tests, and core business interaction paths.

Update this document and the implementation together after visual feedback before treating a revised rule as stable.

## Deferred Migration Backlog

Legacy adoption remains intentionally incremental. Each area below requires its own future OpenSpec change:

1. Remaining system-setting and operational pages: selected-case management, environment variables, security policy, channels, runtime configuration, scheduled tasks, and heartbeat.
2. Creation and resource pages: files, skills, built-in tools, MCP, and application-market surfaces.
3. Insight and quality pages: operations dashboards, Claw analytics, user messages, and continuous governance.
4. Conversation Workspace: conversation content, My Tasks, composer/send states, history, tool calls, progress, generated files, featured cases, and embedded presentation. This is a separate design migration and must not be implied by management-theme work.
5. Cleanup work: remove dormant dark-theme switching only through a separate approved change.
6. Optional typography assets: evaluate locally hosted multilingual UI/editorial fonts, licensing, payload, and cross-platform rendering in a separate change before replacing the platform font baseline.

Untouched legacy pages are not considered violations until their visible region is changed. When a migration starts, use the project's exploration, documented discussion, OpenSpec planning, implementation, verification, and archive workflow.
