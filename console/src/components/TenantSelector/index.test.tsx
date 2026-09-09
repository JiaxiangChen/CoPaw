import React from "react";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  cleanup,
} from "@testing-library/react";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { TenantSelector } from "./index";
import type { TenantSelectorProps } from "./types";

// Mock 数据
const mockTenantList = [
  { tenant_id: "user001", tenant_name: "用户一", bbk_id: "org1" },
  { tenant_id: "user002", tenant_name: "用户二", bbk_id: "org1" },
  { tenant_id: "user003", tenant_name: "用户三", bbk_id: "org2" },
  { tenant_id: "user004", tenant_name: "用户四", bbk_id: "org2" },
  { tenant_id: "current_user", tenant_name: "当前用户", bbk_id: "org1" },
];

// 翻译键映射
const translations: Record<string, string> = {
  "tenantSelector.targetMode": "分发目标",
  "tenantSelector.byOrganization": "按机构",
  "tenantSelector.byUser": "按用户",
  "tenantSelector.selectOrganization": "选择机构",
  "tenantSelector.selectOrganizationPlaceholder": "请选择机构",
  "tenantSelector.organizationSelectionHint":
    "已选择 {{count}} 个机构，涉及 {{userCount}} 个用户",
  "tenantSelector.userCount": "{{count}} 人",
  "tenantSelector.selectUsers": "选择用户",
  "tenantSelector.selectAll": "全选",
  "tenantSelector.clearAll": "清空",
  "tenantSelector.manualInput": "手动输入用户",
  "tenantSelector.manualInputHint":
    "输入额外的用户ID，多个用户用空格或逗号分隔",
  "tenantSelector.manualInputPlaceholder": "例如：user001 user002 user003",
  "tenantSelector.selectedCount": "已选 {{count}} 个：",
  "tenantSelector.extraInput": "输入额外租户ID",
  "tenantSelector.extraInputHint":
    "输入不在列表中的租户ID，多个ID用空格或逗号分隔",
  "tenantSelector.extraInputPlaceholder":
    "例如：external_user001 external_user002",
  "tenantSelector.filterPlaceholder": "搜索租户名称或ID",
  "tenantSelector.filterHint": "找到 {{count}} 个（共 {{total}} 个）",
  "tenantSelector.noMatchHint": "未找到匹配的租户",
  "tenantSelector.noSourceId": "无法加载租户列表",
  "tenantSelector.noSourceIdDescription":
    "未获取到有效的来源标识，请刷新页面重试",
  "tenantSelector.loadError": "加载租户列表失败",
};

const mocks = vi.hoisted(() => ({
  fetchTenantsBySource: vi.fn(),
  useIframeStore: vi.fn(),
}));

vi.mock("@/api/modules/userInfo", () => ({
  fetchTenantsBySource: mocks.fetchTenantsBySource,
}));

vi.mock("@/stores/iframeStore", () => ({
  useIframeStore: mocks.useIframeStore,
}));

vi.mock("@/constants/bbk", () => ({
  BBK_ID_MAP: [
    { label: "机构一", value: "org1" },
    { label: "机构二", value: "org2" },
  ],
  BBK_ID_TO_NAME_MAP: {
    org1: "机构一",
    org2: "机构二",
  },
}));

vi.mock("@/constants/identity", () => ({
  DEFAULT_SOURCE_ID: "default",
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, params?: Record<string, number | string>) => {
      let text = translations[key] || key;
      if (params) {
        Object.entries(params).forEach(([k, v]) => {
          text = text.replace(`{{${k}}}`, String(v));
        });
      }
      return text;
    },
  }),
}));

// 辅助函数：获取卡片按钮（排除标签）
function getCardByText(text: string): HTMLElement | null {
  const elements = screen.getAllByText(text);
  for (const el of elements) {
    const card = el.closest("button");
    if (card && card.className.includes("userCard")) {
      return card;
    }
  }
  return null;
}

// 辅助函数：获取标签元素
function getTagByText(text: string): HTMLElement | null {
  const elements = screen.getAllByText(text);
  for (const el of elements) {
    const tag = el.closest(".ant-tag");
    if (tag) {
      return tag as HTMLElement;
    }
  }
  return null;
}

