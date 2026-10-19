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
import { afterEach, describe, expect, it, vi } from "vitest";
import WealthWorkbench from "./index";

vi.mock("./useVisibilityRefresh", () => ({ useVisibilityRefresh: vi.fn() }));
vi.mock("./store", () => ({
  useWealthStore: (selector: (state: object) => unknown) =>
    selector({
      initialized: true,
      accountId: "rm",
      init: vi.fn(),
    }),
  useCanAccess: () => true,
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
    },
  );

  it("隐藏整个头部并保留侧栏与播报", () => {
    mount("/wealth/board?hideLogo=true");
    expect(screen.queryByRole("banner")).not.toBeInTheDocument();
    expect(screen.getByRole("navigation")).toBeInTheDocument();
    expect(screen.getByText("信息播报")).toBeInTheDocument();
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
    fireEvent.click(screen.getByText("显示头部"));
    expect(screen.getByRole("banner")).toBeInTheDocument();
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
});
