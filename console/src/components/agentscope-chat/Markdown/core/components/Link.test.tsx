import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import Link from "./Link";

vi.mock("@/components/agentscope-chat", () => ({
  useProviderContext: () => ({ getPrefixCls: (name: string) => name }),
}));

vi.mock("../../../DownloadFileCard", () => ({
  default: ({ url, fileName }: { url: string; fileName: string }) => (
    <div data-testid="file-card" data-url={url}>
      {fileName}
    </div>
  ),
}));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  delete window.__postMsgSwe__;
});

function mockParent() {
  const postMessage = vi.fn();
  vi.stubGlobal("parent", { postMessage });
  return postMessage;
}

function clickLink(href: string) {
  render(<Link href={href}>打开链接</Link>);
  fireEvent.click(screen.getByRole("link"));
}

describe("Markdown Link", () => {
  it("向父应用发送解码后的 jumpUrlKey 对象，并使用配置的目标源", () => {
    const postMessage = mockParent();
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    window.__postMsgSwe__ = "https://parent.example.com";
    clickLink("https://example.com/?jumpUrlKey=abc%2B123");
    expect(postMessage).toHaveBeenCalledWith(
      { type: "CLAW_SEND_URL_KEY", payload: { jumpUrlKey: "abc+123" } },
      "https://parent.example.com",
    );
    expect(open).not.toHaveBeenCalled();
  });

  it("同时存在两个消息参数时优先发送 toParentMsg 字符串", () => {
    const postMessage = mockParent();
    clickLink("https://example.com/?toParentMsg=old+message&jumpUrlKey=abc");
    expect(postMessage).toHaveBeenCalledExactlyOnceWith("old+message", "*");
  });

  it("保留空 jumpUrlKey 参数的发送行为", () => {
    const postMessage = mockParent();
    clickLink("https://example.com/?jumpUrlKey=");
    expect(postMessage).toHaveBeenCalledWith(
      { type: "CLAW_SEND_URL_KEY", payload: { jumpUrlKey: "" } },
      "*",
    );
  });

  it("独立页面点击消息链接时打开原始 URL", () => {
    vi.stubGlobal("parent", window);
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    const href = "https://example.com/?jumpUrlKey=abc";
    clickLink(href);
    expect(open).toHaveBeenCalledWith(href, "_blank", "noopener,noreferrer");
  });

  it("iframe 内无消息参数的相对链接正常打开", () => {
    const postMessage = mockParent();
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    clickLink("/customer");
    expect(postMessage).not.toHaveBeenCalled();
    expect(open).toHaveBeenCalledWith(
      "/customer",
      "_blank",
      "noopener,noreferrer",
    );
  });

  it("文件链接仍渲染文件卡片", () => {
    render(<Link href="https://example.com/report.pdf">下载</Link>);
    expect(screen.getByTestId("file-card")).toHaveTextContent("report.pdf");
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("保留电访和客户洞察的业务 ID 以及客户洞察 href 编码", () => {
    const params = btoa("custUid=customer&bbkOrgId=branch");
    const href = `https://xxxx.yyyy.zz.cn/#/homepage/${params}`;
    render(
      <>
        <Link href="https://example.com/?custUid=customer&bbkOrgId=branch#/wpcontactpanelv2/">
          电访
        </Link>
        <Link href={href}>洞察</Link>
      </>,
    );
    expect(screen.getByText("电访")).toHaveAttribute(
      "id",
      "2DF_branch_customer",
    );
    const insight = screen.getByText("洞察");
    expect(insight).toHaveAttribute("id", "2KH_branch_customer");
    const encoded = insight.getAttribute("href") || "";
    const key = "RM-CLAW-WEB";
    const decoded = Array.from(atob(encoded), (char, index) =>
      String.fromCharCode(
        char.charCodeAt(0) ^ key.charCodeAt(index % key.length),
      ),
    ).join("");
    expect(decoded).toBe(href);
  });

  it("脚注仍打开对应引用链接，返回符号不渲染", () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    render(
      <>
        <div id="footnote-1">
          <a href="https://example.com/source">来源</a>
        </div>
        <Link id="user-ref-1" href="#footnote-1" data-footnote-ref="">
          1
        </Link>
        <Link data-footnote-backref="">↩</Link>
      </>,
    );
    fireEvent.click(screen.getByText("1"));
    expect(open).toHaveBeenCalledWith(
      "https://example.com/source",
      "_blank",
      "noopener,noreferrer",
    );
    expect(screen.queryByText("↩")).not.toBeInTheDocument();
  });
});
