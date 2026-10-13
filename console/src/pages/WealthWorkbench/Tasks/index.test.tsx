import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Customer } from "../types";
import { CustomerLabel, Opportunities } from "./index";

function customer(reason: string): Customer {
  return {
    id: "skill-1|cust-1",
    custUid: "cust-1",
    skillId: "skill-1",
    name: "王*",
    label: "",
    reason,
    category: "理财",
    task: "资产到期经营",
    done: false,
    channel: "",
    time: "",
    note: "",
    opportunities: reason ? [reason] : [],
  };
}

describe("Opportunities", () => {
  it("渲染经过清洗的 HTML 列表和加粗内容", () => {
    const reason =
      '<li onclick="alert(1)">持有xxx<b>三层三间</b>，金额200万</li>' +
      "<li>第二条<script>alert(1)</script></li>";
    const { container } = render(<Opportunities customer={customer(reason)} />);

    expect(container.querySelectorAll("ul > li")).toHaveLength(2);
    expect(screen.getByText("三层三间").tagName).toBe("B");
    expect(container.querySelector("[onclick]")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
  });

  it("兼容纯文本", () => {
    render(<Opportunities customer={customer("普通经营机会")} />);

    expect(screen.getByText("普通经营机会")).toBeInTheDocument();
  });

  it("省略预览并在悬停时展示完整 HTML 内容", async () => {
    const reason = "<p>第一段经营机会</p><p><b>第二段完整内容</b></p>";
    const { container } = render(<Opportunities customer={customer(reason)} />);

    const preview = container.querySelector('[tabindex="0"]');
    expect(preview).toBeInTheDocument();

    fireEvent.mouseEnter(preview!);

    const tooltip = await screen.findByRole("tooltip");
    expect(tooltip).toHaveTextContent("第一段经营机会");
    expect(tooltip.querySelector("b")).toHaveTextContent("第二段完整内容");
  });

  it("保留常规块级 HTML 元素", () => {
    const reason = "<div><h1>重点机会</h1><p>客户近期有配置需求</p></div>";
    const { container } = render(<Opportunities customer={customer(reason)} />);

    expect(container.querySelector("h1")).toHaveTextContent("重点机会");
    expect(container.querySelector("p")).toHaveTextContent(
      "客户近期有配置需求",
    );
  });

  it("空值展示占位符", () => {
    render(<Opportunities customer={customer("")} />);

    expect(screen.getByText("--")).toBeInTheDocument();
  });
});

describe("CustomerLabel", () => {
  it("多个标签拆成独立背景项，忽略分隔符之间的空值", () => {
    const { unmount } = render(
      <CustomerLabel label=" 行长指派 、分行重点、、我的关注 " />,
    );
    const first = screen.getByText("行长指派");
    const second = screen.getByText("分行重点");
    const third = screen.getByText("我的关注");
    expect(first.parentElement).toBe(second.parentElement);
    expect(first.parentElement).toBe(third.parentElement);
    expect(first.parentElement?.children).toHaveLength(3);
    expect(first.className).not.toBe(second.className);
    unmount();
  });

  it("空标签展示暂无标签占位", () => {
    render(<CustomerLabel label="" />);

    expect(screen.getByText("暂无标签")).toBeInTheDocument();
  });

  it("非空标签保持原文展示", () => {
    render(<CustomerLabel label="行长指派" />);

    expect(screen.getByText("行长指派")).toBeInTheDocument();
  });
});
