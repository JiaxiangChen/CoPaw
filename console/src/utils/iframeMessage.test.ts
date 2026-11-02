import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanupIframeMessageListener,
  fetchAndSetUserName,
  initIframeMessageListener,
  handleUrlOriginParam,
  resetIframeContextForEntry,
  isWealthMessageEntry,
  waitForWealthUserData,
} from "./iframeMessage";
import { useIframeStore } from "../stores/iframeStore";
import { authApi } from "../api/modules/auth";
import { fetchUserInfo } from "../api/modules/userInfo";
import {
  ensureValidToken,
  isExternalTokenEnabled,
} from "../api/externalToken";
import {
  fetchCustomerInfo,
  fetchUserInit,
} from "../api/modules/customerInfo";

vi.mock("../api/modules/userInfo", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../api/modules/userInfo")>();
  return {
    ...actual,
    fetchUserInfo: vi.fn(),
  };
});

vi.mock("../api/externalToken", () => ({
  ensureValidToken: vi.fn(),
  isExternalTokenEnabled: vi.fn(),
}));

vi.mock("../api/modules/customerInfo", () => ({
  fetchCustomerInfo: vi.fn().mockResolvedValue(null),
  fetchUserInit: vi.fn().mockResolvedValue(null),
}));

vi.mock("../api/modules/auth", () => ({
  authApi: {
    sendCronAuth: vi.fn().mockResolvedValue(undefined),
  },
}));

const mockedFetchUserInfo = vi.mocked(fetchUserInfo);
const mockedEnsureValidToken = vi.mocked(ensureValidToken);
const mockedIsExternalTokenEnabled = vi.mocked(isExternalTokenEnabled);
const mockedFetchCustomerInfo = vi.mocked(fetchCustomerInfo);
const mockedFetchUserInit = vi.mocked(fetchUserInit);

const originalWindowTopDescriptor = Object.getOwnPropertyDescriptor(
  window,
  "top",
);

function mockInIframe(): void {
  Object.defineProperty(window, "top", {
    configurable: true,
    value: {},
  });
}

function restoreWindowTop(): void {
  if (originalWindowTopDescriptor) {
    Object.defineProperty(window, "top", originalWindowTopDescriptor);
  }
}

async function dispatchUserDataMessage(data: Record<string, unknown>) {
  mockedFetchUserInfo.mockResolvedValueOnce({
    code: "SUC0000",
    message: "success",
    result: true,
    data: [{ userName: "张三", pathName: "某企业/总行/生产部" }],
  });

  mockInIframe();
  initIframeMessageListener();
  window.dispatchEvent(
    new MessageEvent("message", {
      origin: "https://parent.example",
      data: {
        type: "USER_DATA",
        data: {
          sapId: "80000001",
          ...data,
        },
      },
    }),
  );

  await vi.waitFor(() => {
    expect(useIframeStore.getState().initialized).toBe(true);
  });
}

