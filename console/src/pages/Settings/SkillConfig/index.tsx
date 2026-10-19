/**
 * 文件不一致！！！！！
 * ！！！！！！！！！！
 * ！！！！！！！！！！
 */
import { useEffect, useMemo, useRef, useState } from "react";
import {
  Button,
  Empty,
  Form,
  Input,
  InputNumber,
  Result,
  Select,
  Skeleton,
  Switch,
  Tooltip,
  type RefSelectProps,
} from "antd";
import { CircleX, Plus, RefreshCw, Settings2, SquarePen } from "lucide-react";
import { mySkillsApi, type SweSkillListItem } from "@/api/modules/mySkills";
import {
  buildSkillConfigCreatePayload,
  buildSkillConfigUpdatePayload,
  skillConfigApi,
  type ActivityClassItem,
  type SkillConfigFormValues,
  type SkillConfigItem,
} from "@/api/modules/skillConfig";
import { getBbkDisplayName } from "@/constants/bbk";
import { DEFAULT_BBK_ID, DEFAULT_SOURCE_ID } from "@/constants/identity";
import { useAppMessage } from "@/hooks/useAppMessage";
import { useIframeStore } from "@/stores/iframeStore";
import {
  buildSkillInspectionData,
  type SkillInspectionData,
} from "./inspectionData";
import styles from "./index.module.less";

type EditorMode = "view" | "create" | "edit";

interface SkillConfigEditorValues extends SkillConfigFormValues {
  selectedSkillId?: string;
}

const DEFAULT_FORM_VALUES: SkillConfigEditorValues = {
  selectedSkillId: undefined,
  skillId: "",
  name: "",
  sort: 1,
  groupId: undefined,
  businessCenterEnabled: false,
  customerInsightEnabled: false,
  outboundCallEnabled: false,
};

const EDITOR_MODE_META = {
  view: {
    label: "查看模式",
    description: "当前内容仅供查看，点击左侧编辑图标后可修改。",
  },
  create: {
    label: "创建模式",
    description: "请选择名称并完成触发规则配置，然后点击保存。",
  },
  edit: {
    label: "编辑模式",
    description: "当前触发规则可以修改，完成后请点击保存。",
  },
} as const;

function getErrorStatus(error: unknown): number | undefined {
  return error instanceof Error
    ? (error as Error & { status?: number }).status
    : undefined;
}

function toEditorValues(item: SkillConfigItem): SkillConfigEditorValues {
  return {
    selectedSkillId: item.skillId,
    skillId: item.skillId,
    name: item.name,
    sort: Math.max(1, item.sort || 1),
    groupId: item.groupId,
    businessCenterEnabled: item.businessCenterEnabled,
    customerInsightEnabled: item.customerInsightEnabled,
    outboundCallEnabled: item.outboundCallEnabled,
  };
}

function SkillList({
  items,
  selectedId,
  onSelect,
  onEdit,
  onCreate,
  onRefresh,
  refreshing,
}: {
  items: SkillConfigItem[];
  selectedId: string | null;
  onSelect: (item: SkillConfigItem) => void;
  onEdit: (item: SkillConfigItem) => void;
  onCreate: () => void;
  onRefresh: () => void;
  refreshing: boolean;
}) {
  return (
    <aside className={styles.skillListPanel} aria-label="Skill 列表">
      <div className={styles.skillListHeader}>
        <div className={styles.panelHeader}>
          <h2>Skill 列表</h2>
          <div className={styles.skillListActions}>
            <Tooltip title="刷新列表">
              <Button
                className={styles.refreshButton}
                type="text"
                size="small"
                icon={<RefreshCw size={15} />}
                loading={refreshing}
                onClick={onRefresh}
                aria-label="刷新 Skill 列表"
              />
            </Tooltip>
            {items.length ? (
              <Button type="primary" size="small" onClick={onCreate}>
                新增
              </Button>
            ) : null}
          </div>
        </div>
        {items.length ? (
          <span className={styles.skillListHint}>
            <SquarePen size={12} aria-hidden="true" />
            悬停条目可编辑
          </span>
        ) : null}
      </div>
      <div
        className={styles.skillList}
        role="region"
        aria-label="Skill 列表内容"
        tabIndex={items.length ? 0 : undefined}
      >
        {items.length ? (
          items.map((item) => {
            const selected = selectedId === item.skillId;
            return (
              <div
                key={item.skillId}
                className={`${styles.skillRow}${
                  selected ? ` ${styles.skillRowSelected}` : ""
                }`}
              >
                <button
                  type="button"
                  className={styles.skillSelectButton}
                  onClick={() => onSelect(item)}
                  aria-current={selected ? "page" : undefined}
                >
                  <span
                    className={`${styles.statusDot}${
                      item.enabled ? ` ${styles.statusDotEnabled}` : ""
                    }`}
                    aria-hidden="true"
                  />
                  <span className={styles.skillName} title={item.name}>
                    {item.name || item.skillId}
                  </span>
                </button>
                <Tooltip title={`编辑 ${item.name || item.skillId}`}>
                  <button
                    type="button"
                    className={styles.editIconButton}
                    onClick={() => onEdit(item)}
                    aria-label={`编辑 ${item.name || item.skillId}`}
                  >
                    <SquarePen size={15} />
                  </button>
                </Tooltip>
              </div>
            );
          })
        ) : (
          <div className={styles.skillListEmpty} role="status">
            <strong>暂无 Skill 数据</strong>
            <span>完成右侧配置并创建后，将显示在这里。</span>
          </div>
        )}
      </div>
    </aside>
  );
}