describe("TenantSelector", () => {
  const defaultProps: TenantSelectorProps = {
    selectedTenantIds: [],
    onChange: vi.fn(),
  };

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.useIframeStore.mockReturnValue("test-source-id");
    mocks.fetchTenantsBySource.mockResolvedValue(mockTenantList);
  });

  afterEach(() => {
    cleanup();
  });

  describe("Rendering", () => {
    it("keeps allowed holder options that are absent from the tenant directory", async () => {
      render(
        <TenantSelector
          {...defaultProps}
          allowedTenantIds={["orphan"]}
          additionalTenantOptions={[
            { tenant_id: "orphan", tenant_name: null, bbk_id: null },
          ]}
        />,
      );

      await waitFor(() => {
        expect(screen.getByText("按用户")).toBeInTheDocument();
      });
      fireEvent.click(screen.getByText("按用户"));

      await waitFor(() => {
        expect(getCardByText("orphan")).toBeTruthy();
      });
    });

    it("keeps additional holder options when the tenant directory is unavailable", async () => {
      mocks.fetchTenantsBySource.mockRejectedValueOnce(
        new Error("tenant directory unavailable"),
      );
      render(
        <TenantSelector
          {...defaultProps}
          allowedTenantIds={["orphan"]}
          additionalTenantOptions={[
            { tenant_id: "orphan", tenant_name: null, bbk_id: null },
          ]}
        />,
      );

      await waitFor(() => {
        expect(screen.getByText("按用户")).toBeInTheDocument();
      });
      fireEvent.click(screen.getByText("按用户"));

      await waitFor(() => {
        expect(getCardByText("orphan")).toBeTruthy();
      });
    });

    it("renders target mode selector", async () => {
      render(<TenantSelector {...defaultProps} />);

      await waitFor(() => {
        expect(screen.getByText("分发目标")).toBeInTheDocument();
      });
      expect(screen.getByText("按机构")).toBeInTheDocument();
      expect(screen.getByText("按用户")).toBeInTheDocument();
    });

    it("shows organization selector in bbk_id mode by default", async () => {
      render(<TenantSelector {...defaultProps} />);

      await waitFor(() => {
        const elements = screen.getAllByText("选择机构");
        expect(elements.length).toBeGreaterThan(0);
      });
    });

    it("switches to user selection in user_id mode", async () => {
      render(<TenantSelector {...defaultProps} />);

      await waitFor(() => {
        expect(screen.getByText("按用户")).toBeInTheDocument();
      });
      fireEvent.click(screen.getByText("按用户"));

      await waitFor(() => {
        expect(screen.getByText("选择用户")).toBeInTheDocument();
      });
    });
  });

  describe("Source ID handling", () => {
    it("uses DEFAULT_SOURCE_ID when useIframeStore returns null", async () => {
      mocks.useIframeStore.mockReturnValue(null);
      render(<TenantSelector {...defaultProps} />);

      // 应该调用 fetchTenantsBySource，使用 fallback sourceId
      await waitFor(() => {
        expect(mocks.fetchTenantsBySource).toHaveBeenCalledWith("default");
      });
    });
  });

  describe("Error handling", () => {
    it("shows error alert when loading fails", async () => {
      mocks.fetchTenantsBySource.mockRejectedValue(new Error("网络错误"));
      render(<TenantSelector {...defaultProps} />);

      await waitFor(() => {
        expect(screen.getByText("加载租户列表失败")).toBeInTheDocument();
        expect(screen.getByText("网络错误")).toBeInTheDocument();
      });
    });
  });

  describe("User mode - Card selection", () => {
    it("clicking card selects it without affecting textarea", async () => {
      const onChange = vi.fn();
      render(<TenantSelector {...defaultProps} onChange={onChange} />);

      await waitFor(() => {
        expect(screen.getByText("按用户")).toBeInTheDocument();
      });
      fireEvent.click(screen.getByText("按用户"));

      await waitFor(() => {
        expect(getCardByText("用户一 (user001)")).toBeTruthy();
      });

      // 点击卡片选中
      const card = getCardByText("用户一 (user001)");
      if (card) fireEvent.click(card);

      await waitFor(() => {
        // onChange 应该被调用
        expect(onChange).toHaveBeenCalled();
        const lastCall = onChange.mock.calls[onChange.mock.calls.length - 1];
        expect(lastCall[0]).toContain("user001");

        // 文本框应该保持为空（点击不影响文本框）
        const textarea = screen.getByPlaceholderText(
          /例如：external/,
        ) as HTMLTextAreaElement;
        expect(textarea.value).toBe("");
      });
    });

    it("clicking card again deselects it", async () => {
      const onChange = vi.fn();
      render(<TenantSelector {...defaultProps} onChange={onChange} />);

      await waitFor(() => {
        expect(screen.getByText("按用户")).toBeInTheDocument();
      });
      fireEvent.click(screen.getByText("按用户"));

      await waitFor(() => {
        expect(getCardByText("用户一 (user001)")).toBeTruthy();
      });

      // 点击选中
      const card = getCardByText("用户一 (user001)");
      if (card) fireEvent.click(card);

      await waitFor(() => {
        expect(onChange).toHaveBeenCalled();
        // 验证卡片已选中
        expect(card?.className).toMatch(/Selected/);
      });

      // 再次点击取消选中
      vi.clearAllMocks(); // 清除之前的调用
      const cardAgain = getCardByText("用户一 (user001)");
      if (cardAgain) fireEvent.click(cardAgain);

      await waitFor(() => {
        // 验证卡片取消选中
        expect(cardAgain?.className).not.toMatch(/Selected/);
      });
    });

    it("select all selects all cards", async () => {
      const onChange = vi.fn();
      render(<TenantSelector {...defaultProps} onChange={onChange} />);

      await waitFor(() => {
        expect(screen.getByText("按用户")).toBeInTheDocument();
      });
      fireEvent.click(screen.getByText("按用户"));

      await waitFor(() => {
        expect(screen.getByText(/全\s*选/)).toBeInTheDocument();
      });

      fireEvent.click(screen.getByText(/全\s*选/));

      await waitFor(() => {
        expect(onChange).toHaveBeenCalled();
        const lastCall = onChange.mock.calls[onChange.mock.calls.length - 1];
        // 5 个用户都应该被选中
        expect(lastCall[0].length).toBe(5);
      });
    });

    it("clear all clears both cards and textarea", async () => {
      const onChange = vi.fn();
      render(<TenantSelector {...defaultProps} onChange={onChange} />);

      await waitFor(() => {
        expect(screen.getByText("按用户")).toBeInTheDocument();
      });
      fireEvent.click(screen.getByText("按用户"));

      // 先选中一些
      await waitFor(() => {
        expect(getCardByText("用户一 (user001)")).toBeTruthy();
      });
      const card = getCardByText("用户一 (user001)");
      if (card) fireEvent.click(card);

      await waitFor(() => {
        // 验证卡片已选中
        expect(card?.className).toMatch(/Selected/);
      });

      // 点击清空
      fireEvent.click(screen.getByText(/清\s*空/));

      await waitFor(() => {
        // 验证卡片取消选中
        const cardAfter = getCardByText("用户一 (user001)");
        expect(cardAfter?.className).not.toMatch(/Selected/);

        // 验证文本框为空
        const textarea = screen.getByPlaceholderText(
          /例如：external/,
        ) as HTMLTextAreaElement;
        expect(textarea.value).toBe("");
      });
    });
  });

  describe("User mode - Filter functionality", () => {
    it("shows filter input in user mode", async () => {
      render(<TenantSelector {...defaultProps} />);

      await waitFor(() => {
        expect(screen.getByText("按用户")).toBeInTheDocument();
      });
      fireEvent.click(screen.getByText("按用户"));

      await waitFor(() => {
        expect(
          screen.getByPlaceholderText("搜索租户名称或ID"),
        ).toBeInTheDocument();
      });
    });

    it("filters cards by name", async () => {
      render(<TenantSelector {...defaultProps} />);

      await waitFor(() => {
        expect(screen.getByText("按用户")).toBeInTheDocument();
      });
      fireEvent.click(screen.getByText("按用户"));

      // 初始显示所有卡片
      await waitFor(() => {
        expect(getCardByText("用户一 (user001)")).toBeTruthy();
        expect(getCardByText("用户三 (user003)")).toBeTruthy();
      });

      // 输入筛选关键字
      const filterInput = screen.getByPlaceholderText("搜索租户名称或ID");
      fireEvent.change(filterInput, { target: { value: "用户一" } });

      await waitFor(() => {
        // 用户一应该显示
        expect(getCardByText("用户一 (user001)")).toBeTruthy();
        // 用户三不应该显示
        expect(screen.queryByText("用户三 (user003)")).not.toBeInTheDocument();
      });
    });

    it("filters cards by ID", async () => {
      render(<TenantSelector {...defaultProps} />);

      await waitFor(() => {
        expect(screen.getByText("按用户")).toBeInTheDocument();
      });
      fireEvent.click(screen.getByText("按用户"));

      await waitFor(() => {
        expect(
          screen.getByPlaceholderText("搜索租户名称或ID"),
        ).toBeInTheDocument();
      });

      // 输入 ID 筛选
      const filterInput = screen.getByPlaceholderText("搜索租户名称或ID");
      fireEvent.change(filterInput, { target: { value: "user001" } });

      await waitFor(() => {
        expect(getCardByText("用户一 (user001)")).toBeTruthy();
        expect(screen.queryByText("用户三 (user003)")).not.toBeInTheDocument();
      });
    });

    it("shows no match hint when filter has no results", async () => {
      render(<TenantSelector {...defaultProps} />);

      await waitFor(() => {
        expect(screen.getByText("按用户")).toBeInTheDocument();
      });
      fireEvent.click(screen.getByText("按用户"));

      await waitFor(() => {
        expect(
          screen.getByPlaceholderText("搜索租户名称或ID"),
        ).toBeInTheDocument();
      });

      // 输入不存在的关键字
      const filterInput = screen.getByPlaceholderText("搜索租户名称或ID");
      fireEvent.change(filterInput, { target: { value: "不存在的租户" } });

      await waitFor(() => {
        expect(screen.getByText("未找到匹配的租户")).toBeInTheDocument();
      });
    });

    it("clearing filter shows all cards", async () => {
      render(<TenantSelector {...defaultProps} />);

      await waitFor(() => {
        expect(screen.getByText("按用户")).toBeInTheDocument();
      });
      fireEvent.click(screen.getByText("按用户"));

      await waitFor(() => {
        expect(
          screen.getByPlaceholderText("搜索租户名称或ID"),
        ).toBeInTheDocument();
      });

      // 先筛选
      const filterInput = screen.getByPlaceholderText("搜索租户名称或ID");
      fireEvent.change(filterInput, { target: { value: "user001" } });

      await waitFor(() => {
        expect(screen.queryByText("用户三 (user003)")).not.toBeInTheDocument();
      });

      // 清空筛选
      fireEvent.change(filterInput, { target: { value: "" } });

      await waitFor(() => {
        expect(getCardByText("用户一 (user001)")).toBeTruthy();
        expect(getCardByText("用户三 (user003)")).toBeTruthy();
      });
    });

    it("select all only selects filtered cards", async () => {
      const onChange = vi.fn();
      render(<TenantSelector {...defaultProps} onChange={onChange} />);

      await waitFor(() => {
        expect(screen.getByText("按用户")).toBeInTheDocument();
      });
      fireEvent.click(screen.getByText("按用户"));

      await waitFor(() => {
        expect(
          screen.getByPlaceholderText("搜索租户名称或ID"),
        ).toBeInTheDocument();
      });

      // 筛选
      const filterInput = screen.getByPlaceholderText("搜索租户名称或ID");
      fireEvent.change(filterInput, { target: { value: "user00" } }); // 匹配 user001-user004

      await waitFor(() => {
        expect(screen.getByText(/找到 4 个/)).toBeInTheDocument();
      });

      // 全选
      fireEvent.click(screen.getByText(/全\s*选/));

      await waitFor(() => {
        const lastCall = onChange.mock.calls[onChange.mock.calls.length - 1];
        // 只选中筛选后的 4 个
        expect(lastCall[0].length).toBe(4);
      });
    });
  });

  describe("User mode - Extra ID input", () => {
    it("entering extra IDs adds them to result", async () => {
      const onChange = vi.fn();
      render(<TenantSelector {...defaultProps} onChange={onChange} />);

      await waitFor(() => {
        expect(screen.getByText("按用户")).toBeInTheDocument();
      });
      fireEvent.click(screen.getByText("按用户"));

      await waitFor(() => {
        expect(
          screen.getByPlaceholderText(/例如：external/),
        ).toBeInTheDocument();
      });

      // 输入额外ID（不在列表中的）
      const textarea = screen.getByPlaceholderText(/例如：external/);
      fireEvent.change(textarea, {
        target: { value: "extra_user001 extra_user002" },
      });

      await waitFor(() => {
        expect(onChange).toHaveBeenCalled();
        const lastCall = onChange.mock.calls[onChange.mock.calls.length - 1];
        expect(lastCall[0]).toContain("extra_user001");
        expect(lastCall[0]).toContain("extra_user002");
      });
    });

    it("entering IDs in list selects matching cards", async () => {
      const onChange = vi.fn();
      render(<TenantSelector {...defaultProps} onChange={onChange} />);

      await waitFor(() => {
        expect(screen.getByText("按用户")).toBeInTheDocument();
      });
      fireEvent.click(screen.getByText("按用户"));

      await waitFor(() => {
        expect(
          screen.getByPlaceholderText(/例如：external/),
        ).toBeInTheDocument();
      });

      // 输入列表中存在的ID
      const textarea = screen.getByPlaceholderText(/例如：external/);
      fireEvent.change(textarea, { target: { value: "user001" } });

      // 列表中存在的 ID 会同步为卡片选中态
      await waitFor(() => {
        const card = getCardByText("用户一 (user001)");
        expect(card?.className).toMatch(/Selected/);
      });
    });

    it("final result = card selected + extra IDs", async () => {
      const onChange = vi.fn();
      render(<TenantSelector {...defaultProps} onChange={onChange} />);

      await waitFor(() => {
        expect(screen.getByText("按用户")).toBeInTheDocument();
      });
      fireEvent.click(screen.getByText("按用户"));

      // 选中卡片
      await waitFor(() => {
        expect(getCardByText("用户一 (user001)")).toBeTruthy();
      });
      const card = getCardByText("用户一 (user001)");
      if (card) fireEvent.click(card);

      // 输入额外ID
      await waitFor(() => {
        expect(
          screen.getByPlaceholderText(/例如：external/),
        ).toBeInTheDocument();
      });
      const textarea = screen.getByPlaceholderText(/例如：external/);
      fireEvent.change(textarea, { target: { value: "extra_user" } });

      await waitFor(() => {
        const lastCall = onChange.mock.calls[onChange.mock.calls.length - 1];
        // 两者都应该在结果中
        expect(lastCall[0]).toContain("user001");
        expect(lastCall[0]).toContain("extra_user");
      });
    });
  });

  describe("Selected tags display", () => {
    it("shows selected tags for card selections and extra IDs", async () => {
      const onChange = vi.fn();
      render(<TenantSelector {...defaultProps} onChange={onChange} />);

      await waitFor(() => {
        expect(screen.getByText("按用户")).toBeInTheDocument();
      });
      fireEvent.click(screen.getByText("按用户"));

      // 选中卡片
      await waitFor(() => {
        expect(getCardByText("用户一 (user001)")).toBeTruthy();
      });
      const card = getCardByText("用户一 (user001)");
      if (card) fireEvent.click(card);

      // 输入额外ID
      await waitFor(() => {
        expect(
          screen.getByPlaceholderText(/例如：external/),
        ).toBeInTheDocument();
      });
      const textarea = screen.getByPlaceholderText(/例如：external/);
      fireEvent.change(textarea, { target: { value: "extra_user" } });

      await waitFor(() => {
        expect(screen.getByText(/已选 2 个/)).toBeInTheDocument();
        expect(getTagByText("用户一 (user001)")).toBeTruthy();
        expect(getTagByText("extra_user")).toBeTruthy();
      });
    });

    it("clicking tag close removes from selection", async () => {
      const onChange = vi.fn();
      render(<TenantSelector {...defaultProps} onChange={onChange} />);

      await waitFor(() => {
        expect(screen.getByText("按用户")).toBeInTheDocument();
      });
      fireEvent.click(screen.getByText("按用户"));

      // 选中卡片
      await waitFor(() => {
        expect(getCardByText("用户一 (user001)")).toBeTruthy();
      });
      const card = getCardByText("用户一 (user001)");
      if (card) fireEvent.click(card);

      await waitFor(() => {
        expect(screen.getByText(/已选 1 个/)).toBeInTheDocument();
      });

      // 点击标签关闭按钮
      const tag = getTagByText("用户一 (user001)");
      if (tag) {
        const closeBtn = tag.querySelector(".ant-tag-close-icon");
        if (closeBtn) fireEvent.click(closeBtn);
      }

      await waitFor(() => {
        expect(screen.queryByText(/已选 1 个/)).not.toBeInTheDocument();
        // 卡片也应该取消选中
        const cardAfter = getCardByText("用户一 (user001)");
        expect(cardAfter?.className).not.toMatch(/Selected/);
      });
    });

    it("closing extra ID tag removes from textarea", async () => {
      const onChange = vi.fn();
      render(<TenantSelector {...defaultProps} onChange={onChange} />);

      await waitFor(() => {
        expect(screen.getByText("按用户")).toBeInTheDocument();
      });
      fireEvent.click(screen.getByText("按用户"));

      await waitFor(() => {
        expect(
          screen.getByPlaceholderText(/例如：external/),
        ).toBeInTheDocument();
      });
      const textarea = screen.getByPlaceholderText(/例如：external/);
      fireEvent.change(textarea, { target: { value: "extra_user" } });

      await waitFor(() => {
        expect(getTagByText("extra_user")).toBeTruthy();
      });

      // 点击标签关闭按钮
      const tag = getTagByText("extra_user");
      if (tag) {
        const closeBtn = tag.querySelector(".ant-tag-close-icon");
        if (closeBtn) fireEvent.click(closeBtn);
      }

      await waitFor(() => {
        const textareaAfter = screen.getByPlaceholderText(
          /例如：external/,
        ) as HTMLTextAreaElement;
        expect(textareaAfter.value).toBe("");
      });
    });
  });

  describe("External state synchronization", () => {
    it("syncs external IDs to card selection and extra textarea", async () => {
      render(
        <TenantSelector
          {...defaultProps}
          selectedTenantIds={["user001", "extra_user"]}
        />,
      );

      await waitFor(() => {
        expect(mocks.fetchTenantsBySource).toHaveBeenCalled();
      });

      fireEvent.click(screen.getByText("按用户"));

      await waitFor(() => {
        // user001 应该选中卡片
        const card = getCardByText("用户一 (user001)");
        expect(card?.className).toMatch(/Selected/);

        // extra_user 应该在文本框中
        const textarea = screen.getByPlaceholderText(
          /例如：external/,
        ) as HTMLTextAreaElement;
        expect(textarea.value).toContain("extra_user");
      });
    });
  });

  describe("Mode switching", () => {
    it("clears selections when switching modes", async () => {
      const onChange = vi.fn();
      render(<TenantSelector {...defaultProps} onChange={onChange} />);

      await waitFor(() => {
        expect(screen.getByText("按用户")).toBeInTheDocument();
      });
      fireEvent.click(screen.getByText("按用户"));

      // 选中一些
      await waitFor(() => {
        expect(getCardByText("用户一 (user001)")).toBeTruthy();
      });
      const card = getCardByText("用户一 (user001)");
      if (card) fireEvent.click(card);

      await waitFor(() => {
        expect(onChange).toHaveBeenCalled();
      });

      // 切换到机构模式
      fireEvent.click(screen.getByText("按机构"));

      await waitFor(() => {
        const elements = screen.getAllByText("选择机构");
        expect(elements.length).toBeGreaterThan(0);
      });

      // 再切回用户模式，选择应该被清空
      fireEvent.click(screen.getByText("按用户"));

      await waitFor(() => {
        const cardAfter = getCardByText("用户一 (user001)");
        expect(cardAfter?.className).not.toMatch(/Selected/);
      });
    });
  });
});
