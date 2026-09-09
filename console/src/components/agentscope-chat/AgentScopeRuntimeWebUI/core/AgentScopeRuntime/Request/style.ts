import { createGlobalStyle } from "antd-style";

export default createGlobalStyle`
.swe-request-card > .${(p) => p.theme.prefixCls}-bubble-content-wrapper {
  min-width: 0;
  max-width: 100%;

  > .${(p) => p.theme.prefixCls}-space {
    max-width: 100%;
    flex-wrap: wrap;
  }
}

.swe-request-grouped > .${(p) => p.theme.prefixCls}-bubble-content-wrapper {
  box-sizing: border-box;
  width: fit-content;
  align-items: flex-start;
  gap: 8px;
  padding: 9px 16px;
  border-radius: ${(p) => p.theme.borderRadiusLG}px;
  background-color: ${(p) => p.theme.colorPrimaryBg};

  > .${(p) => p.theme.prefixCls}-bubble-content-filled {
    padding: 0;
    background: transparent;
  }
}
`;
