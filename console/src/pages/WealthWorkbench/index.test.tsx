import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import {
  MemoryRouter,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import WealthWorkbench from "./index";

const access = vi.hoisted(() => ({ tasks: true }));
beforeEach(() => {
  access.tasks = true;
});

vi.mock("./useVisibilityRefresh", () => ({ useVisibilityRefresh: vi.fn() }));
vi.mock("./store", () => ({
  useWealthStore: (selector: (state: object) => unknown) =>
    selector({
      initialized: true,
      accountId: "rm",
      init: vi.fn(),
    }),
  useCanAccess: () => access.tasks,
}));
vi.mock("./components/Topbar", () => ({
  Topbar: () => <header>工作台头部</header>,
}));
vi.mock("./components/Ticker", () => ({
  Ticker: () => <aside>信息播报</aside>,
}));
vi.mock("./components/Sidebar", () => ({ Sidebar: () => <nav>侧栏</nav> }));
vi.mock("./components/Icon", () => ({
  IconDefs: () => null,
  Icon: () => null,
}));
vi.mock("./components/DialogHost", () => ({ DialogHost: () => null }));
vi.mock("./Board", () => ({ default: () => <div>看板</div> }));
vi.mock("./Create", () => ({ default: () => <div>创建规划</div> }));
vi.mock("./Tasks", () => ({ default: () => <div>任务</div> }));

function Navigation() {
  const navigate = useNavigate();
  const location = useLocation();
  return (
    <>
      <output>
        {location.pathname}
        {location.search}
      </output>
      <button onClick={() => navigate("/wealth/create?other=kept")}>
        切换页面
      </button>
      <button onClick={() => navigate("/wealth/board?hideLogo=false")}>
        显示头部
      </button>
    </>
  );
}

function mount(entry: string) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Navigation />
      <Routes>
        <Route path="/wealth/*" element={<WealthWorkbench />} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(cleanup);

describe("财富工作台嵌入头部", () => {
  it.each(["", "?hideLogo=false", "?hideLogo=1", "?hideLogo=invalid"])(
    "默认显示头部：%s",
    (query) => {
      mount(`/wealth/board${query}`);
      expect(screen.getByRole("banner")).toBeInTheDocument();
      expect(screen.getByRole("navigation")).toBeInTheDocument();
      expect(screen.getByText("信息播报")).toBeInTheDocument();
    },
  );

  it("同时隐藏头部、信息播报与整个侧栏", () => {
    mount("/wealth/board?hideLogo=true");
    expect(screen.queryByRole("banner")).not.toBeInTheDocument();
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
    expect(screen.queryByText("信息播报")).not.toBeInTheDocument();
  });

  it("内部导航延续隐藏参数并保留其他参数，可显式恢复显示", async () => {
    mount("/wealth/board?hideLogo=true");
    fireEvent.click(screen.getByText("切换页面"));
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        "/wealth/create?other=kept&hideLogo=true",
      ),
    );
    expect(screen.queryByRole("banner")).not.toBeInTheDocument();
    expect(screen.queryByText("信息播报")).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("显示头部"));
    expect(screen.getByRole("banner")).toBeInTheDocument();
    expect(screen.getByRole("navigation")).toBeInTheDocument();
    expect(screen.getByText("信息播报")).toBeInTheDocument();
  });

  it("默认入口重定向后仍保留隐藏设置", async () => {
    mount("/wealth?hideLogo=true");
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        "/wealth/board?hideLogo=true",
      ),
    );
    expect(screen.queryByRole("banner")).not.toBeInTheDocument();
  });
  it.each([true, false])("隐藏侧栏不依赖任务权限：%s", (tasks) => {
    access.tasks = tasks;
    mount("/wealth/board?hideLogo=true");
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  });
  it.each(["", "&hideLogo=true"])(
    "内部跳转不额外补回 origin 参数：%s",
    async (query) => {
      mount(`/wealth/board?origin=Y${query}`);
      fireEvent.click(screen.getByText("切换页面"));
      await waitFor(() =>
        expect(screen.getByRole("status")).toHaveTextContent(
          query
            ? "/wealth/create?other=kept&hideLogo=true"
            : "/wealth/create?other=kept",
        ),
      );
      expect(screen.getByRole("status")).not.toHaveTextContent("origin=Y");
    },
  );
});