function InspectionPanel({ data }: { data: SkillInspectionData | null }) {
  return (
    <section
      className={styles.inspectionPanel}
      aria-labelledby="inspection-title"
    >
      <div className={styles.sectionHeading}>
        <span className={styles.sectionMarker} aria-hidden="true" />
        <h2 id="inspection-title">回检</h2>
      </div>
      {data?.sections.map((section, index) => (
        <div className={styles.inspectionSection} key={section.title}>
          <div className={styles.inspectionSectionTitle}>
            <span className={styles.stepBadge}>{index + 1}</span>
            <strong>{section.title}</strong>
            <span>{section.description}</span>
          </div>
          <div className={styles.metricGrid}>
            {section.metrics.map((metric) => (
              <div className={styles.metricCard} key={metric.label}>
                <span className={styles.metricLabel}>{metric.label}</span>
                <div className={styles.metricValue}>
                  {metric.value}
                  {metric.suffix ? <small>{metric.suffix}</small> : null}
                </div>
                <span className={styles.metricDescription}>
                  {metric.description}
                </span>
              </div>
            ))}
          </div>
        </div>
      ))}
      <div className={styles.depthSection}>
        <div className={styles.sectionHeading}>
          <span className={styles.sectionMarker} aria-hidden="true" />
          <h3>L2 客户级方案模块滚动深度</h3>
        </div>
        <div
          className={styles.depthList}
          role="list"
          aria-label="客户级方案模块滚动深度"
        >
          {data?.depthItems.map((item, index) => (
            <div
              className={styles.depthRow}
              key={item.id || item.label}
              role="listitem"
              aria-label={
                item.exposureCount === undefined
                  ? undefined
                  : `${item.label}：曝光 ${item.exposureCount} 次，占比 ${item.value}`
              }
            >
              <span className={styles.depthIndex}>
                {item.sequence ?? index + 1}
              </span>
              <span className={styles.depthLabel}>{item.label}</span>
              <strong>{item.value}</strong>
              <span className={styles.depthTrack} aria-hidden="true">
                <span
                  className={styles.depthTrackFill}
                  style={{ width: `${item.ratePercent ?? 0}%` }}
                />
              </span>
            </div>
          ))}
        </div>
        <p className={styles.placeholderHint}>
          {data?.depthTotalCount === undefined
            ? "客户级方案模块滚动深度暂无数据"
            : `曝光总数 ${data.depthTotalCount}`}
        </p>
      </div>
    </section>
  );
}

