import { createGlobalStyle } from "antd-style";
import { DESIGN_TOKENS } from "@/config/designTokens";

export default createGlobalStyle`
.expandable-panel {
  position: absolute;
  top: 0;
  left: ${DESIGN_TOKENS.toolbarWidth + DESIGN_TOKENS.panelGap}px;
  width: ${DESIGN_TOKENS.sidebarWidth}px;
  max-height: 100%;
  background-color: ${DESIGN_TOKENS.colorBgCard};
  border-radius: ${DESIGN_TOKENS.radiusPanel}px;
  box-shadow: 0 4px 24px rgba(0, 0, 0, 0.08);
  z-index: 100;
  overflow-y: auto;
  overflow-x: hidden;
  display: flex;
  flex-direction: column;
  padding: ${DESIGN_TOKENS.panelPadding}px;
  animation: panelSlideIn 0.15s ease-out;
}

.expandable-panel,
.expandable-panel-content {
  &::-webkit-scrollbar {
    width: 4px;
  }

  &::-webkit-scrollbar-track {
    background: transparent;
  }

  &::-webkit-scrollbar-thumb {
    background: rgba(0, 0, 0, 0.12);
    border-radius: 4px;
  }

  &::-webkit-scrollbar-thumb:hover {
    background: rgba(0, 0, 0, 0.28);
  }

  scrollbar-width: thin;
  scrollbar-color: rgba(0, 0, 0, 0.12) transparent;
}

@keyframes panelSlideIn {
  from {
    opacity: 0;
    transform: translateX(-8px);
  }
  to {
    opacity: 1;
    transform: translateX(0);
  }
}

.expandable-panel-header {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-bottom: 12px;
  flex-shrink: 0;
}

.expandable-panel-header-title {
  font-size: 16px;
  font-weight: 600;
  //font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
  color: ${DESIGN_TOKENS.colorTextPrimary};
  line-height: 22px;
  margin-left: 4px;
}

.expandable-panel-content {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
}

.expandable-panel-task-empty {
  min-height: 116px;
  padding: 24px 16px;
  border-radius: ${DESIGN_TOKENS.radiusCard}px;
  background: rgba(55, 105, 252, 0.035);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
}

.expandable-panel-task-empty-title {
  color: ${DESIGN_TOKENS.colorTextMuted};
  font-size: 14px;
  font-weight: 500;
  line-height: 20px;
}

.expandable-panel-task-empty-description {
  margin-top: 12px;
  color: ${DESIGN_TOKENS.colorTextMuted};
  font-size: 13px;
  line-height: 20px;
}

/* ─── Task card styles ─── */
.expandable-panel-task-card {
  padding: ${DESIGN_TOKENS.panelTaskCardPadding}px;
  border: 0.5px solid ${DESIGN_TOKENS.colorCardBorder};
  border-radius: ${DESIGN_TOKENS.radiusPanel}px;
  cursor: pointer;
  transition:
    background-color 0.15s ease,
    border-color 0.15s ease,
    box-shadow 0.15s ease;

  &:hover {
    background-color: rgba(0, 0, 0, 0.02);
    border-color: rgba(55, 105, 252, 0.18);
    box-shadow: 0 4px 14px rgba(17, 20, 45, 0.06);
  }

  & + & {
    margin-top: ${DESIGN_TOKENS.panelTaskCardGap}px;
  }

  &--paused {
    box-shadow: inset 0 0 0 1px rgba(0, 0, 0, 0.03);
  }

  &--auto-paused {
    background:
      linear-gradient(90deg, rgba(223, 146, 33, 0.12), rgba(223, 146, 33, 0.04));
  }

  &--running {
    background:
      linear-gradient(90deg, rgba(55, 105, 252, 0.12), rgba(55, 105, 252, 0.04));
    box-shadow: inset 0 0 0 1px rgba(55, 105, 252, 0.12);
  }

  &--selected {
    border-color: rgba(55, 105, 252, 0.32);
    box-shadow: inset 3px 0 0 #3769FC;
  }
}

.expandable-panel-paused-group {
  margin: 0 0 6px;
}

.expandable-panel-paused-toggle {
  width: 100%;
  min-height: 30px;
  padding: 0 4px;
  display: flex;
  align-items: center;
  gap: 6px;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: ${DESIGN_TOKENS.colorTextMuted};
  font-size: 12px;
  line-height: 18px;
  text-align: left;
  cursor: pointer;
  transition: color 0.15s ease;

  &::after {
    content: "";
    height: 1px;
    flex: 1 1 auto;
    margin-left: 4px;
    background: rgba(17, 20, 45, 0.06);
  }

  &:hover {
    color: ${DESIGN_TOKENS.colorTextSecondary};
  }

  &:focus-visible {
    outline: 2px solid rgba(55, 105, 252, 0.32);
    outline-offset: 1px;
  }
}

.expandable-panel-paused-label {
  min-width: 0;
  color: inherit;
  font-weight: 400;
}

.expandable-panel-paused-count {
  flex: 0 0 auto;
  color: inherit;
  font-size: 12px;
  font-weight: 400;
  line-height: 18px;
  font-variant-numeric: tabular-nums;
}

.expandable-panel-paused-chevron {
  width: 10px;
  height: 10px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: ${DESIGN_TOKENS.colorTextMuted};
  transition: transform 0.2s ease;

  &--collapsed {
    transform: rotate(-90deg);
  }
}

.expandable-panel-paused-items {
  display: flex;
  flex-direction: column;
  margin-top: 4px;

  &[hidden] {
    display: none;
  }
}

.expandable-panel-task-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.expandable-panel-task-title {
  font-size: 14px;
  font-weight: 400;
  font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
  color: ${DESIGN_TOKENS.colorTextPrimary};
  line-height: 21px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1;
  min-width: 0;
}

.expandable-panel-task-badge {
  flex-shrink: 0;
  min-width: 14px;
  height: 14px;
  padding: 0 4px;
  border-radius: ${DESIGN_TOKENS.radiusBadge}px;
  background-color: ${DESIGN_TOKENS.colorBadgeRed};
  color: #FFFFFF;
  font-size: ${DESIGN_TOKENS.badgeFontSize}px;
  font-family: "PingFang SC", sans-serif;
  line-height: 14px;
  text-align: center;
}

.expandable-panel-task-trailing {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: flex-end;
  gap: 6px;
}

.expandable-panel-task-action-trigger {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  padding: 0;
  border: none;
  border-radius: 7px;
  background: transparent;
  color: ${DESIGN_TOKENS.colorTextMuted};
  cursor: pointer;
  transition:
    background-color 0.15s ease,
    color 0.15s ease;

  &:hover,
  &--open {
    background: rgba(132, 130, 231, 0.14);
    color: ${DESIGN_TOKENS.colorPrimary};
  }

  &:focus-visible {
    outline: 2px solid rgba(55, 105, 252, 0.32);
    outline-offset: 2px;
  }
}

.expandable-panel-task-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 4px;
  flex: 0 0 auto;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.15s ease;
}

.expandable-panel-task-card:hover .expandable-panel-task-actions,
.expandable-panel-task-card:focus-within .expandable-panel-task-actions {
  opacity: 1;
  pointer-events: auto;
}

.expandable-panel-task-status {
  font-size: 12px;
  line-height: 16px;
  margin-top: 4px;

  &--auto {
    color: #A15C07;
  }

  &--manual {
    color: ${DESIGN_TOKENS.colorTextMuted};
  }

}

.expandable-panel-task-subtitle {
  font-size: 12px;
  font-weight: 400;
  font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
  color: ${DESIGN_TOKENS.colorTextMuted};
  line-height: 16px;
  margin-top: 4px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.expandable-panel-task-next-run {
  font-size: 12px;
  font-weight: 400;
  font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
  color: ${DESIGN_TOKENS.colorTextSecondary};
  line-height: 16px;
  margin-top: 4px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.expandable-panel-task-time {
  margin-right: 8px;
  color: ${DESIGN_TOKENS.colorTextMuted};
}

.dark-mode .expandable-panel-paused-toggle {
  color: rgba(255, 255, 255, 0.46);
}

.dark-mode .expandable-panel-paused-toggle:hover {
  color: rgba(255, 255, 255, 0.70);
}

.dark-mode .expandable-panel-paused-toggle::after {
  background: rgba(255, 255, 255, 0.08);
}

@media (prefers-reduced-motion: reduce) {
  .expandable-panel,
  .expandable-panel-paused-toggle,
  .expandable-panel-paused-chevron {
    animation: none;
    transition: none;
  }
}

/* ─── History row styles ─── */
.expandable-panel-history-item {
  padding: 10px 0;
  cursor: pointer;
  transition: background-color 0.15s ease;

  &:hover {
    background-color: rgba(0, 0, 0, 0.02);
  }

  & + & {
    /* no extra gap */
  }
}

.expandable-panel-history-title {
  font-size: 14px;
  font-weight: 400;
  //font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
  color: ${DESIGN_TOKENS.colorTextSecondary};
  line-height: 22px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.expandable-panel-history-time {
  font-size: 12px;
  font-weight: 400;
  font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
  color: ${DESIGN_TOKENS.colorTextMuted};
  line-height: 16px;
  margin-top: 2px;
}

.expandable-panel-empty {
  padding: 24px 0;
  text-align: center;
  color: ${DESIGN_TOKENS.colorTextMuted};
  font-size: 13px;
}

`;
