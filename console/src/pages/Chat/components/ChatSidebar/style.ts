import { createGlobalStyle } from "antd-style";
import { DESIGN_TOKENS } from "@/config/designTokens";

export default createGlobalStyle`
/* ─── Wrapper: contains sidebar + collapse toggle ─── */
.chat-sidebar-wrapper {
  position: relative;
  flex-shrink: 0;
  display: flex;
  flex-direction: row;
  width: ${DESIGN_TOKENS.sidebarWidth}px;
  height: 100%;
  overflow: hidden;
  transition: width 200ms cubic-bezier(0.25, 1, 0.5, 1);
}

.chat-sidebar-wrapper--collapsed {
  width: ${DESIGN_TOKENS.toolbarWidth}px;
  overflow: visible;

  .chat-sidebar-collapse-toggle {
    position: absolute;
    top: ${DESIGN_TOKENS.toolbarIconPaddingTop}px;
    left: ${DESIGN_TOKENS.toolbarIconPaddingLeft}px;
    z-index: 1;
    width: ${DESIGN_TOKENS.toolbarIconSize}px;
    height: ${DESIGN_TOKENS.toolbarIconSize}px;
    border-radius: 6px;
  }

  .collapsed-toolbar-icons {
    padding-top: 76px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .chat-sidebar-wrapper {
    transition: none;
  }
}

/* ─── Collapse toggle button ─── */
.chat-sidebar-collapse-toggle {
  flex: none;
  border: none;
  background: transparent;
  color: ${DESIGN_TOKENS.colorTextMuted};
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  transition:
    background-color 0.16s ease,
    color 0.16s ease;

  &--expanded {
    position: absolute;
    top: 6px;
    right: 2px;
    z-index: 1;
    width: 28px;
    height: 28px;
    border-radius: 6px;
  }

  &:hover {
    background-color: rgba(55, 105, 252, 0.08);
    color: ${DESIGN_TOKENS.colorPrimary};
  }

  &:active {
    background-color: rgba(55, 105, 252, 0.14);
  }

  &:focus-visible {
    outline: 2px solid ${DESIGN_TOKENS.colorPrimary};
    outline-offset: 2px;
  }
}

/* ─── Expanded sidebar ─── */
.chat-sidebar {
  position: relative;
  width: ${DESIGN_TOKENS.sidebarWidth}px;
  height: 100%;
  background-color: ${DESIGN_TOKENS.colorBgSidebar};
  display: flex;
  flex-direction: column;
  border-right: 1px solid rgba(0, 0, 0, 0.06);
  overflow: hidden;
}

.chat-sidebar-content {
  flex: 1;
  // overflow-y: auto;
}

.chat-sidebar-content,
.chat-sidebar-content-record-list {
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

.chat-sidebar-content-record-list {
  height: calc(100vh - 139px);
  overflow: auto;
}

.chat-sidebar-content-record-list--without-header {
  height: calc(100vh - 139px + 56px);
}

/* History section */
.chat-sidebar-history {
  padding: 0 20px;

  &-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    height: 21px;
    margin-bottom: 6px;
    cursor: pointer;
  }

  &-title {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 14px;
    font-weight: 500;
    color: ${DESIGN_TOKENS.colorTextPrimary};
  }

  &-toggle {
    width: 10px;
    height: 10px;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: transform 0.2s ease;

    &--collapsed {
      transform: rotate(-90deg);
    }
  }

  &-item {
    padding: 10px 0;
    cursor: pointer;
    border-bottom: 1px solid rgba(0, 0, 0, 0.04);

    &:last-child {
      border-bottom: none;
    }

    &:hover {
      background-color: rgba(0, 0, 0, 0.02);
    }
  }

  &-item-title {
    font-size: 14px;
    line-height: 21px;
    color: ${DESIGN_TOKENS.colorTextSecondary};
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  &-item-time {
    font-size: 12px;
    line-height: 16px;
    color: ${DESIGN_TOKENS.colorTextMuted};
    margin-top: 2px;
  }
}

/* New topic button */
.chat-sidebar-new-topic {
  padding: 35px 20px 12px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.chat-sidebar-new-topic-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  width: 228px;
  height: 34px;
  border-radius: ${DESIGN_TOKENS.radiusButtonPill}px;
  background-color: ${DESIGN_TOKENS.colorPrimary};
  color: #FFFFFF;
  font-size: 14px;
  font-weight: 500;
  border: none;
  cursor: pointer;
  outline: none;
  transition: opacity 0.15s ease;

  &:hover {
    opacity: 0.85;
  }
}

/* Footer toolbar */
.chat-sidebar-footer {
  flex-shrink: 0;
  height: 60px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0 28px;
  border-top: 1px solid rgba(0, 0, 0, 0.06);
  background-color: ${DESIGN_TOKENS.colorBgSidebar};
}

.chat-sidebar-footer-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  color: ${DESIGN_TOKENS.colorTextPrimary};
  cursor: pointer;

  &:hover {
    opacity: 0.7;
  }
}

.chat-sidebar-footer-divider {
  width: 1px;
  height: 19px;
  background-color: #D7D7DD;
  margin: 0 24px;
}

.chat-sidebar-history-list {
  flex: 1;
  min-height: 100px;
  // max-height: 400px;
  // overflow-y: auto;

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

/* Skeleton styles */
.chat-sidebar-history-skeleton-item {
  height: 48px;
  padding: 10px 12px;
  margin-bottom: 4px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.chat-sidebar-history-skeleton-title {
  height: 20px;
  width: 60%;
  background: linear-gradient(90deg, #f0f0f0 25%, #e8e8e8 50%, #f0f0f0 75%);
  background-size: 200% 100%;
  animation: skeleton-shimmer 1.5s infinite;
  border-radius: 4px;
}

.chat-sidebar-history-skeleton-time {
  height: 14px;
  width: 40%;
  background: linear-gradient(90deg, #f0f0f0 25%, #e8e8e8 50%, #f0f0f0 75%);
  background-size: 200% 100%;
  animation: skeleton-shimmer 1.5s infinite;
  border-radius: 4px;
}

@keyframes skeleton-shimmer {
  0% {
    background-position: 200% 0;
  }
  100% {
    background-position: -200% 0;
  }
}

/* Empty state */
.chat-sidebar-history-empty {
  padding: 20px;
  text-align: center;
  color: ${DESIGN_TOKENS.colorTextMuted};
  font-size: 14px;
}

.chat-sidebar-guide-preview {
  .swe-image-preview-img-wrapper {
    align-items: flex-start;
    padding-top: 32px;
  }

  .swe-image-preview-img {
    max-width: none;
    max-height: none;
  }
}
`;