export default function SkillConfigPage() {
  const { message } = useAppMessage();
  const bbkId = useIframeStore((state) => state.bbk) || DEFAULT_BBK_ID;
  const sourceId = useIframeStore((state) => state.source) || DEFAULT_SOURCE_ID;
  const [form] = Form.useForm<SkillConfigEditorValues>();
  const nameSelectRef = useRef<RefSelectProps>(null);
  const [configs, setConfigs] = useState<SkillConfigItem[]>([]);
  const [marketSkills, setMarketSkills] = useState<SweSkillListItem[]>([]);
  const [activityClasses, setActivityClasses] = useState<ActivityClassItem[]>(
    [],
  );
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedConfig, setSelectedConfig] = useState<SkillConfigItem | null>(
    null,
  );
  const [inspection, setInspection] = useState<SkillInspectionData | null>(
    null,
  );
  const [mode, setMode] = useState<EditorMode>("view");
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [detailReloadKey, setDetailReloadKey] = useState(0);
  const [detailLoading, setDetailLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [saving, setSaving] = useState(false);
  const modeMeta = EDITOR_MODE_META[mode];

  useEffect(() => {
    if (mode === "create") {
      nameSelectRef.current?.focus();
    }
  }, [mode]);

  const skillNameOptions = useMemo(() => {
    const options = marketSkills
      .filter((skill) => skill.skill_id)
      .map((skill) => ({
        value: skill.skill_id,
        label: skill.cn_name || skill.skill_name || skill.skill_id,
      }));
    if (
      selectedConfig &&
      !options.some((option) => option.value === selectedConfig.skillId)
    ) {
      options.unshift({
        value: selectedConfig.skillId,
        label: selectedConfig.name || selectedConfig.skillId,
      });
    }
    return options;
  }, [marketSkills, selectedConfig]);

  const groupOptions = useMemo(() => {
    const options = [...activityClasses]
      .sort(
        (left, right) =>
          Number(left.displayOrder || 0) - Number(right.displayOrder || 0),
      )
      .map((item) => ({
        value: item.activityClassId,
        label: item.activityClassName || item.activityClassId,
      }));
    if (
      selectedConfig?.groupId &&
      !options.some((option) => option.value === selectedConfig.groupId)
    ) {
      options.unshift({
        value: selectedConfig.groupId,
        label: selectedConfig.groupName || selectedConfig.groupId,
      });
    }
    return options;
  }, [activityClasses, selectedConfig]);

  const loadConfigs = async () => {
    const nextConfigs = await skillConfigApi.listSkillConfigs(bbkId);
    setConfigs(nextConfigs);
    return nextConfigs;
  };

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setListError(null);
    Promise.allSettled([
      skillConfigApi.listSkillConfigs(bbkId),
      mySkillsApi.listSweSkills(sourceId),
      skillConfigApi.listActivityClasses(bbkId),
    ])
      .then(([configResult, skillResult, activityClassResult]) => {
        if (cancelled) return;
        if (configResult.status === "fulfilled") {
          setConfigs(configResult.value);
          if (configResult.value.length) {
            setSelectedId(configResult.value[0].skillId);
          }
        } else if (getErrorStatus(configResult.reason) === 500) {
          setConfigs([]);
          setSelectedId(null);
          setSelectedConfig(null);
          setInspection(null);
          setMode("view");
        } else {
          const errorMessage =
            configResult.reason instanceof Error
              ? configResult.reason.message
              : "请稍后重试";
          setListError(errorMessage);
          message.error(errorMessage);
        }
        if (skillResult.status === "fulfilled") {
          setMarketSkills(skillResult.value.skills ?? []);
        } else {
          setMarketSkills([]);
          message.error("Skill 名称列表加载失败");
        }
        if (activityClassResult.status === "fulfilled") {
          setActivityClasses(activityClassResult.value);
        } else {
          setActivityClasses([]);
          message.error(
            activityClassResult.reason instanceof Error
              ? activityClassResult.reason.message
              : "所属分组列表加载失败",
          );
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [bbkId, message, reloadKey, sourceId]);

  useEffect(() => {
    if (!selectedId || mode === "create") return;
    let cancelled = false;
    setDetailLoading(true);
    const inspectionPromise = Promise.allSettled([
      skillConfigApi.getPreviewStats(selectedId, bbkId),
      skillConfigApi.getValueReturnStats(selectedId, bbkId),
      skillConfigApi.getSkillExposureStats(selectedId, bbkId),
    ]).then(([previewResult, valueReturnResult, exposureResult]) =>
      buildSkillInspectionData(
        previewResult.status === "fulfilled" ? previewResult.value : undefined,
        valueReturnResult.status === "fulfilled"
          ? valueReturnResult.value
          : undefined,
        exposureResult.status === "fulfilled"
          ? exposureResult.value
          : undefined,
      ),
    );
    Promise.all([
      skillConfigApi.getSkillConfigDetail(selectedId, bbkId),
      inspectionPromise,
    ])
      .then(([detail, inspectionData]) => {
        if (cancelled) return;
        setSelectedConfig(detail);
        setInspection(inspectionData);
        form.setFieldsValue(toEditorValues(detail));
      })
      .catch((error) => {
        if (!cancelled) {
          message.error(
            error instanceof Error ? error.message : "Skill 详情加载失败",
          );
        }
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [bbkId, detailReloadKey, form, message, mode, selectedId]);

  const handleCreate = () => {
    setMode("create");
    setSelectedId(null);
    setSelectedConfig(null);
    form.setFieldsValue(DEFAULT_FORM_VALUES);
    setInspection(buildSkillInspectionData());
  };

  const handleSelect = (item: SkillConfigItem) => {
    setMode("view");
    setSelectedId(item.skillId);
  };

  const handleEdit = (item: SkillConfigItem) => {
    setMode("edit");
    setSelectedId(item.skillId);
  };

  const handleRefresh = async () => {
    setRefreshing(true);
    try {
      const nextConfigs = await loadConfigs();
      const firstConfig = nextConfigs[0];
      if (firstConfig) {
        setMode("view");
        setSelectedId(firstConfig.skillId);
        setSelectedConfig(null);
        setInspection(null);
        setDetailReloadKey((key) => key + 1);
      } else if (mode !== "create") {
        setSelectedId(null);
        setSelectedConfig(null);
        setInspection(null);
      }
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : "Skill 列表刷新失败",
      );
    } finally {
      setRefreshing(false);
    }
  };

  const handleSkillChange = (skillId: string) => {
    const skill = marketSkills.find((item) => item.skill_id === skillId);
    form.setFieldsValue({
      skillId,
      name: skill?.cn_name || skill?.skill_name || skillId,
    });
  };

  const handleCancel = () => {
    if (mode === "create") {
      if (!configs.length) {
        setMode("view");
        setInspection(null);
        return;
      }
      setSelectedId(configs[0].skillId);
    } else if (selectedConfig) {
      form.setFieldsValue(toEditorValues(selectedConfig));
    }
    setMode("view");
  };

  const handleSave = async () => {
    const values = await form.validateFields();
    const selectedMarketSkill = marketSkills.find(
      (item) => item.skill_id === values.skillId,
    );
    const valuesForSubmit = {
      ...values,
      name:
        selectedMarketSkill?.cn_name ||
        selectedMarketSkill?.skill_name ||
        values.name,
    };
    setSaving(true);
    try {
      const groupName = groupOptions.find(
        (option) => option.value === values.groupId,
      )?.label;
      if (mode === "create") {
        const bbkName =
          configs.find((item) => item.bbkId === bbkId)?.bbkName ||
          getBbkDisplayName(bbkId);
        const createPayload = buildSkillConfigCreatePayload(
          valuesForSubmit,
          bbkId,
          bbkName,
          groupName,
        );
        const createdConfig = await skillConfigApi.createSkillConfig(
          createPayload,
        );
        setSelectedConfig(createdConfig);
        form.setFieldsValue(toEditorValues(createdConfig));
        message.success("Skill 触发规则创建成功");
      } else {
        const updatePayload = buildSkillConfigUpdatePayload(
          valuesForSubmit,
          selectedConfig ?? undefined,
          bbkId,
          groupName,
        );
        const updatedConfig = await skillConfigApi.updateSkillConfig(
          updatePayload,
        );
        setSelectedConfig(updatedConfig);
        form.setFieldsValue(toEditorValues(updatedConfig));
        message.success("Skill 触发规则更新成功");
      }
    } catch (error) {
      if (error instanceof Error) message.error(error.message);
      setSaving(false);
      return;
    }

    setMode("view");
    setSelectedId(values.skillId);
    try {
      const nextConfigs = await loadConfigs();
      setSelectedId(values.skillId || nextConfigs[0]?.skillId || null);
    } catch {
      message.warning("保存成功，但 Skill 列表刷新失败，请稍后重试");
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className={styles.page}>
        <div className={styles.loadingState}>
          <Skeleton active />
        </div>
      </div>
    );
  }

  if (listError) {
    return (
      <div className={styles.page}>
        <div className={styles.errorState}>
          <Result
            className={styles.errorResult}
            status="error"
            icon={
              <CircleX
                className={styles.errorIcon}
                size={44}
                strokeWidth={1.75}
                aria-hidden="true"
              />
            }
            title="Skill 配置加载失败"
            subTitle={listError}
            extra={
              <Button
                type="primary"
                onClick={() => setReloadKey((key) => key + 1)}
              >
                重新加载
              </Button>
            }
          />
        </div>
      </div>
    );
  }

  const showWorkspace =
    configs.length > 0 || mode === "create" || selectedId !== null;

  return (
    <div className={styles.page}>
      {!showWorkspace ? (
        <div className={styles.emptyState}>
          <Empty
            image={<Settings2 size={52} strokeWidth={1.35} />}
            description={
              <div className={styles.emptyCopy}>
                <strong>暂无 Skill 配置</strong>
                <span>创建第一条触发规则后，可在这里查看和维护回检配置。</span>
              </div>
            }
          >
            <Button
              type="primary"
              size="large"
              icon={<Plus size={17} />}
              onClick={handleCreate}
            >
              新增 Skill
            </Button>
          </Empty>
        </div>
      ) : (
        <div className={styles.workspace}>
          <SkillList
            items={configs}
            selectedId={selectedId}
            onSelect={handleSelect}
            onEdit={handleEdit}
            onCreate={handleCreate}
            onRefresh={handleRefresh}
            refreshing={refreshing}
          />
          <section className={styles.rulePanel} aria-labelledby="rule-title">
            <div className={styles.rulePanelHeader}>
              <div className={styles.sectionHeading}>
                <span className={styles.sectionMarker} aria-hidden="true" />
                <h2 id="rule-title">Skill 触发规则</h2>
              </div>
              {mode !== "view" ? (
                <div className={styles.formActions}>
                  <Button size="small" onClick={handleCancel}>
                    取消
                  </Button>
                  <Button
                    type="primary"
                    size="small"
                    loading={saving}
                    onClick={handleSave}
                  >
                    保存
                  </Button>
                </div>
              ) : null}
            </div>
            <div
              className={`${styles.modeNotice} ${styles[`modeNotice${mode}`]}`}
              role="status"
            >
              <span className={styles.modeBadge}>{modeMeta.label}</span>
              <span>{modeMeta.description}</span>
            </div>
            {detailLoading ? (
              <Skeleton active paragraph={{ rows: 8 }} />
            ) : (
              <Form
                form={form}
                layout="vertical"
                disabled={mode === "view"}
                requiredMark={false}
                className={styles.ruleForm}
              >
                <Form.Item
                  name="selectedSkillId"
                  label="Skill 名称"
                  rules={[{ required: true, message: "请选择 Skill 名称" }]}
                >
                  <Select
                    ref={nameSelectRef}
                    disabled={mode !== "create"}
                    showSearch
                    optionFilterProp="label"
                    options={skillNameOptions}
                    placeholder="请选择 Skill 名称"
                    onChange={handleSkillChange}
                  />
                </Form.Item>
                <Form.Item name="skillId" label="Skill ID">
                  <Input
                    disabled
                    className={styles.skillIdInput}
                    placeholder="选择名称后自动生成"
                  />
                </Form.Item>
                <Form.Item name="name" hidden>
                  <Input />
                </Form.Item>
                <div className={styles.sortRow}>
                  <Form.Item
                    name="sort"
                    label="排序"
                    rules={[
                      {
                        type: "number",
                        min: 1,
                        max: 9999,
                        message: "排序值请输入 1 至 9999",
                      },
                    ]}
                  >
                    <InputNumber min={1} max={9999} precision={0} />
                  </Form.Item>
                  <span>数值越小越优先</span>
                </div>
                <div className={styles.ruleDivider} />
                <Form.Item
                  name="businessCenterEnabled"
                  label="商机中心"
                  valuePropName="checked"
                  className={styles.switchField}
                >
                  <Switch size="small" />
                </Form.Item>
                <div className={styles.groupField}>
                  <Form.Item name="groupId" label="所属分组">
                    <Select
                      allowClear
                      options={groupOptions}
                      placeholder="请选择所属分组"
                    />
                  </Form.Item>
                </div>
                <Form.Item
                  name="customerInsightEnabled"
                  label="客户洞察"
                  valuePropName="checked"
                  className={styles.switchField}
                >
                  <Switch size="small" />
                </Form.Item>
                <Form.Item
                  name="outboundCallEnabled"
                  label="电访"
                  valuePropName="checked"
                  className={styles.switchField}
                >
                  <Switch size="small" />
                </Form.Item>
              </Form>
            )}
          </section>
          <InspectionPanel data={inspection} />
        </div>
      )}
    </div>
  );
}
