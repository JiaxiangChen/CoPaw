import { DatePicker } from "antd";
import { Form } from "@agentscope-ai/design";

/** 仅新页面注册日期字段，旧任务表单不改变。 */
export function ManagedDateRangeField({
  inherited = false,
}: {
  inherited?: boolean;
}) {
  return (
    <Form.Item
      name="runDates"
      label="可执行时间区间"
      extra={
        inherited
          ? "继承父任务，请在父任务修改"
          : "可不填，默认不限日期段；按北京时间包含首尾两天"
      }
    >
      <DatePicker.RangePicker
        disabled={inherited}
        style={{ width: "100%" }}
        format="YYYY-MM-DD"
        placeholder={["开始日期", "结束日期"]}
      />
    </Form.Item>
  );
}
