import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConfigProvider } from "antd";
import { useIframeStore } from "../../stores/iframeStore";
import WealthWorkbench from "./index";
import { useWealthStore } from "./store";
import { useWealthOperations } from "./operations";
import * as api from "./api";

vi.mock("./api", async (original) => ({
  ...(await original<typeof import("./api")>()),
  fetchBootstrap: vi.fn(async () => ({
    plans: [],
    draft: { name: "", items: [] },
    savedAt: "",
  })),
}));
vi.mock("./Board", () => ({ default: () => <div>规划看板内容</div> }));
vi.mock("./Create", () => ({ default: () => <div>创建规划内容</div> }));
vi.mock("./Tasks", () => ({ default: () => <div>客户经理任务内容</div> }));
vi.mock("./useVisibilityRefresh", () => ({ useVisibilityRefresh: vi.fn() }));
vi.mock("./components/DialogHost", () => ({
  DialogHost: () => {
    const dialog = useWealthStore((state) => state.dialog);
    return dialog ? (
      <div role="dialog">
        {dialog.body}
        {dialog.buttons.map((button) => (
          <button
            key={button.label}
            onClick={() => {
              useWealthStore.getState().closeDialog();
              button.onClick?.();
            }}
          >
            {button.label}
          </button>
        ))}
      </div>
    ) : null;
  },
}));

beforeEach(() => {
  window.__env__ = { operationsUsersByBranch: { V00001: ["80008888"] } };
  useWealthOperations.setState(useWealthOperations.getInitialState(), true);
  useWealthStore.setState(useWealthStore.getInitialState(), true);
  useIframeStore.getState().clearContext();
  useIframeStore
    .getState()
    .setContext({ userId: "80008888", bbk: "V00001", positionId: "RB0101" });
});
afterEach(cleanup);

function mount(entry = "/wealth/board") {
  return render(
    <ConfigProvider prefixCls="swe">
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/wealth/*" element={<WealthWorkbench />} />
        </Routes>
      </MemoryRouter>
    </ConfigProvider>,
  );
}

describe("运维入口和生命周期", () => {
  it.each(["other-user", "80008888"])(
    "入口同时校验用户与分行：%s",
    async (userId) => {
      useIframeStore.getState().setContext({ userId, bbk: "110" });
      mount();
      await screen.findByText("规划看板内容");
      expect(
        screen.queryByRole("switch", { name: "运维模式" }),
      ).not.toBeInTheDocument();
    },
  );

  it("开关默认关闭，开启显示待选分行提示和可搜索选择器", async () => {
    mount();
    const toggle = await screen.findByRole("switch", { name: "运维模式" });
    expect(toggle).toHaveAttribute("aria-checked", "false");
    fireEvent.click(toggle);
    await screen.findByText("运维模式已开启");
    expect(screen.queryByText("规划看板内容")).not.toBeInTheDocument();
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "运维分行" }));
    expect(await screen.findByText("北京分行")).toBeInTheDocument();
    expect(screen.queryByText("总行")).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("北京分行"));
    await screen.findByText("规划看板内容");
    expect(useWealthStore.getState().accountId).toBe("middle");
  });

  it("有草稿时可取消切换，写入期间开关禁用", async () => {
    mount();
    const toggle = await screen.findByRole("switch", { name: "运维模式" });
    useWealthStore.setState({ draft: { name: "我的草稿", items: [] } });
    fireEvent.click(toggle);
    expect(screen.getByRole("dialog")).toHaveTextContent(
      "清空当前草稿和分发选择",
    );
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(useWealthStore.getState().draft.name).toBe("我的草稿");
    useWealthStore.setState({ acting: true });
    await waitFor(() => expect(toggle).toBeDisabled());
  });

  it("分行数据加载完成不会把用户从创建页拉回看板", async () => {
    mount();
    fireEvent.click(await screen.findByRole("switch", { name: "运维模式" }));
    let finish!: (
      value: Awaited<ReturnType<typeof api.fetchBootstrap>>,
    ) => void;
    vi.mocked(api.fetchBootstrap).mockReturnValueOnce(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "运维分行" }));
    fireEvent.click(await screen.findByText("北京分行"));
    fireEvent.click(screen.getByRole("button", { name: "创建计划" }));
    await screen.findByText("创建规划内容");
    useWealthStore.getState().setDraftName("刚输入的新草稿");
    finish({ plans: [], draft: { name: "", items: [] }, savedAt: "" });
    await waitFor(() =>
      expect(useWealthStore.getState().plansLoaded).toBe(true),
    );
    expect(screen.getByText("创建规划内容")).toBeInTheDocument();
    expect(useWealthStore.getState().draft.name).toBe("刚输入的新草稿");
  });

  it("白名单的未识别岗位保持普通模式拦截，但可主动进入运维模式", async () => {
    useIframeStore.getState().setContext({ positionId: null });
    mount();
    await screen.findByText("暂无访问权限");
    fireEvent.click(screen.getByRole("switch", { name: "运维模式" }));
    await screen.findByText("运维模式已开启");
    expect(useWealthStore.getState().accountId).toBe("middle");
  });

  it("hideLogo=true 不提供入口，动态隐藏时退出运维恢复原岗位", async () => {
    const page = mount("/wealth/board?hideLogo=true");
    await screen.findByText("规划看板内容");
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
    page.unmount();
    mount();
    fireEvent.click(await screen.findByRole("switch", { name: "运维模式" }));
    await screen.findByText("运维模式已开启");
    useIframeStore.getState().setContext({ hideLogo: true });
    await waitFor(() =>
      expect(useWealthOperations.getState().enabled).toBe(false),
    );
    expect(useWealthStore.getState().accountId).toBe("rm");
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
  });

  it("退出页面清理模式，身份变化恢复真实身份", async () => {
    const page = mount();
    fireEvent.click(await screen.findByRole("switch", { name: "运维模式" }));
    await screen.findByText("运维模式已开启");
    useIframeStore
      .getState()
      .setContext({ userId: "other-user", positionId: "RB1101" });
    await waitFor(() =>
      expect(useWealthOperations.getState().enabled).toBe(false),
    );
    expect(useWealthStore.getState().accountId).toBe("president");
    page.unmount();
    useIframeStore.getState().setContext({ userId: "80008888" });
    const secondPage = mount();
    fireEvent.click(await screen.findByRole("switch", { name: "运维模式" }));
    await screen.findByText("运维模式已开启");
    secondPage.unmount();
    expect(useWealthOperations.getState().enabled).toBe(false);
  });
});