describe("fetchAndSetUserName", () => {
  it.each([
    ["/wealth/board", "RCZT"],
    ["/wealth/create", "RMASSIST"],
    ["/console/wealth/board", "NEW_CHANNEL"],
  ])(
    "%s 消息入口使用渠道 %s 初始化",
    async (path, source) => {
      window.history.pushState({}, "", path);
      useIframeStore.getState().setContext({
        userId: "old-user",
        positionId: "RB0301",
        source: "RMASSIST",
      });
      useIframeStore.getState().markInitialized();
      mockInIframe();
      initIframeMessageListener();
      expect(useIframeStore.getState().userId).toBeNull();
      expect(useIframeStore.getState().initialized).toBe(false);
      const completed = vi.fn();
      const ready = waitForWealthUserData().then(completed);
      await Promise.resolve();
      expect(completed).not.toHaveBeenCalled();

      const data = {
        type: "USER_DATA",
        data: {
          sapId: "00800001",
          source,
          hideLogo: "true",
          userName: "张三",
          bbkId: "100",
          positionId: "RB0101",
          orgCode: "ORG001",
          subBranchId: "SUB001",
          orgLvl: "4",
          sysId: "SYS",
        },
      };
      // 无关窗口及缺少有效渠道的消息不能解除等待。
      window.dispatchEvent(new MessageEvent("message", { data, source: null }));
      window.dispatchEvent(
        new MessageEvent("message", {
          data: { ...data, data: { ...data.data, source: "" } },
          source: window.parent,
        }),
      );
      expect(useIframeStore.getState().initialized).toBe(false);
      window.dispatchEvent(
        new MessageEvent("message", {
          data,
          source: window.parent,
          origin: "https://parent.example",
        }),
      );
      await ready;
      expect(useIframeStore.getState()).toMatchObject({
        userId: "00800001",
        source,
        hideLogo: true,
        userName: "张三",
        bbk: "100",
        positionId: "RB0101",
        orgCode: "ORG001",
        subBranchId: "SUB001",
        orgLvl: "4",
        sysId: "SYS",
        isOriginY: false,
        initialized: true,
      });
      await expect(waitForWealthUserData()).resolves.toBeUndefined();
      window.dispatchEvent(
        new MessageEvent("message", { data, source: window.parent }),
      );
      expect(completed).toHaveBeenCalledTimes(1);
      expect(mockedFetchUserInfo).not.toHaveBeenCalled();
      expect(mockedFetchUserInit).not.toHaveBeenCalled();
      expect(mockedFetchCustomerInfo).not.toHaveBeenCalled();
      expect(authApi.sendCronAuth).not.toHaveBeenCalled();
      expect(
        JSON.parse(sessionStorage.getItem("swe-iframe-context") || "null")
          .state,
      ).not.toHaveProperty("hideLogo");
    },
  );

  it("origin=Y 财富入口保持 RMASSIST 和 Cookie 身份初始化", async () => {
    mockInIframe();
    window.history.pushState({}, "", "/wealth/board?origin=Y&hideLogo=true");
    document.cookie = "userid=80000002; path=/";
    document.cookie = "positionID=RB0101; path=/";
    initIframeMessageListener();
    expect(isWealthMessageEntry()).toBe(false);
    await handleUrlOriginParam();
    expect(useIframeStore.getState()).toMatchObject({
      source: "RMASSIST",
      userId: "80000002",
      positionId: "RB0101",
      isOriginY: true,
    });
    window.history.pushState({}, "", "/wealth/create");
    expect(isWealthMessageEntry()).toBe(false);
  });

  it("Chat 中已有的 USER_DATA 即使 source 为 RCZT 仍沿用用户查询流程", async () => {
    window.history.pushState({}, "", "/chat");
    await dispatchUserDataMessage({ source: "RCZT", hideLogo: true, positionId: "RB0101" });
    expect(mockedFetchUserInfo).toHaveBeenCalledWith("80000001");
    expect(useIframeStore.getState()).toMatchObject({
      source: "RCZT", userName: "张三", hideLogo: false, positionId: null,
    });
  });

  it("RCZT 消息缺少有效用户时不初始化", () => {
    mockInIframe();
    window.history.pushState({}, "", "/wealth/board");
    initIframeMessageListener();
    for (const sapId of [null, "", " ", 123]) {
      window.dispatchEvent(new MessageEvent("message", {
        source: window.parent,
        data: { type: "USER_DATA", data: { source: "RCZT", sapId } },
      }));
    }
    expect(useIframeStore.getState().initialized).toBe(false);
    expect(useIframeStore.getState().userId).toBeNull();
  });

  it.each([undefined, null, "", " ", 123])("缺少有效渠道时不初始化：%s", async (source) => {
    mockInIframe();
    window.history.pushState({}, "", "/wealth/board");
    initIframeMessageListener();
    window.dispatchEvent(new MessageEvent("message", {
      source: window.parent,
      data: { type: "USER_DATA", data: { source, sapId: "00800001" } },
    }));
    await Promise.resolve();
    expect(useIframeStore.getState().initialized).toBe(false);
    expect(useIframeStore.getState().source).toBeNull();
  });

  beforeEach(() => {
    sessionStorage.clear();
    localStorage.clear();
    window.history.pushState({}, "", "/");
    [
      "userid",
      "sysid",
      "vbbk",
      "vorgcode",
      "subBranchId",
      "vorglvl",
      "positionID",
      "token",
      "username",
    ].forEach((name) => {
      document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
    });
    useIframeStore.getState().clearContext();
    vi.clearAllMocks();
    mockedIsExternalTokenEnabled.mockReturnValue(false);
    mockedEnsureValidToken.mockResolvedValue("token");
  });

  afterEach(() => {
    cleanupIframeMessageListener();
    restoreWindowTop();
    vi.useRealTimers();
  });

  it("在 userId 缺失时不请求用户信息接口", async () => {
    await expect(fetchAndSetUserName()).resolves.toBe(false);

    expect(mockedFetchUserInfo).not.toHaveBeenCalled();
  });

  it("首次空 userId 返回后，后续 userId 到达仍会请求用户信息", async () => {
    await expect(fetchAndSetUserName()).resolves.toBe(false);

    useIframeStore.getState().setContext({ userId: "80000001" });
    mockedFetchUserInfo.mockResolvedValueOnce({
      code: "SUC0000",
      message: "success",
      result: true,
      data: [{ userName: "张三", pathName: "某企业/总行/生产部" }],
    });

    await expect(fetchAndSetUserName()).resolves.toBe(true);

    expect(mockedFetchUserInfo).toHaveBeenCalledTimes(1);
    expect(mockedFetchUserInfo).toHaveBeenCalledWith("80000001");
    expect(useIframeStore.getState().userName).toBe("张三");
  });

  it("同一个 userId 的并发查询只请求一次接口", async () => {
    useIframeStore.getState().setContext({ userId: "80000001" });
    mockedFetchUserInfo.mockResolvedValueOnce({
      code: "SUC0000",
      message: "success",
      result: true,
      data: [{ userName: "张三", pathName: "某企业/总行/生产部" }],
    });

    const [firstResult, secondResult] = await Promise.all([
      fetchAndSetUserName(),
      fetchAndSetUserName(),
    ]);

    expect(firstResult).toBe(true);
    expect(secondResult).toBe(true);
    expect(mockedFetchUserInfo).toHaveBeenCalledTimes(1);
  });

  it("外部 token 开启时先等待 token，再请求用户信息", async () => {
    const callOrder: string[] = [];
    useIframeStore.getState().setContext({ userId: "80000001" });
    mockedIsExternalTokenEnabled.mockReturnValue(true);
    mockedEnsureValidToken.mockImplementation(async () => {
      callOrder.push("token:start");
      await Promise.resolve();
      callOrder.push("token:end");
      return "token";
    });
    mockedFetchUserInfo.mockImplementation(async () => {
      callOrder.push("user-info");
      return {
        code: "SUC0000",
        message: "success",
        result: true,
        data: [{ userName: "张三", pathName: "某企业/总行/生产部" }],
      };
    });

    await expect(fetchAndSetUserName()).resolves.toBe(true);

    expect(callOrder).toEqual(["token:start", "token:end", "user-info"]);
    expect(mockedEnsureValidToken).toHaveBeenCalledTimes(1);
    expect(mockedFetchUserInfo).toHaveBeenCalledWith("80000001");
  });

  it("用户切换后忽略旧 userId 的异步返回结果", async () => {
    useIframeStore.getState().setContext({ userId: "80000001" });
    mockedFetchUserInfo.mockImplementation(async () => {
      useIframeStore.getState().setContext({ userId: "80000002" });
      return {
        code: "SUC0000",
        message: "success",
        result: true,
        data: [{ userName: "张三", pathName: "某企业/总行/生产部" }],
      };
    });

    await expect(fetchAndSetUserName()).resolves.toBe(false);

    expect(useIframeStore.getState().userId).toBe("80000002");
    expect(useIframeStore.getState().userName).toBeNull();
  });

  it("独立访问时清理残留的 iframe 上下文", () => {
    useIframeStore.getState().setContext({
      userId: "80000001",
      userName: "张三",
      source: "RMASSIST",
      bbk: "100",
      authHeaders: [{ headerName: "token", headerValue: "old" }],
    });

    resetIframeContextForEntry();

    expect(useIframeStore.getState().userId).toBeNull();
    expect(useIframeStore.getState().userName).toBeNull();
    expect(useIframeStore.getState().source).toBeNull();
    expect(useIframeStore.getState().authHeaders).toEqual([]);
  });

  it("USER_DATA skipPreviewTracking=true 时存储为跳过 preview 埋点", async () => {
    await dispatchUserDataMessage({ skipPreviewTracking: true });

    expect(useIframeStore.getState().skipPreviewTracking).toBe(true);
  });

  it('USER_DATA skipPreviewTracking="true" 时存储为跳过 preview 埋点', async () => {
    await dispatchUserDataMessage({ skipPreviewTracking: "true" });

    expect(useIframeStore.getState().skipPreviewTracking).toBe(true);
  });

  it("USER_DATA skipPreviewTracking=false 时保持记录 preview 埋点", async () => {
    useIframeStore.getState().setContext({ skipPreviewTracking: true });

    await dispatchUserDataMessage({ skipPreviewTracking: false });

    expect(useIframeStore.getState().skipPreviewTracking).toBe(false);
  });

  it("USER_DATA 未传 skipPreviewTracking 时保持记录 preview 埋点", async () => {
    useIframeStore.getState().setContext({ skipPreviewTracking: true });

    await dispatchUserDataMessage({});

    expect(useIframeStore.getState().skipPreviewTracking).toBe(false);
  });

  it.each([
    ["/chat?origin=Y", "CLAW", "WP"],
    ["/chat?origin=Y&hideLogo=true", "CLAW", "WP"],
    [
      "/wealth/board?origin=Y&hideLogo=true&pageSource=MENU_B&platformSource=WEB",
      "MENU_B",
      "WEB",
    ],
  ])("%s 不继承消息菜单的持久化上下文", async (path, pageSource, platformSource) => {
    await dispatchUserDataMessage({
      clawName: "菜单 A",
      space: "space-a",
      source: "CHANNEL_A",
      manager: true,
      isSuperManager: true,
      hideChat: true,
      skipPreviewTracking: true,
      pageSource: "MENU_A",
      platformSource: "PLATFORM_A",
      auth: [{ headerName: "token", headerValue: "menu-a-token" }],
    });
    cleanupIframeMessageListener();
    await useIframeStore.persist.rehydrate();
    expect(useIframeStore.getState().authHeaders).not.toEqual([]);

    window.history.pushState({}, "", path);
    // 同一用户切换菜单也必须重建上下文，不能只按 userId 判断。
    document.cookie = "userid=80000001; path=/";
    document.cookie = "vbbk=200; path=/";
    resetIframeContextForEntry();
    initIframeMessageListener();
    await handleUrlOriginParam();

    const expected = {
      userId: "80000001",
      userName: null,
      bbk: "200",
      source: "RMASSIST",
      clawName: null,
      space: null,
      authHeaders: [],
      parentOrigin: null,
      manager: false,
      isSuperManager: false,
      hideChat: false,
      skipPreviewTracking: false,
      pageSource,
      platformSource,
    };
    expect(useIframeStore.getState()).toMatchObject({
      ...expected,
      initialized: true,
      isOriginY: true,
      hideMenu: true,
    });
    expect(
      JSON.parse(sessionStorage.getItem("swe-iframe-context") || "null").state,
    ).toMatchObject(expected);
  });

  it("origin=Y 缺少身份 Cookie 时不回退到消息菜单的旧身份", async () => {
    await dispatchUserDataMessage({ source: "CHANNEL_A", manager: true });
    cleanupIframeMessageListener();
    window.history.pushState({}, "", "/chat?origin=Y&hideLogo=true");

    resetIframeContextForEntry();
    initIframeMessageListener();
    await handleUrlOriginParam();

    expect(useIframeStore.getState()).toMatchObject({
      userId: null,
      userName: null,
      source: null,
      authHeaders: [],
      manager: false,
      initialized: false,
      isOriginY: true,
    });
  });

  it.each([
    ["", "MENU_B", "WEB"],
    ["&pageSource=URL_B&platformSource=URL_WEB", "URL_B", "URL_WEB"],
  ])("origin=Y 保留本次加载期间收到的新消息并遵守来源优先级：%s", async (query, pageSource, platformSource) => {
    window.history.pushState({}, "", `/chat?origin=Y&hideLogo=true${query}`);
    document.cookie = "userid=80000001; path=/";
    resetIframeContextForEntry();
    initIframeMessageListener();

    // 模拟注册监听器后、等待外部 token 期间收到当前菜单的 USER_DATA。
    await dispatchUserDataMessage({
      source: "RMASSIST",
      pageSource: "MENU_B",
      platformSource: "WEB",
      auth: [{ headerName: "token", headerValue: "menu-b-token" }],
    });
    await handleUrlOriginParam();

    expect(useIframeStore.getState()).toMatchObject({
      userId: "80000001",
      isOriginY: true,
      pageSource,
      platformSource,
      authHeaders: expect.arrayContaining([
        { headerName: "token", headerValue: "menu-b-token" },
      ]),
    });
  });

  it("origin=Y 切换 userId 时清空旧 userName", async () => {
    useIframeStore.getState().setContext({
      userId: "80000001",
      userName: "张三",
    });
    window.history.pushState({}, "", "/?origin=Y");
    document.cookie = "userid=80000002; path=/";

    await handleUrlOriginParam();

    expect(useIframeStore.getState().isOriginY).toBe(true);
    expect(
      JSON.parse(sessionStorage.getItem("swe-iframe-context") || "null")?.state,
    ).not.toHaveProperty("isOriginY");
    expect(useIframeStore.getState().userId).toBe("80000002");
    expect(useIframeStore.getState().userName).toBeNull();
  });

  it.each(["true", "false", "1", "invalid", null])(
    "hideLogo=%s 时客户信息与授权同步遵守入口设置",
    async (hideLogo) => {
      vi.useFakeTimers();
      const query = hideLogo === null ? "" : `&hideLogo=${hideLogo}`;
      window.history.pushState({}, "", `/wealth/board?origin=Y${query}`);
      document.cookie = "userid=80000002; path=/";

      await handleUrlOriginParam();

      const expectedCalls = hideLogo === "true" ? 0 : 1;
      expect(authApi.sendCronAuth).toHaveBeenCalledTimes(expectedCalls);
      expect(mockedFetchCustomerInfo).toHaveBeenCalledTimes(expectedCalls);
      expect(useIframeStore.getState().initialized).toBe(true);

      // 内部导航丢失查询参数后，跳过初始化的入口也不会出现定时请求。
      window.history.pushState({}, "", "/wealth/create");
      await vi.advanceTimersByTimeAsync(30 * 60 * 1000);

      expect(authApi.sendCronAuth).toHaveBeenCalledTimes(expectedCalls * 2);
      expect(mockedFetchCustomerInfo).toHaveBeenCalledTimes(expectedCalls * 2);
    },
  );

  it("hideLogo=true 时沿用初始上下文和导航并跳过异步初始化", async () => {
    vi.useFakeTimers();
    window.history.pushState(
      {},
      "",
      "/chat?origin=Y&hideLogo=true&sessionId=session-1&taskId=task-1",
    );
    document.cookie = "userid=80000002; path=/";
    document.cookie = "token=cookie-token; path=/";
    document.cookie = "username=%E5%BC%A0%E4%B8%89; path=/";
    document.cookie = "vbbk=100; path=/";
    document.cookie = "vorgcode=ORG001; path=/";
    document.cookie = "positionID=RB0101; path=/";

    await handleUrlOriginParam();
    await vi.advanceTimersByTimeAsync(30 * 60 * 1000);

    expect(useIframeStore.getState()).toMatchObject({
      userId: "80000002",
      userName: null,
      token: null,
      bbk: "100",
      orgCode: "ORG001",
      positionId: "RB0101",
      initialized: true,
      sessionId: "session-1",
      taskId: "task-1",
    });
    expect(mockedFetchUserInit).not.toHaveBeenCalled();
    expect(mockedFetchCustomerInfo).not.toHaveBeenCalled();
    expect(authApi.sendCronAuth).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("/wealth + origin=Y 时从 cookie 读取用户名且不请求用户信息接口", async () => {
    window.history.pushState({}, "", "/wealth/board?origin=Y");
    document.cookie = "userid=80000002; path=/";
    document.cookie = "vbbk=100; path=/";
    document.cookie = "username=%E5%BC%A0%E4%B8%89; path=/";

    await handleUrlOriginParam();
    await expect(fetchAndSetUserName()).resolves.toBe(true);

    expect(mockedFetchUserInfo).not.toHaveBeenCalled();
    expect(useIframeStore.getState().userName).toBe("张三");
  });

  it.each([
    "/wealth/board?origin=Y",
    "/console/wealth/board?origin=Y",
  ])("%s 时不初始化 Agent", async (path) => {
    window.history.pushState({}, "", path);
    document.cookie = "userid=80000002; path=/";

    await handleUrlOriginParam();

    expect(mockedFetchUserInit).not.toHaveBeenCalled();
  });

  it("/console/wealth + origin=Y 时也不请求用户信息接口", async () => {
    window.history.pushState({}, "", "/console/wealth/board?origin=Y");
    document.cookie = "userid=80000002; path=/";
    document.cookie = "vbbk=100; path=/";
    document.cookie = "username=李四; path=/";

    await handleUrlOriginParam();
    await expect(fetchAndSetUserName()).resolves.toBe(true);

    expect(mockedFetchUserInfo).not.toHaveBeenCalled();
    expect(useIframeStore.getState().userName).toBe("李四");
  });

  it("财富工作台内部导航丢失 origin 查询串后仍不请求用户信息接口", async () => {
    window.history.pushState({}, "", "/wealth/board?origin=Y");
    document.cookie = "userid=80000002; path=/";
    document.cookie = "vbbk=100; path=/";
    document.cookie = "username=张三; path=/";

    await handleUrlOriginParam();
    window.history.pushState({}, "", "/wealth/create");
    await expect(fetchAndSetUserName()).resolves.toBe(true);

    expect(mockedFetchUserInfo).not.toHaveBeenCalled();
    expect(useIframeStore.getState().userName).toBe("张三");
  });

  it("财富工作台缺少 BBK cookie 时回退查询用户信息", async () => {
    window.history.pushState({}, "", "/wealth/board?origin=Y");
    document.cookie = "userid=80000002; path=/";
    document.cookie = "username=张三; path=/";
    mockedFetchUserInfo.mockResolvedValueOnce({
      code: "SUC0000",
      message: "success",
      result: true,
      data: [{ userName: "接口姓名", pathName: "某企业/总行/生产部" }],
    });

    await handleUrlOriginParam();
    await expect(fetchAndSetUserName()).resolves.toBe(true);

    expect(mockedFetchUserInfo).toHaveBeenCalledWith("80000002");
    expect(useIframeStore.getState()).toMatchObject({
      userName: "接口姓名",
      bbk: "100",
    });
  });

  it("客户信息切换用户后不使用旧用户的 username cookie", async () => {
    window.history.pushState({}, "", "/wealth/board?origin=Y");
    document.cookie = "userid=80000002; path=/";
    document.cookie = "vbbk=100; path=/";
    document.cookie = "username=旧用户; path=/";
    mockedFetchCustomerInfo.mockResolvedValueOnce({
      returnCode: "SUC0000",
      body: {
        output: {
          result: {
            userChange: true,
            sysId: "new-sys",
            token: "new-token",
            bbk: "200",
            orgCode: "new-org",
            orgLvl: "new-level",
            userId: "80000003",
            positionId: "RB0101",
          },
        },
      },
    });
    mockedFetchUserInfo.mockResolvedValueOnce({
      code: "SUC0000",
      message: "success",
      result: true,
      data: [{ userName: "新用户", pathName: "某企业/分行/生产部" }],
    });

    await handleUrlOriginParam();
    await expect(fetchAndSetUserName()).resolves.toBe(true);

    expect(mockedFetchUserInfo).toHaveBeenCalledWith("80000003");
    expect(useIframeStore.getState()).toMatchObject({
      userId: "80000003",
      userName: "新用户",
      bbk: "200",
    });
  });

  it("非财富路由的 origin=Y 入口仍请求用户信息接口", async () => {
    window.history.pushState({}, "", "/chat?origin=Y");
    document.cookie = "userid=80000002; path=/";
    document.cookie = "username=张三; path=/";
    mockedFetchUserInfo.mockResolvedValueOnce({
      code: "SUC0000",
      message: "success",
      result: true,
      data: [{ userName: "接口姓名", pathName: "某企业/总行/生产部" }],
    });

    await handleUrlOriginParam();
    await expect(fetchAndSetUserName()).resolves.toBe(true);

    expect(mockedFetchUserInfo).toHaveBeenCalledWith("80000002");
    expect(useIframeStore.getState().userName).toBe("接口姓名");
  });

  it("非 origin=Y 入口会清除本次页面的 origin 标记", async () => {
    useIframeStore.getState().setOriginY(true);

    await handleUrlOriginParam();

    expect(useIframeStore.getState().isOriginY).toBe(false);
  });

  it("origin=Y 时从 cookie 读取 subBranchId", async () => {
    window.history.pushState({}, "", "/?origin=Y");
    document.cookie = "userid=80000002; path=/";
    document.cookie = "subBranchId=SUB001; path=/";

    await handleUrlOriginParam();

    expect(useIframeStore.getState().subBranchId).toBe("SUB001");
  });

  it("origin=Y 首次进入时不被客户信息接口阻塞用户初始化", async () => {
    window.history.pushState({}, "", "/?origin=Y");
    document.cookie = "userid=80000002; path=/";
    document.cookie = "vbbk=100; path=/";
    document.cookie = "vorgcode=ORG001; path=/";
    document.cookie = "positionID=POS001; path=/";
    mockedFetchCustomerInfo.mockReturnValueOnce(new Promise<null>(() => {}));

    void handleUrlOriginParam();
    await Promise.resolve();

    expect(mockedFetchUserInit).toHaveBeenCalledWith({
      filename: "PROFILE.md",
      text: expect.stringMatching(
        /分行号：100[\s\S]*网点机构编号：ORG001[\s\S]*岗位编号：POS001[\s\S]*客户经理ID：80000002/,
      ),
    });
  });

  it("origin=Y 进入时不使用本地初始化标记跳过接口", async () => {
    window.history.pushState({}, "", "/?origin=Y");
    document.cookie = "userid=80000002; path=/";
    localStorage.setItem("swe-80000002", "exist");

    await handleUrlOriginParam();

    expect(mockedFetchUserInit).toHaveBeenCalledWith({
      filename: "PROFILE.md",
      text: expect.stringContaining("客户经理ID：80000002"),
    });
  });

  it("origin=Y 客户信息未切换用户时只同步 cookie token", async () => {
    window.history.pushState({}, "", "/?origin=Y");
    document.cookie = "userid=80000002; path=/";
    document.cookie = "token=fresh-token; path=/";
    mockedFetchCustomerInfo.mockResolvedValueOnce({
      returnCode: "SUC0000",
      body: {
        output: {
          result: {
            userChange: false,
            sysId: "updated-sys",
            token: "response-token",
            bbk: "updated-bbk",
            orgCode: "updated-org",
            orgLvl: "updated-lvl",
            userId: "updated-user",
            positionId: "updated-position",
          },
        },
      },
    });

    await handleUrlOriginParam();

    expect(useIframeStore.getState()).toMatchObject({
      userId: "80000002",
      token: "fresh-token",
      bbk: null,
      orgCode: null,
      positionId: null,
      userChange: false,
    });
  });

  it("origin=Y 客户信息未切换用户时只初始化一次", async () => {
    window.history.pushState({}, "", "/?origin=Y");
    document.cookie = "userid=80000002; path=/";
    mockedFetchUserInit.mockResolvedValue({ appended: true });
    mockedFetchCustomerInfo.mockResolvedValueOnce({
      returnCode: "SUC0000",
      body: {
        output: {
          result: {
            userChange: false,
            sysId: "sys",
            token: "response-token",
            bbk: "bbk",
            orgCode: "org",
            orgLvl: "lvl",
            userId: "80000002",
            positionId: "position",
          },
        },
      },
    });

    await handleUrlOriginParam();

    expect(mockedFetchUserInit).toHaveBeenCalledTimes(1);
  });

});
