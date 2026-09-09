import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { Modal } from "antd";
import CronJobOverviewPage from "./index";
import styles from "./index.module.less";

const api = vi.hoisted(() => ({
  getCronJobOverviewPageData: vi.fn(),
  getCronBranchTaskBehavior: vi.fn(),
  exportBranchDimension: vi.fn(),
}));
const iframe = vi.hoisted(() => ({ bbk: undefined as string | undefined }));
vi.mock("../../../api/modules/monitor", () => ({ monitorApi: api }));
vi.mock("../../../stores/iframeStore", () => ({
  useIframeStore: (selector: (state: typeof iframe) => unknown) =>
    selector(iframe),
}));

const pageData = {
  summaryMetrics: [],
  failureReasons: [],
  anomalySummary: {},
  anomalyRankRows: [],
  branchRankingRows: [
    { rank: 1, bbkId: "200", branchName: "测试分行", skillCount: "2" },
  ],
};
const createObjectURL = vi.fn<(blob: Blob) => string>(() => "blob:server-file");
const revokeObjectURL = vi.fn();

beforeEach(() => {
  iframe.bbk = undefined;
  api.getCronJobOverviewPageData.mockResolvedValue(pageData);
  api.getCronBranchTaskBehavior.mockResolvedValue({ items: [] });
  api.exportBranchDimension.mockResolvedValue(new Blob(["backend file"]));
  vi.stubGlobal(
    "URL",
    class extends URL {
      static createObjectURL = createObjectURL;
      static revokeObjectURL = revokeObjectURL;
    },
  );
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("branch dimension server export", () => {
  it("keeps button styling, sends current filters and sort, and downloads only the response blob", async () => {
    const { container } = render(
      <MemoryRouter
        initialEntries={[
          "/analytics/cron-job-overview?start_date=2026-09-01&end_date=2026-09-08&bbk_ids=200,300",
        ]}
      >
        <CronJobOverviewPage />
      </MemoryRouter>,
    );
    await screen.findByText("测试分行");
    const button = screen.getByRole("button", { name: "分行维度导出 Excel" });
    expect(button).toHaveClass(styles.exportButton);
    const sort = screen.getByRole("button", { name: "技能数排序" });
    const base = {
      start_date: "2026-09-01",
      end_date: "2026-09-08",
      bbk_ids: "200,300",
    };
    for (const direction of ["desc", "asc", null]) {
      fireEvent.click(sort);
      fireEvent.click(button);
      expect(button).toBeDisabled();
      expect(api.exportBranchDimension).toHaveBeenLastCalledWith(
        direction
          ? { ...base, sort_by: "skillCount", sort_order: direction }
          : base,
      );
      await waitFor(() => expect(button).toBeEnabled());
    }
    expect(container.querySelector("table")).toBeInTheDocument();
    expect(createObjectURL).toHaveBeenCalledWith(
      await api.exportBranchDimension.mock.results[0].value,
    );
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledTimes(3);
    await waitFor(() =>
      expect(revokeObjectURL).toHaveBeenCalledWith("blob:server-file"),
    );
  });

  it("locks branch scope for export and recovers from backend errors", async () => {
    iframe.bbk = "200";
    api.exportBranchDimension.mockRejectedValueOnce(new Error("无权导出"));
    const error = vi
      .spyOn(Modal, "error")
      .mockImplementation(() => ({ destroy: vi.fn(), update: vi.fn() }));
    render(
      <MemoryRouter initialEntries={["/?bbk_ids=300"]}>
        <CronJobOverviewPage />
      </MemoryRouter>,
    );
    await screen.findByText("测试分行");
    const button = screen.getByRole("button", { name: "分行维度导出 Excel" });
    fireEvent.click(button);
    await waitFor(() =>
      expect(error).toHaveBeenCalledWith({
        title: "导出失败",
        content: "无权导出",
      }),
    );
    expect(api.exportBranchDimension).toHaveBeenCalledWith(
      expect.objectContaining({ bbk_ids: "200" }),
    );
    expect(createObjectURL).not.toHaveBeenCalled();
    expect(button).toBeEnabled();
    fireEvent.click(button);
    await waitFor(() => expect(createObjectURL).toHaveBeenCalledOnce());
  });

  it("disables export during loading and for an empty table", async () => {
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
    fireEvent.click(button);
    expect(api.exportBranchDimension).not.toHaveBeenCalled();
  });
});
