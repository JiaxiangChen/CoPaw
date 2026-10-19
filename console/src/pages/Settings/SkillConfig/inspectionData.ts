/**
 * 文件不一致！！！！！
 * ！！！！！！！！！！
 * ！！！！！！！！！！
 */
import type {
  SkillExposureStats,
  SkillPreviewStats,
  SkillValueReturnStats,
} from "@/api/modules/skillConfig";

export interface InspectionMetric {
  label: string;
  value: string;
  suffix?: string;
  description: string;
}

export interface InspectionSection {
  title: string;
  description: string;
  metrics: InspectionMetric[];
}

export interface InspectionDepthItem {
  id?: string;
  sequence?: number;
  label: string;
  value: string;
  exposureCount?: number;
  ratePercent?: number;
}

export interface SkillInspectionData {
  sections: InspectionSection[];
  depthItems: InspectionDepthItem[];
  depthTotalCount?: number;
}

function formatMetricValue(value: number | undefined): string {
  return Number.isFinite(value) ? String(value) : "--";
}

function formatAmount(value: number | undefined): {
  value: string;
  suffix?: string;
} {
  if (!Number.isFinite(value)) return { value: "--" };
  const amount = value as number;
  if (Math.abs(amount) < 10000) {
    return { value: String(amount), suffix: "元" };
  }
  return {
    value: String(Number((amount / 10000).toFixed(2))),
    suffix: "万元",
  };
}

function formatCountDescription(
  countLabel: string,
  count: number | undefined,
  listCount: number | undefined,
): string {
  if (!Number.isFinite(count) || !Number.isFinite(listCount)) {
    return `${countLabel} / 名单数`;
  }
  return `${countLabel} ${count} / 名单数 ${listCount}`;
}

function parseExposurePercent(value: string): number {
  const parsed = Number.parseFloat(value.replace("%", ""));
  return Number.isFinite(parsed) ? Math.min(100, Math.max(0, parsed)) : 0;
}

export function buildSkillInspectionData(
  previewStats?: SkillPreviewStats,
  valueReturnStats?: SkillValueReturnStats,
  exposureStats?: SkillExposureStats,
): SkillInspectionData {
  return {
    sections: [
      {
        title: "查看",
        description: "L1 盘户名单触达",
        metrics: [
          {
            label: "整体 UV",
            value: formatMetricValue(previewStats?.uv),
            description: "客户经理数",
          },
          {
            label: "整体 PV",
            value: formatMetricValue(previewStats?.pv),
            description: "名单页总浏览次数",
          },
        ],
      },
      {
        title: "采纳",
        description: "名单被采纳、被联系的比例",
        metrics: [
          {
            label: "名单采纳率",
            value: formatMetricValue(valueReturnStats?.acceptRate),
            suffix: "%",
            description: formatCountDescription(
              "采纳数",
              valueReturnStats?.acceptCount,
              valueReturnStats?.listCount,
            ),
          },
          {
            label: "接触率",
            value: formatMetricValue(valueReturnStats?.contactRate),
            suffix: "%",
            description: formatCountDescription(
              "接触数",
              valueReturnStats?.contactCount,
              valueReturnStats?.listCount,
            ),
          },
        ],
      },
      {
        title: "转化",
        description: "客户经营结果",
        metrics: [
          {
            label: "AUM 提升金额",
            ...formatAmount(valueReturnStats?.aumIncrease),
            description: "接触后客户资产提升总额",
          },
          {
            label: "财富产品购买金额",
            ...formatAmount(valueReturnStats?.wealthProductAmount),
            description: "接触后客户购买财富产品总额",
          },
        ],
      },
    ],
    depthItems: exposureStats
      ? [...exposureStats.items]
          .sort((left, right) => left.sequence - right.sequence)
          .map((item) => ({
            id: item.targetId,
            sequence: item.sequence,
            label: item.targetName || item.targetId,
            value: item.exposureRate || "--",
            exposureCount: item.exposureCount,
            ratePercent: parseExposurePercent(item.exposureRate),
          }))
      : [],
    depthTotalCount: exposureStats?.totalCount,
  };
}
