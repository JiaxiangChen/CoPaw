"""Excel presentation of the existing branch behavior statistics."""

from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from ...models.cron import CronBranchRankingItem

# Order and labels follow Console's RankingTable. Each tuple is
# (public sort key, heading, numerator/field, optional denominator).
BRANCH_COLUMNS = (
    ("skillCount", "技能数", "skill_count", None),
    ("totalTasks", "任务总数", "total_tasks", None),
    ("successCount", "成功执行任务总数", "success_count", None),
    ("readTasks", "已读任务数", "read_tasks", None),
    ("involvedManagers", "涉及用户数", "involved_managers", None),
    ("resultViewManagers", "查看结果的用户数", "result_view_managers", None),
    (
        "resultViewManagerRate",
        "RM查看Claw任务结果比例",
        "result_view_managers",
        "involved_managers",
    ),
    ("planManagers", "查看客户级方案用户数", "plan_managers", None),
    (
        "planManagerRate",
        "查看结果的RM中点击客户级方案的比例",
        "plan_managers",
        "result_view_managers",
    ),
    ("insightManagers", "点击去洞察用户数", "insight_managers", None),
    (
        "insightManagerRate",
        "查看结果的RM中点击去洞察的比例",
        "insight_managers",
        "plan_managers",
    ),
    ("phoneManagers", "点击去电访的用户数", "phone_managers", None),
    (
        "phoneManagerRate",
        "查看结果的RM中点击去电访的比例",
        "phone_managers",
        "plan_managers",
    ),
    (
        "recommendedCustomers",
        "Claw任务推荐的客户数",
        "recommended_customers",
        None,
    ),
    ("viewedCustomers", "被用户查看的客户数", "viewed_customers", None),
    (
        "viewedCustomerRate",
        "客户查看率",
        "viewed_customers",
        "recommended_customers",
    ),
    ("insightCustomers", "去洞察客户数", "insight_customers", None),
    ("phoneCustomers", "去电访客户数", "phone_customers", None),
    ("contactedCustomers", "接触客户数", "contacted_customers", None),
    ("contactRate", "接触客户率", "contact_rate", None),
)
BRANCH_SORT_KEYS = frozenset(column[0] for column in BRANCH_COLUMNS)


def branch_values(item: CronBranchRankingItem) -> list[int | float]:
    """Round percentage points before sorting, just as the UI displays them."""
    values = []
    for key, _, field, denominator in BRANCH_COLUMNS:
        value = getattr(item, field) or 0
        if denominator:
            divisor = getattr(item, denominator) or 0
            value = value / divisor if divisor else 0
        if denominator or key == "contactRate":
            # Decimal.from_float preserves JS Number/toFixed tie behavior.
            value = float(
                Decimal.from_float(value * 100).quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP,
                )
            )
        values.append(value)
    return values


def export_branch_dimension(
    items: list[CronBranchRankingItem],
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> bytes:
    """Export all rows; stable sorting retains the query order for ties."""
    rows = [(item, branch_values(item)) for item in items]
    if sort_by:
        index = next(
            i for i, col in enumerate(BRANCH_COLUMNS) if col[0] == sort_by
        )
        rows.sort(key=lambda row: row[1][index], reverse=sort_order == "desc")

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "分行维度"
    sheet.merge_cells("A1:A2")
    sheet.merge_cells("B1:B2")
    sheet["B1"] = "分行名称"
    for cell_range, title in (
        ("C1:F1", "任务信息"),
        ("G1:O1", "by客户经理"),
        ("P1:V1", "by客户"),
    ):
        sheet.merge_cells(cell_range)
        sheet[cell_range.split(":")[0]] = title
    for column, (_, title, _, _) in enumerate(BRANCH_COLUMNS, 3):
        sheet.cell(2, column, title)
    for row in sheet.iter_rows(min_row=1, max_row=2, max_col=22):
        for cell in row:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="4472C4")
            cell.alignment = Alignment(
                horizontal="center", vertical="center", wrap_text=True
            )
    sheet.row_dimensions[1].height = 24
    sheet.row_dimensions[2].height = 65
    for column in range(1, 23):
        sheet.column_dimensions[get_column_letter(column)].width = 18
    sheet.column_dimensions["A"].width = 8
    sheet.column_dimensions["B"].width = 25
    sheet.freeze_panes = "C3"
    for rank, (item, values) in enumerate(rows, 1):
        sheet.cell(rank + 2, 1, rank)
        name = sheet.cell(rank + 2, 2, item.bbk_name or item.bbk_id or "-")
        name.data_type = "s"
        for column, (value, definition) in enumerate(
            zip(values, BRANCH_COLUMNS), 3
        ):
            percentage = definition[3] or definition[0] == "contactRate"
            cell = sheet.cell(
                rank + 2, column, value / 100 if percentage else value
            )
            cell.number_format = "0.00%" if percentage else "#,##0"
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()
