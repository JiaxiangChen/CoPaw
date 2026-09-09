import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import ExcelJS from "exceljs";
import { Modal } from "antd";
import type { CronJobOverviewBranchRankingRow } from "../../../api/modules/monitor";
import CronJobOverviewPage from "./index";

const api = vi.hoisted(() => ({
  getCronJobOverviewPageData: vi.fn(),
  getCronBranchTaskBehavior: vi.fn(),
  exportSkillUsageDetails: vi.fn(),
}));
vi.mock("../../../api/modules/monitor", () => ({ monitorApi: api }));
vi.mock("../../../stores/iframeStore", () => ({
  useIframeStore: (selector: (state: { bbk?: string }) => unknown) =>
    selector({}),
}));

const row: CronJobOverviewBranchRankingRow = {
  rank: 1,
  bbkId: "100",
  branchName: "=测试分行<&>",
  skillCount: "001",
  totalTasks: "1,234",
  successCount: "1200",
  readTasks: "0",
  involvedManagers: "10",
  resultViewManagers: "8",
  resultViewManagerRate: "80.00%",
  planManagers: "4",
  planManagerRate: "50.00%",
  insightManagers: "2",
  insightManagerRate: "25.00%",
  phoneManagers: "1",
  phoneManagerRate: "12.50%",
  recommendedCustomers: "100",
  viewedCustomers: "40",
  viewedCustomerRate: "40.00%",
  insightCustomers: "20",
  phoneCustomers: "10",
  contactedCustomers: "-",
  contactRate: "0.00%",
};
const pageData = {
  summaryMetrics: [],
  branchRankingRows: [
    row,
    { ...row, rank: 2, bbkId: "200", branchName: "另一分行", skillCount: "20" },
  ],
  failureReasons: [],
  anomalySummary: {},
  anomalyRankRows: [],
};

describe("branch dimension Excel export", () => {
  beforeEach(() => {
    api.getCronJobOverviewPageData.mockResolvedValue(pageData);
    api.getCronBranchTaskBehavior.mockResolvedValue({ items: [] });
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("exports every displayed cell and merged header in the current sort order without API requests", async () => {
    const createObjectURL = vi.fn<(blob: Blob) => string>(
      () => "blob:branch-export",
    );
    const revokeObjectURL = vi.fn();
    vi.stubGlobal(
      "URL",
      Object.assign(URL, { createObjectURL, revokeObjectURL }),
    );
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => {});
    const { container } = render(
      <MemoryRouter>
        <CronJobOverviewPage />
      </MemoryRouter>,
    );
    await screen.findByText(row.branchName);
    fireEvent.click(screen.getByRole("button", { name: "技能数排序" }));
    const table = container.querySelector("table")!;
    const displayedRows = Array.from(table.tBodies[0].rows, (tr) =>
      Array.from(tr.cells, (cell) => cell.textContent),
    );
    expect(displayedRows[0][1]).toBe("另一分行");
    vi.clearAllMocks();
    const button = screen.getByRole("button", { name: "分行维度导出 Excel" });
    fireEvent.click(button);
    expect(button).toBeDisabled();
    // Sorting during generation must not change the click-time snapshot.
    fireEvent.click(screen.getByRole("button", { name: "技能数排序" }));
    await waitFor(() => expect(click).toHaveBeenCalledOnce());
    const blob = createObjectURL.mock.calls[0][0] as Blob;
    const buffer = await new Promise<ArrayBuffer>((resolve) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result as ArrayBuffer);
      reader.readAsArrayBuffer(blob);
    });
    const workbook = new ExcelJS.Workbook();
    await workbook.xlsx.load(buffer);
    const sheet = workbook.getWorksheet("分行维度")!;
    expect(sheet.columnCount).toBe(22);
    expect(sheet.rowCount).toBe(displayedRows.length + 2);
    expect([...sheet.model.merges].sort()).toEqual([
      "A1:A2",
      "B1:B2",
      "C1:F1",
      "G1:O1",
      "P1:V1",
    ]);
    expect([
      sheet.getCell("C1").value,
      sheet.getCell("G1").value,
      sheet.getCell("P1").value,
    ]).toEqual(["任务信息", "by客户经理", "by客户"]);
    Array.from(table.tHead!.rows[1].cells).forEach((cell, index) => {
      expect(sheet.getCell(2, index + 3).value).toBe(cell.textContent);
    });
    displayedRows.forEach((cells, index) =>
      cells.forEach((value, column) => {
        expect(sheet.getCell(index + 3, column + 1).value).toBe(value);
      }),
    );
    expect((click.mock.instances[0] as HTMLAnchorElement).download).toMatch(
      /^定时任务分行维度_\d{8}_\d{6}\.xlsx$/,
    );
    expect(api.getCronJobOverviewPageData).not.toHaveBeenCalled();
    expect(api.getCronBranchTaskBehavior).not.toHaveBeenCalled();
    expect(api.exportSkillUsageDetails).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(revokeObjectURL).toHaveBeenCalledWith("blob:branch-export"),
    );
    expect(button).toBeEnabled();
  }, 15000);

  it("disables export while loading and when the table is empty", async () => {
    api.getCronJobOverviewPageData.mockResolvedValue({
      ...pageData,
      branchRankingRows: [],
    });
    render(
      <MemoryRouter>
        <CronJobOverviewPage />
      </MemoryRouter>,
    );
    const button = screen.getByRole("button", { name: "分行维度导出 Excel" });
    expect(button).toBeDisabled();
    await waitFor(() =>
      expect(screen.queryByText("加载中...")).not.toBeInTheDocument(),
    );
    expect(button).toBeDisabled();
  });

  it("reports export failure and allows retry", async () => {
    vi.stubGlobal(
      "URL",
      Object.assign(URL, {
        createObjectURL: vi.fn(() => {
          throw new Error("文件生成失败");
        }),
      }),
    );
    const error = vi
      .spyOn(Modal, "error")
      .mockImplementation(() => ({ destroy: vi.fn(), update: vi.fn() }));
    render(
      <MemoryRouter>
        <CronJobOverviewPage />
      </MemoryRouter>,
    );
    await screen.findByText(row.branchName);
    const button = screen.getByRole("button", { name: "分行维度导出 Excel" });
    fireEvent.click(button);
    await waitFor(() =>
      expect(error).toHaveBeenCalledWith({
        title: "导出失败",
        content: "文件生成失败",
      }),
    );
    expect(button).toBeEnabled();
  });
});
