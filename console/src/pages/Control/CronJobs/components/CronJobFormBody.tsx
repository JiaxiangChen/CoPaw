import {
  Checkbox,
  Form,
  Input,
  InputNumber,
  Select,
  Switch,
} from "@agentscope-ai/design";
import { TimePicker } from "antd";
import type { FormInstance } from "antd";
import { useTranslation } from "react-i18next";
import type { ExecutionModelOption } from "@/hooks/useExecutionModelOptions";
import { DEFAULT_EXECUTION_MODEL_KEY } from "@/hooks/useExecutionModelOptions";
import { normalizeSkillIdsInput, type CronJobFormValues } from "../helpers";
import { TIMEZONE_OPTIONS } from "./constants";
import styles from "../index.module.less";

type SelectOption = { value: string; label: string };

interface CronJobFormBodyProps {
  form: FormInstance<CronJobFormValues>;
  executionModelOptions: ExecutionModelOption[];
  executionModelLoading: boolean;
  tenantDefaultModelLabel: string;
  skillOptions?: SelectOption[];
  skillOptionsLoading?: boolean;
  idDisabled?: boolean;
  /** 新版编辑将启停作为独立管理操作；默认保留旧表单行为。 */
  enabledDisabled?: boolean;
}

export function CronJobFormBody({
  form,
  executionModelOptions,
  executionModelLoading,
  tenantDefaultModelLabel,
  skillOptions = [],
  skillOptionsLoading = false,
  idDisabled = false,
  enabledDisabled = false,
}: CronJobFormBodyProps) {
  const { t } = useTranslation();
  const validateSkillIds = (_: unknown, value: unknown) => {
    try {
      normalizeSkillIdsInput(value);
      return Promise.resolve();
    } catch (error) {
      return Promise.reject(
        error instanceof Error ? error : new Error("绑定技能ID格式不正确"),
      );
    }
  };
  const skillOptionsKey = skillOptions.map((option) => option.value).join("|");

  return (
    <>
      <Form.Item
        name="id"
        label={t("cronJobs.id")}
        rules={[{ required: true, message: t("cronJobs.pleaseInputId") }]}
        tooltip={t("cronJobs.idTooltip")}
      >
        <Input
          disabled={idDisabled}
          placeholder={t("cronJobs.jobIdPlaceholder")}
        />
      </Form.Item>

      <Form.Item
        name="name"
        label={t("cronJobs.name")}
        rules={[{ required: true, message: t("cronJobs.pleaseInputName") }]}
        tooltip={t("cronJobs.nameTooltip")}
      >
        <Input placeholder={t("cronJobs.jobNamePlaceholder")} />
      </Form.Item>

      <Form.Item
        name="enabled"
        label={t("cronJobs.enabled")}
        valuePropName="checked"
      >
        <Switch disabled={enabledDisabled} />
      </Form.Item>

      <Form.Item name={["schedule", "type"]} label="ScheduleType" hidden>
        <Input disabled value="cron" />
      </Form.Item>

      <Form.Item
        label={t("cronJobs.scheduleCronLabel")}
        required
        tooltip={t("cronJobs.cronTooltip")}
      >
        <Form.Item name="cronType" noStyle>
          <Select>
            <Select.Option value="hourly">
              {t("cronJobs.cronTypeHourly")}
            </Select.Option>
            <Select.Option value="daily">
              {t("cronJobs.cronTypeDaily")}
            </Select.Option>
            <Select.Option value="weekly">
              {t("cronJobs.cronTypeWeekly")}
            </Select.Option>
            <Select.Option value="custom">
              {t("cronJobs.cronTypeCustom")}
            </Select.Option>
          </Select>
        </Form.Item>
      </Form.Item>

      <Form.Item
        noStyle
        shouldUpdate={(prev, cur) => prev.cronType !== cur.cronType}
      >
        {({ getFieldValue }) => {
          const cronType = getFieldValue("cronType");

          if (cronType === "daily" || cronType === "weekly") {
            return (
              <Form.Item
                name="cronTime"
                label={t("cronJobs.cronTime")}
                rules={[{ required: true }]}
              >
                <TimePicker
                  format="HH:mm"
                  minuteStep={15}
                  needConfirm={false}
                  style={{ width: "100%" }}
                />
              </Form.Item>
            );
          }
          return null;
        }}
      </Form.Item>

      <Form.Item
        noStyle
        shouldUpdate={(prev, cur) => prev.cronType !== cur.cronType}
      >
        {({ getFieldValue }) => {
          const cronType = getFieldValue("cronType");

          if (cronType === "weekly") {
            return (
              <Form.Item
                name="cronDaysOfWeek"
                label={t("cronJobs.cronDaysOfWeek")}
                rules={[{ required: true, message: "请选择至少一天" }]}
              >
                <Checkbox.Group
                  options={[
                    { label: t("cronJobs.cronDayMon"), value: "mon" },
                    { label: t("cronJobs.cronDayTue"), value: "tue" },
                    { label: t("cronJobs.cronDayWed"), value: "wed" },
                    { label: t("cronJobs.cronDayThu"), value: "thu" },
                    { label: t("cronJobs.cronDayFri"), value: "fri" },
                    { label: t("cronJobs.cronDaySat"), value: "sat" },
                    { label: t("cronJobs.cronDaySun"), value: "sun" },
                  ]}
                />
              </Form.Item>
            );
          }
          return null;
        }}
      </Form.Item>

      <Form.Item
        noStyle
        shouldUpdate={(prev, cur) => prev.cronType !== cur.cronType}
      >
        {({ getFieldValue }) => {
          const cronType = getFieldValue("cronType");

          if (cronType === "custom") {
            return (
              <Form.Item
                name="cronCustom"
                label={t("cronJobs.cronCustomExpression")}
                rules={[
                  { required: true, message: t("cronJobs.pleaseInputCron") },
                ]}
                extra={
                  <div className={styles.formExtraText}>
                    <div style={{ marginBottom: 4 }}>
                      {t("cronJobs.cronExample")}
                    </div>
                    {/* <div>
                      {t("cronJobs.cronHelper")}{" "}
                      <a
                        href="https://crontab.guru/"
                        target="_blank"
                        rel="noopener noreferrer"
                        className={styles.formHelperLink}
                      >
                        {t("cronJobs.cronHelperLink")} →
                      </a>
                    </div> */}
                  </div>
                }
              >
                <Input placeholder="0 9 * * *" />
              </Form.Item>
            );
          }
          return null;
        }}
      </Form.Item>

      <Form.Item name={["schedule", "cron"]} hidden>
        <Input />
      </Form.Item>

      <Form.Item
        name={["schedule", "timezone"]}
        label={t("cronJobs.scheduleTimezone")}
        tooltip={t("cronJobs.timezoneTooltip")}
      >
        <Select
          showSearch
          placeholder={t("cronJobs.selectTimezone")}
          filterOption={(input, option) =>
            (option?.label?.toString() || "")
              .toLowerCase()
              .includes(input.toLowerCase())
          }
          options={TIMEZONE_OPTIONS}
        />
      </Form.Item>

      <Form.Item label="通知延迟">
        <div className={styles.inlineFieldGroup}>
          <Form.Item name="notificationDelayValue" noStyle>
            <InputNumber min={0} style={{ flex: 1 }} placeholder="0" />
          </Form.Item>
          <Form.Item name="notificationDelayUnit" noStyle>
            <Select style={{ width: 140 }}>
              <Select.Option value="minutes">分钟</Select.Option>
              <Select.Option value="hours">小时</Select.Option>
            </Select>
          </Form.Item>
        </div>
      </Form.Item>

      <Form.Item
        name="task_type"
        label={t("cronJobs.taskType")}
        rules={[
          { required: true, message: t("cronJobs.pleaseSelectTaskType") },
        ]}
        tooltip={t("cronJobs.taskTypeTooltip")}
      >
        <Select
          onChange={(value) => {
            if (value !== "agent") {
              form.setFieldValue(
                "execution_model_key",
                DEFAULT_EXECUTION_MODEL_KEY,
              );
            }
          }}
        >
          <Select.Option value="text">text</Select.Option>
          <Select.Option value="agent">agent</Select.Option>
          <Select.Option value="workflow">技能任务</Select.Option>
        </Select>
      </Form.Item>

      <Form.Item
        noStyle
        shouldUpdate={(prev, cur) => prev.task_type !== cur.task_type}
      >
        {({ getFieldValue }) => {
          const workflow = getFieldValue("task_type") === "workflow";
          return (
            <Form.Item
              name="skillIds"
              label="绑定技能ID"
              required={workflow}
              extra={workflow ? "将按第一个绑定技能执行" : undefined}
              rules={[
                {
                  validator: async (_, value) => {
                    await validateSkillIds(_, value);
                    if (
                      workflow &&
                      (!Array.isArray(value) || value.length === 0)
                    ) {
                      throw new Error("技能任务需要选择技能");
                    }
                  },
                },
              ]}
            >
              <Select
                key={skillOptionsKey}
                mode="multiple"
                allowClear
                showSearch
                loading={skillOptionsLoading}
                placeholder="请选择绑定技能ID"
                optionFilterProp="label"
                options={skillOptions}
              />
            </Form.Item>
          );
        }}
      </Form.Item>

      <Form.Item
        noStyle
        shouldUpdate={(prev, cur) => prev.task_type !== cur.task_type}
      >
        {({ getFieldValue }) => {
          const taskType = getFieldValue("task_type");
          const textRequired = taskType === "text";
          const agentRequired = taskType === "agent";

          return (
            <>
              {taskType === "agent" && (
                <Form.Item
                  name="execution_model_key"
                  label="执行模型"
                  extra={
                    <span className={styles.formExtraText}>
                      未指定时跟随当前租户默认模型
                    </span>
                  }
                >
                  <Select loading={executionModelLoading}>
                    <Select.Option value={DEFAULT_EXECUTION_MODEL_KEY}>
                      {tenantDefaultModelLabel}
                    </Select.Option>
                    {executionModelOptions.map((option) => (
                      <Select.Option key={option.key} value={option.key}>
                        {option.label}
                      </Select.Option>
                    ))}
                  </Select>
                </Form.Item>
              )}

              {taskType === "text" && (
                <Form.Item
                  name="text"
                  label={t("cronJobs.text")}
                  required={textRequired}
                  rules={
                    textRequired
                      ? [
                          {
                            required: true,
                            message: t("cronJobs.pleaseInputMessageContent"),
                          },
                        ]
                      : []
                  }
                  tooltip={t("cronJobs.textTooltip")}
                >
                  <Input.TextArea
                    rows={3}
                    placeholder={t("cronJobs.taskDescriptionPlaceholder")}
                  />
                </Form.Item>
              )}

              {taskType === "agent" && (
                <Form.Item
                  name={["request", "input"]}
                  label={t("cronJobs.requestInput")}
                  required={agentRequired}
                  rules={[
                    ...(agentRequired
                      ? [
                          {
                            required: true,
                            message: t("cronJobs.pleaseInputRequest"),
                          },
                        ]
                      : []),
                    {
                      validator: (_, value) => {
                        if (!value) return Promise.resolve();
                        try {
                          JSON.parse(value);
                          return Promise.resolve();
                        } catch {
                          return Promise.reject(
                            new Error(t("cronJobs.invalidJsonFormat")),
                          );
                        }
                      },
                    },
                  ]}
                  tooltip={t("cronJobs.requestInputTooltip")}
                  extra={
                    <span className={styles.formExtraText}>
                      {t("cronJobs.requestInputExample")}
                    </span>
                  }
                >
                  <Input.TextArea
                    rows={6}
                    placeholder='[{"role":"user","content":[{"text":"Hello","type":"text"}]}]'
                    style={{ fontFamily: "monospace", fontSize: 12 }}
                  />
                </Form.Item>
              )}
            </>
          );
        }}
      </Form.Item>

      <Form.Item
        noStyle
        shouldUpdate={(prev, cur) => prev.task_type !== cur.task_type}
      >
        {({ getFieldValue }) =>
          getFieldValue("task_type") !== "workflow" ? (
            <>
              <Form.Item
                name={["request", "session_id"]}
                label={t("cronJobs.requestSessionId")}
                tooltip={t("cronJobs.requestSessionIdTooltip")}
              >
                <Input placeholder="default" />
              </Form.Item>
              <Form.Item
                name={["request", "user_id"]}
                label={t("cronJobs.requestUserId")}
                tooltip={t("cronJobs.requestUserIdTooltip")}
              >
                <Input placeholder="system" />
              </Form.Item>
            </>
          ) : null
        }
      </Form.Item>

      <Form.Item name={["dispatch", "type"]} label="DispatchType" hidden>
        <Input disabled value="channel" />
      </Form.Item>

      <Form.Item
        name={["dispatch", "channel"]}
        label={t("cronJobs.dispatchChannel")}
        rules={[{ required: true, message: t("cronJobs.pleaseInputChannel") }]}
        tooltip={t("cronJobs.dispatchChannelTooltip")}
      >
        <Input placeholder="console" />
      </Form.Item>

      <Form.Item
        name={["dispatch", "target", "user_id"]}
        label={t("cronJobs.dispatchTargetUserId")}
        rules={[{ required: true, message: t("cronJobs.pleaseInputUserId") }]}
        tooltip={t("cronJobs.dispatchTargetUserIdTooltip")}
      >
        <Input placeholder="admin" />
      </Form.Item>

      <Form.Item
        name={["dispatch", "target", "session_id"]}
        label={t("cronJobs.dispatchTargetSessionId")}
        rules={[
          { required: true, message: t("cronJobs.pleaseInputSessionId") },
        ]}
        tooltip={t("cronJobs.dispatchTargetSessionIdTooltip")}
      >
        <Input placeholder="default" />
      </Form.Item>

      <Form.Item
        name={["dispatch", "mode"]}
        label={t("cronJobs.dispatchMode")}
        tooltip={t("cronJobs.dispatchModeTooltip")}
      >
        <Select>
          <Select.Option value="stream">stream</Select.Option>
          <Select.Option value="final">final</Select.Option>
        </Select>
      </Form.Item>

      <Form.Item
        name={["runtime", "max_concurrency"]}
        label={t("cronJobs.runtimeMaxConcurrency")}
        tooltip={t("cronJobs.maxConcurrencyTooltip")}
      >
        <InputNumber min={1} style={{ width: "100%" }} placeholder="1" />
      </Form.Item>

      <Form.Item
        name={["runtime", "timeout_seconds"]}
        label={t("cronJobs.runtimeTimeoutSeconds")}
        tooltip={t("cronJobs.timeoutSecondsTooltip")}
      >
        <InputNumber min={1} style={{ width: "100%" }} placeholder="300" />
      </Form.Item>

      <Form.Item
        name={["runtime", "misfire_grace_seconds"]}
        label={t("cronJobs.runtimeMisfireGraceSeconds")}
        tooltip={t("cronJobs.misfireGraceSecondsTooltip")}
      >
        <InputNumber min={0} style={{ width: "100%" }} placeholder="60" />
      </Form.Item>
    </>
  );
}
