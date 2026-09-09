import ExcelJS from "exceljs";
import dayjs from "dayjs";

export async function exportBranchTable(table: HTMLTableElement) {
  const workbook = new ExcelJS.Workbook();
  const sheet = workbook.addWorksheet("分行维度");
  const headerRows = table.tHead?.rows.length ?? 0;

  Array.from(table.rows).forEach((row, rowIndex) => {
    const rowNumber = rowIndex + 1;
    let columnNumber = 1;
    Array.from(row.cells).forEach((cell) => {
      while (sheet.getCell(rowNumber, columnNumber).isMerged) columnNumber++;
      const target = sheet.getCell(rowNumber, columnNumber);
      // Keep display text verbatim, including percentages, separators and leading zeros.
      // String cells also prevent branch names beginning with '=' from becoming formulas.
      target.value = cell.textContent ?? "";
      target.alignment = {
        horizontal: "center",
        vertical: "middle",
        wrapText: true,
      };
      target.font = { name: "微软雅黑", size: 11, bold: rowIndex < headerRows };
      if (rowIndex < headerRows) {
        target.fill = {
          type: "pattern",
          pattern: "solid",
          fgColor: { argb: "FFF3F6FA" },
        };
      }
      if (cell.rowSpan > 1 || cell.colSpan > 1) {
        sheet.mergeCells(
          rowNumber,
          columnNumber,
          rowNumber + cell.rowSpan - 1,
          columnNumber + cell.colSpan - 1,
        );
      }
      columnNumber += cell.colSpan;
    });
    sheet.getRow(rowNumber).height = rowIndex < headerRows ? 60 : 30;
  });
  sheet.columns.forEach((column, index) => {
    column.width = index === 0 ? 6 : index === 1 ? 22 : 18;
  });
  sheet.views = [{ state: "frozen", ySplit: headerRows }];

  const buffer = await workbook.xlsx.writeBuffer();
  const blob = new Blob([buffer], {
    type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `定时任务分行维度_${dayjs().format("YYYYMMDD_HHmmss")}.xlsx`;
  document.body.appendChild(link);
  try {
    link.click();
  } finally {
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }
}
