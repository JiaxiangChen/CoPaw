import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import CronJobOverviewPage from "./index";
import styles from "./index.module.less";

const monitorApiMock = vi.hoisted(() => ({
  getCronJobOverviewPageData: vi.fn(),
  getCronBranchTaskBehavior: vi.fn(),
  getBranchSkills: vi.fn(),
  getBranchSkillManagers: vi.fn(),
  getBranchSkillManagerCustomers: vi.fn(),
  getBranchManagerSummary: vi.fn(),
  getManagerSkills: vi.fn(),
  getManagerCustomers: vi.fn(),
  getExecutions: vi.fn(),
}));
const iframeStoreMock = vi.hoisted(() => ({
  bbk: undefined as string | undefined,
}));

vi.mock("../../../api/modules/monitor", async () => {
  const actual = await vi.importActual<
    typeof import("../../../api/modules/monitor")
  >("../../../api/modules/monitor");
  return {
    ...actual,
    monitorApi: monitorApiMock,
  };
});

vi.mock("../../../stores/iframeStore", () => ({
  useIframeStore: (selector: (state: unknown) => unknown) =>
    selector({
      bbk: iframeStoreMock.bbk,
    }),
}));

describe("CronJobOverview summary cards", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    iframeStoreMock.bbk = undefined;
    monitorApiMock.getCronJobOverviewPageData.mockResolvedValue({
      summaryMetrics: [
        { key: "branches", value: "12" },
        { key: "managers", value: "86" },
        {
          key: "tasks",
          value: "320",
          hintValue: "新增 12 个",
          footerValue: "2,480 次",
        },
        { key: "success", value: "93.20", footerValue: "2,112/154" },
        { key: "read", value: "61.50", footerValue: "1,525" },
        { key: "report", value: "34.80" },
        { key: "report_count", value: "863" },
        { key: "insight_count", value: "512" },
        { key: "phone_count", value: "221" },
      ],
      branchRankingRows: [],
      failureReasons: [],
      anomalySummary: {
        affectedBranches: "0",
        affectedBranchesUnit: "家",
        affectedManagers: "0",
        affectedManagersUnit: "人",
      },
      anomalyRankRows: [],
    });
    monitorApiMock.getCronBranchTaskBehavior.mockResolvedValue({
      start_date: "2026-06-30",
      end_date: "2026-06-30",
      items: [],
    });
    monitorApiMock.getBranchSkills.mockResolvedValue({
      start_date: "2026-06-30",
      end_date: "2026-06-30",
      bbk_id: "100",
      bbk_name: "测试分行",
      items: [],
    });
    monitorApiMock.getBranchSkillManagers.mockResolvedValue({
      start_date: "2026-06-30",
      end_date: "2026-06-30",
      bbk_id: "100",
      skill_name: "skill",
      items: [],
    });
    monitorApiMock.getBranchSkillManagerCustomers.mockResolvedValue({
      start_date: "2026-06-30",
      end_date: "2026-06-30",
      bbk_id: "100",
      skill_name: "skill",
      user_id: "u1",
      items: [],
    });
    monitorApiMock.getBranchManagerSummary.mockResolvedValue({
      start_date: "2026-06-30",
      end_date: "2026-06-30",
      bbk_id: "100",
      bbk_name: "测试分行",
      items: [],
    });
    monitorApiMock.getManagerSkills.mockResolvedValue({
      start_date: "2026-06-30",
      end_date: "2026-06-30",
      bbk_id: "100",
      user_id: "u1",
      user_name: "张三",
      items: [],
    });
    monitorApiMock.getManagerCustomers.mockResolvedValue({
      start_date: "2026-06-30",
      end_date: "2026-06-30",
      bbk_id: "100",
      user_id: "u1",
      user_name: "张三",
      items: [],
    });
    monitorApiMock.getExecutions.mockResolvedValue({
      items: [],
      total: 0,
      page: 1,
      page_size: 100,
    });
  });

  afterEach(() => {
    cleanup();
  });

  it("renders the report metric card with footer metrics and no sub-icons", async () => {
    const { container } = render(
      <MemoryRouter initialEntries={["/analytics/cron-job-overview"]}>
        <Routes>
          <Route
            path="/analytics/cron-job-overview"
            element={<CronJobOverviewPage />}
          />
        </Routes>
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(monitorApiMock.getCronJobOverviewPageData).toHaveBeenCalledTimes(
        1,
      );
    });

    const reportTitle = await screen.findByText("查看方案任务率");
    const reportCard = reportTitle.closest("article");
    expect(reportCard).not.toBeNull();
    expect(reportCard?.querySelectorAll("svg")).toHaveLength(1);
    expect(screen.getByText("查看方案任务数")).toBeInTheDocument();
    expect(screen.getByText("去洞察任务数")).toBeInTheDocument();
    expect(screen.getByText("去电访任务数")).toBeInTheDocument();
    expect(screen.getByText("863")).toBeInTheDocument();
    expect(screen.getByText("512")).toBeInTheDocument();
    expect(screen.getByText("221")).toBeInTheDocument();
    expect(screen.getByLabelText("概览指标").className).toContain(
      styles.summaryGrid,
    );
  });

  it("locks branch filter to current branch for branch users", async () => {
    iframeStoreMock.bbk = "200";

    const { container } = render(
      <MemoryRouter initialEntries={["/analytics/cron-job-overview"]}>
        <Routes>
          <Route
            path="/analytics/cron-job-overview"
            element={<CronJobOverviewPage />}
          />
        </Routes>
      </MemoryRouter>,
    );

    await waitFor(() => {
      const selectedItem = container.querySelector(
        ".ant-select-selection-item",
      );
      expect(selectedItem?.textContent).toContain("200");
    });
    expect(container.querySelector(".ant-select")).toHaveClass(
      "ant-select-disabled",
    );
    await waitFor(() => {
      expect(monitorApiMock.getCronJobOverviewPageData).toHaveBeenCalledWith(
        expect.objectContaining({
          bbk_ids: "200",
        }),
      );
    });
    expect(container.querySelector(".ant-select-disabled")).toBeInTheDocument();
  });

  it("renders expanded manager detail without extra drill-down scroll wrapper", async () => {
    monitorApiMock.getCronJobOverviewPageData.mockResolvedValueOnce({
      summaryMetrics: [
        { key: "branches", value: "12" },
        { key: "managers", value: "86" },
        {
          key: "tasks",
          value: "320",
          hintValue: "新增 12 个",
          footerValue: "2,480 次",
        },
        { key: "success", value: "93.20", footerValue: "2,112/154" },
        { key: "read", value: "61.50", footerValue: "1,525" },
        { key: "report", value: "34.80" },
        { key: "report_count", value: "863" },
        { key: "insight_count", value: "512" },
        { key: "phone_count", value: "221" },
      ],
      branchRankingRows: [
        {
          rank: 1,
          branchName: "测试分行",
          bbkId: "100",
          skillCount: 3,
          totalTasks: 20,
          successCount: 18,
          readTasks: 11,
          involvedManagers: 5,
          resultViewManagers: 4,
          planManagers: 3,
          insightManagers: 2,
          phoneManagers: 1,
          recommendedCustomers: 30,
          viewedCustomers: 12,
          insightCustomers: 5,
          phoneCustomers: 2,
        },
      ],
      failureReasons: [],
      anomalySummary: {
        affectedBranches: "0",
        affectedBranchesUnit: "家",
        affectedManagers: "0",
        affectedManagersUnit: "人",
      },
      anomalyRankRows: [],
    });
    monitorApiMock.getBranchManagerSummary.mockResolvedValueOnce({
      start_date: "2026-06-30",
      end_date: "2026-06-30",
      bbk_id: "100",
      bbk_name: "测试分行",
      items: [
        {
          user_id: "u1",
          user_name: "张三",
          skill_count: 2,
          total_tasks: 10,
          success_count: 9,
          read_tasks: 6,
          result_view_customers: 4,
          plan_customers: 3,
          insight_customers: 2,
          phone_customers: 1,
        },
      ],
    });

    const { container } = render(
      <MemoryRouter initialEntries={["/analytics/cron-job-overview"]}>
        <Routes>
          <Route
            path="/analytics/cron-job-overview"
            element={<CronJobOverviewPage />}
          />
        </Routes>
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByText("测试分行"));

    await waitFor(() => {
      expect(monitorApiMock.getBranchManagerSummary).toHaveBeenCalledTimes(1);
    });

    expect(
      await screen.findByText("当前分行下的客户经理明细"),
    ).toBeInTheDocument();
    expect(
      container.querySelector(`.${styles.drillDownTableScroll}`),
    ).toBeNull();
  });

  it("renders the branch-dimension report with grouped headers and sortable metrics", async () => {
    monitorApiMock.getCronJobOverviewPageData.mockResolvedValueOnce({
      summaryMetrics: [],
      branchRankingRows: [
        {
          rank: 1,
          branchName: "测试分行",
          bbkId: "100",
          skillCount: "3",
          totalTasks: "20",
          successCount: "18",
          readTasks: "11",
          involvedManagers: "5",
          resultViewManagers: "4",
          resultViewManagerRate: "80.00%",
          planManagers: "3",
          planManagerRate: "75.00%",
          insightManagers: "2",
          insightManagerRate: "66.67%",
          phoneManagers: "1",
          phoneManagerRate: "33.33%",
          recommendedCustomers: "30",
          viewedCustomers: "12",
          viewedCustomerRate: "40.00%",
          insightCustomers: "5",
          phoneCustomers: "2",
          contactedCustomers: "8",
          contactRate: "40.00%",
        },
      ],
      failureReasons: [],
      anomalySummary: {
        affectedBranches: "0",
        affectedBranchesUnit: "家",
        affectedManagers: "0",
        affectedManagersUnit: "人",
      },
      anomalyRankRows: [],
    });
    monitorApiMock.getBranchManagerSummary.mockResolvedValueOnce({
      start_date: "2026-06-30",
      end_date: "2026-06-30",
      bbk_id: "100",
      bbk_name: "测试分行",
      items: [
        {
          user_id: "u2",
          user_name: "李四",
          skill_count: 3,
          total_tasks: 20,
          success_count: 16,
          read_tasks: 9,
          recommended_customers: 24,
          viewed_customers: 10,
          insight_customers: 4,
          phone_customers: 2,
          contacted_customers: 8,
          contact_rate: 0.4,
        },
        {
          user_id: "u1",
          user_name: "张三",
          skill_count: 1,
          total_tasks: 5,
          success_count: 5,
          read_tasks: 2,
          recommended_customers: 8,
          viewed_customers: 3,
          insight_customers: 1,
          phone_customers: 1,
          contacted_customers: 2,
          contact_rate: 0.25,
        },
      ],
    });

    const { container } = render(
      <MemoryRouter initialEntries={["/analytics/cron-job-overview"]}>
        <Routes>
          <Route
            path="/analytics/cron-job-overview"
            element={<CronJobOverviewPage />}
          />
        </Routes>
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByText("测试分行"));

    await screen.findByText("当前分行下的客户经理明细");
    const managerTable = container.querySelector(".ant-table");
    expect(managerTable).not.toBeNull();
    const headerCells = Array.from(
      managerTable!.querySelectorAll(".ant-table-thead th"),
    );
    const managerNameHeader = headerCells.find(
      (cell) => cell.textContent?.includes("客户经理名称"),
    );
    const totalTasksHeader = headerCells.find(
      (cell) => cell.textContent?.includes("任务总数"),
    );

    expect(managerNameHeader?.querySelector(".ant-table-column-sorter")).toBe(
      null,
    );
    expect(
      totalTasksHeader?.querySelector(".ant-table-column-sorter"),
    ).not.toBeNull();

    const managerNames = () =>
      Array.from(
        managerTable!.querySelectorAll(
          ".ant-table-tbody tr:not(.ant-table-measure-row)",
        ),
      ).map((row) => row.children[0]?.textContent);

    expect(managerNames()).toEqual(["李四", "张三"]);

    fireEvent.click(
      totalTasksHeader!.querySelector(".ant-table-column-sorters")!,
    );

    expect(managerNames()).toEqual(["张三", "李四"]);

    expect(screen.queryByText("技能视角-分行综合排行")).not.toBeInTheDocument();
    expect(screen.getByText("分行维度")).toBeInTheDocument();
    expect(screen.getByText("任务信息")).toBeInTheDocument();
    expect(screen.getByText("by客户经理")).toBeInTheDocument();
    expect(screen.getByText("by客户")).toBeInTheDocument();
    expect(screen.getByText("RM查看Claw任务结果比例")).toBeInTheDocument();
    expect(
      screen.getByText("查看结果的RM中点击客户级方案的比例"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("查看结果的RM中点击去洞察的比例"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("查看结果的RM中点击去电访的比例"),
    ).toBeInTheDocument();
    expect(screen.getByText("客户查看率")).toBeInTheDocument();
    expect(screen.getAllByText("接触客户率").length).toBeGreaterThan(0);
    expect(screen.getAllByText("80.00%").length).toBeGreaterThan(0);
    expect(screen.getAllByText("75.00%").length).toBeGreaterThan(0);
    expect(screen.getAllByText("66.67%").length).toBeGreaterThan(0);
    expect(screen.getAllByText("33.33%").length).toBeGreaterThan(0);
  });

  it("shows unified loading placeholders for overview cards, anomaly section, and skill-view ranking while the main query is pending", async () => {
    let resolveOverview:
      | ((
          value: Awaited<
            ReturnType<typeof monitorApiMock.getCronJobOverviewPageData>
          >,
        ) => void)
      | null = null;
    let resolveTaskRanking:
      | ((
          value: Awaited<
            ReturnType<typeof monitorApiMock.getCronBranchTaskBehavior>
          >,
        ) => void)
      | null = null;

    monitorApiMock.getCronJobOverviewPageData.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOverview = resolve;
        }),
    );
    monitorApiMock.getCronBranchTaskBehavior.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveTaskRanking = resolve;
        }),
    );

    render(
      <MemoryRouter initialEntries={["/analytics/cron-job-overview"]}>
        <Routes>
          <Route
            path="/analytics/cron-job-overview"
            element={<CronJobOverviewPage />}
          />
        </Routes>
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(
        screen.getAllByTestId("cron-panel-loading").length,
      ).toBeGreaterThanOrEqual(9);
    });
    expect(screen.getByText("分行综合排行")).toBeInTheDocument();
    expect(screen.getAllByText("加载中...").length).toBeGreaterThan(0);
    expect(screen.getByText("分行层异常诊断")).toBeInTheDocument();
    expect(screen.getByText("分行异常排行").closest("section")).toHaveClass(
      styles.rankPanel,
    );
    expect(screen.getByLabelText("概览指标")).toBeInTheDocument();

    resolveTaskRanking?.({
      start_date: "2026-06-30",
      end_date: "2026-06-30",
      items: [],
    });
    resolveOverview?.({
      summaryMetrics: [],
      branchRankingRows: [],
      failureReasons: [],
      anomalySummary: {
        affectedBranches: "0",
        affectedBranchesUnit: "家",
        affectedManagers: "0",
        affectedManagersUnit: "人",
      },
      anomalyRankRows: [],
    });

    await waitFor(() => {
      expect(
        screen.queryByTestId("cron-panel-loading"),
      ).not.toBeInTheDocument();
    });
  });

  it("renders branch skill details returned by the backend without local filtering", async () => {
    monitorApiMock.getCronBranchTaskBehavior.mockResolvedValueOnce({
      start_date: "2026-06-30",
      end_date: "2026-06-30",
      items: [
        {
          rank: 1,
          bbk_id: "100",
          bbk_name: "测试分行",
          manager_count: 1,
          total_tasks: 2,
          success_count: 2,
          success_rate: 1,
          read_tasks: 1,
          read_rate: 0.5,
        },
      ],
    });
    monitorApiMock.getBranchSkills.mockResolvedValueOnce({
      start_date: "2026-06-30",
      end_date: "2026-06-30",
      bbk_id: "100",
      bbk_name: "测试分行",
      items: [
        {
          skill_name: "insurance_mkt",
          cron_task_count: 2,
          success_count: 2,
          success_rate: 1,
          read_count: 1,
          error_count: 0,
        },
      ],
    });

    render(
      <MemoryRouter initialEntries={["/analytics/cron-job-overview"]}>
        <Routes>
          <Route
            path="/analytics/cron-job-overview"
            element={<CronJobOverviewPage />}
          />
        </Routes>
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByText("测试分行"));

    expect(await screen.findByText("insurance_mkt")).toBeInTheDocument();
  });

  it("sorts skill-view branch ranking metrics on the client while keeping rank and branch headers unsortable", async () => {
    monitorApiMock.getCronJobOverviewPageData.mockResolvedValueOnce({
      summaryMetrics: [],
      branchRankingRows: [
        {
          rank: 1,
          branchName: "甲分行",
          bbkId: "100",
          skillCount: "3",
          totalTasks: "20",
          successCount: "18",
          readTasks: "11",
          involvedManagers: "5",
          resultViewManagers: "4",
          planManagers: "3",
          insightManagers: "2",
          phoneManagers: "1",
          recommendedCustomers: "30",
          viewedCustomers: "12",
          insightCustomers: "5",
          phoneCustomers: "2",
          contactedCustomers: "8",
          contactRate: "40.00%",
        },
        {
          rank: 2,
          branchName: "乙分行",
          bbkId: "200",
          skillCount: "5",
          totalTasks: "8",
          successCount: "8",
          readTasks: "7",
          involvedManagers: "2",
          resultViewManagers: "2",
          planManagers: "1",
          insightManagers: "1",
          phoneManagers: "1",
          recommendedCustomers: "10",
          viewedCustomers: "9",
          insightCustomers: "6",
          phoneCustomers: "1",
          contactedCustomers: "6",
          contactRate: "60.00%",
        },
        {
          rank: 3,
          branchName: "丙分行",
          bbkId: "300",
          skillCount: "1",
          totalTasks: "32",
          successCount: "4",
          readTasks: "2",
          involvedManagers: "1",
          resultViewManagers: "1",
          planManagers: "0",
          insightManagers: "0",
          phoneManagers: "0",
          recommendedCustomers: "5",
          viewedCustomers: "3",
          insightCustomers: "1",
          phoneCustomers: "0",
          contactedCustomers: "1",
          contactRate: "20.00%",
        },
      ],
      failureReasons: [],
      anomalySummary: {
        affectedBranches: "0",
        affectedBranchesUnit: "家",
        affectedManagers: "0",
        affectedManagersUnit: "人",
      },
      anomalyRankRows: [],
    });

    const { container } = render(
      <MemoryRouter initialEntries={["/analytics/cron-job-overview"]}>
        <Routes>
          <Route
            path="/analytics/cron-job-overview"
            element={<CronJobOverviewPage />}
          />
        </Routes>
      </MemoryRouter>,
    );

    await screen.findByText("分行维度");

    expect(
      screen.queryByRole("button", { name: "分行名称排序" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "任务总数排序" }),
    ).toBeInTheDocument();

    const skillViewTable = container.querySelectorAll(
      `.${styles.behaviorTable}`,
    )[0];
    const branchNames = () =>
      Array.from(skillViewTable?.querySelectorAll("tbody tr") ?? []).map(
        (row) => row.children[1]?.textContent,
      );

    expect(branchNames()).toEqual(["甲分行", "乙分行", "丙分行"]);

    fireEvent.click(screen.getByRole("button", { name: "任务总数排序" }));

    expect(branchNames()).toEqual(["丙分行", "甲分行", "乙分行"]);
    expect(
      Array.from(skillViewTable?.querySelectorAll("tbody tr") ?? []).map(
        (row) => row.children[0]?.textContent,
      ),
    ).toEqual(["1", "2", "3"]);
  });

  it("uses full-row drill-down table styling for wrapped branch skill names", async () => {
    monitorApiMock.getCronBranchTaskBehavior.mockResolvedValueOnce({
      start_date: "2026-06-30",
      end_date: "2026-06-30",
      items: [
        {
          bbk_id: "100",
          bbk_name: "测试分行",
          manager_count: 1,
          total_tasks: 2,
          success_count: 2,
          success_rate: 100,
          read_tasks: 1,
          plan_count: 0,
          insight_count: 0,
          phone_count: 0,
          plan_clicks: 0,
          insight_clicks: 0,
          phone_clicks: 0,
          error_count: 0,
        },
      ],
    });
    monitorApiMock.getBranchSkills.mockResolvedValueOnce({
      start_date: "2026-06-30",
      end_date: "2026-06-30",
      bbk_id: "100",
      bbk_name: "测试分行",
      items: [
        {
          skill_name: "长名称技能长名称技能长名称技能长名称技能长名称技能",
          cron_task_count: 2,
          success_count: 2,
          success_rate: 100,
          read_count: 1,
          error_count: 0,
        },
      ],
    });

    const { container } = render(
      <MemoryRouter initialEntries={["/analytics/cron-job-overview"]}>
        <Routes>
          <Route
            path="/analytics/cron-job-overview"
            element={<CronJobOverviewPage />}
          />
        </Routes>
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByText("测试分行"));

    expect(await screen.findByText(/长名称技能/)).toBeInTheDocument();
    expect(
      container.querySelector(`.${styles.branchSkillDrillDownTable}`),
    ).toHaveClass(styles.branchSkillDrillDownTable);
  });
});
