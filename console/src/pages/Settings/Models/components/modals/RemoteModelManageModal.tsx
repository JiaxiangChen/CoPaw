import { useState, useEffect } from "react";
import {
  Button,
  Checkbox,
  Form,
  Input,
  InputNumber,
  Modal,
  Switch,
  Tag,
} from "@agentscope-ai/design";
import {
  DeleteOutlined,
  PlusOutlined,
  ApiOutlined,
  SyncOutlined,
  EyeOutlined,
  SettingOutlined,
} from "@ant-design/icons";
import type { ProviderInfo } from "../../../../../api/types";
import api from "../../../../../api";
import { useTranslation } from "react-i18next";
import { useTheme } from "../../../../../contexts/ThemeContext";
import { useAppMessage } from "../../../../../hooks/useAppMessage";
import styles from "../../index.module.less";

interface RemoteModelManageModalProps {
  provider: ProviderInfo;
  open: boolean;
  onClose: () => void;
  onSaved: () => void | Promise<void>;
}

export function RemoteModelManageModal({
  provider,
  open,
  onClose,
  onSaved,
}: RemoteModelManageModalProps) {
  const { t } = useTranslation();
  const { isDark } = useTheme();
  const { message } = useAppMessage();
  const [adding, setAdding] = useState(false);
  const [saving, setSaving] = useState(false);
  const [discovering, setDiscovering] = useState(false);
  const [testingModelId, setTestingModelId] = useState<string | null>(null);
  const [probingModelId, setProbingModelId] = useState<string | null>(null);
  const [configModelId, setConfigModelId] = useState<string | null>(null);
  const [configSaving, setConfigSaving] = useState(false);
  const [form] = Form.useForm();
  const [configForm] = Form.useForm();
  const canDiscover = provider.support_model_discovery;

  // For custom providers ALL models are deletable.
  // For built-in providers only extra_models are deletable.
  const extraModelIds = new Set((provider.extra_models || []).map((m) => m.id));

  const doAddModel = async (id: string, name: string) => {
    await api.addModel(provider.id, { id, name });
    message.success(t("models.modelAdded", { name }));
    form.resetFields();
    setAdding(false);
    onSaved();
  };

  const handleAddModel = async () => {
    try {
      const values = await form.validateFields();
      const id = values.id.trim();
      const name = values.name?.trim() || id;

      // Step 1: Test the model connection first
      setSaving(true);
      const testResult = await api.testModelConnection(provider.id, {
        model_id: id,
      });

      if (!testResult.success) {
        // Test failed – ask user whether to proceed anyway
        setSaving(false);
        Modal.confirm({
          title: t("models.testConnectionFailed"),
          content: t("models.modelTestFailedConfirm", {
            message: testResult.message || t("models.modelTestFailed"),
          }),
          okText: t("models.addModel"),
          cancelText: t("models.cancel"),
          onOk: async () => {
            setSaving(true);
            try {
              await doAddModel(id, name);
            } catch (error) {
              const errMsg =
                error instanceof Error
                  ? error.message
                  : t("models.modelAddFailed");
              message.error(errMsg);
            } finally {
              setSaving(false);
            }
          },
        });
        return;
      }

      // Step 2: If test passed, add the model
      await doAddModel(id, name);
    } catch (error) {
      if (error && typeof error === "object" && "errorFields" in error) return;
      const errMsg =
        error instanceof Error ? error.message : t("models.modelAddFailed");
      message.error(errMsg);
    } finally {
      setSaving(false);
    }
  };

  const handleTestModel = async (modelId: string) => {
    setTestingModelId(modelId);
    try {
      const result = await api.testModelConnection(provider.id, {
        model_id: modelId,
      });
      if (result.success) {
        message.success(result.message || t("models.testConnectionSuccess"));
      } else {
        message.warning(result.message || t("models.testConnectionFailed"));
      }
    } catch (error) {
      const errMsg =
        error instanceof Error
          ? error.message
          : t("models.testConnectionError");
      message.error(errMsg);
    } finally {
      setTestingModelId(null);
    }
  };

  const handleProbeMultimodal = async (modelId: string) => {
    setProbingModelId(modelId);
    try {
      const result = await api.probeMultimodal(provider.id, modelId);
      const parts: string[] = [];
      if (result.supports_image) parts.push(t("models.probeImage", "图片"));
      if (result.supports_video) parts.push(t("models.probeVideo", "视频"));
      if (parts.length > 0) {
        message.success(
          t("models.probeSupported", {
            types: parts.join(", "),
            defaultValue: `支持: ${parts.join(", ")}`,
          }),
        );
      } else {
        message.info(t("models.probeNotSupported", "该模型不支持多模态输入"));
      }
      await onSaved();
    } catch (error) {
      const errMsg =
        error instanceof Error
          ? error.message
          : t("models.probeFailed", "探测失败");
      message.error(errMsg);
    } finally {
      setProbingModelId(null);
    }
  };

  const handleRemoveModel = (modelId: string, modelName: string) => {
    Modal.confirm({
      title: t("models.removeModel"),
      content: t("models.removeModelConfirm", {
        name: modelName,
        provider: provider.name,
      }),
      okText: t("common.delete"),
      okButtonProps: { danger: true },
      cancelText: t("models.cancel"),
      onOk: async () => {
        try {
          await api.removeModel(provider.id, modelId);
          message.success(t("models.modelRemoved", { name: modelName }));
          await onSaved();
        } catch (error) {
          const errMsg =
            error instanceof Error
              ? error.message
              : t("models.modelRemoveFailed");
          message.error(errMsg);
        }
      },
    });
  };

  const openModelConfig = async (modelId: string) => {
    try {
      const config = await api.getModelRuntimeConfig(provider.id, modelId);
      configForm.setFieldsValue({
        ...config,
        supported_reasoning_efforts: config.supported_reasoning_efforts ?? [],
      });
      setConfigModelId(modelId);
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : t("models.failedToSaveConfig"),
      );
    }
  };

  const saveModelConfig = async () => {
    if (!configModelId) return;
    try {
      const values = await configForm.validateFields();
      setConfigSaving(true);
      await api.updateModelRuntimeConfig(provider.id, configModelId, values);
      message.success(t("models.configurationSaved", { name: configModelId }));
      setConfigModelId(null);
      await onSaved();
    } catch (error) {
      if (error && typeof error === "object" && "errorFields" in error) return;
      message.error(
        error instanceof Error ? error.message : t("models.failedToSaveConfig"),
      );
    } finally {
      setConfigSaving(false);
    }
  };

  const handleClose = () => {
    setAdding(false);
    form.resetFields();
    onClose();
  };

  const handleDiscoverModels = async () => {
    setDiscovering(true);
    try {
      const result = await api.discoverModels(provider.id);
      if (!result.success) {
        message.warning(result.message || t("models.discoverModelsFailed"));
        return;
      }

      if (result.added_count > 0) {
        message.success(
          t("models.autoDiscoveredAndAdded", {
            count: result.models.length,
            added: result.added_count,
          }),
        );
        await onSaved();
      } else if (result.models.length > 0) {
        message.info(
          t("models.autoDiscoveredNoNew", { count: result.models.length }),
        );
        await onSaved();
      } else {
        message.info(result.message || t("models.noModels"));
      }
    } catch (error) {
      const errMsg =
        error instanceof Error
          ? error.message
          : t("models.discoverModelsFailed");
      message.error(errMsg);
    } finally {
      setDiscovering(false);
    }
  };

  useEffect(() => {
    // Do not auto-discover models when modal opens, as it may take some time and we don't want to block the UI.
    // Instead, users can click the "Discover Models" button to trigger discovery when needed.
  }, [open, canDiscover, provider.id, provider.models.length]);

  const all_models = [
    ...(provider.models ?? []),
    ...(provider.extra_models ?? []),
  ];

  return (
    <Modal
      rootClassName="console-management-modal"
      title={t("models.manageModelsTitle", { provider: provider.name })}
      open={open}
      onCancel={handleClose}
      footer={
        <div className={styles.modalFooter}>
          <div className={styles.modalFooterRight}>
            <Button onClick={handleClose}>{t("models.cancel")}</Button>
          </div>
        </div>
      }
      width={560}
      destroyOnHidden
    >
      {/* Model list */}
      <div className={styles.modelList}>
        {all_models.length === 0 ? (
          <div className={styles.modelListEmpty}>{t("models.noModels")}</div>
        ) : (
          all_models.map((m) => {
            const isDeletable = extraModelIds.has(m.id);
            return (
              <div key={m.id} className={styles.modelListItem}>
                <div className={styles.modelListItemInfo}>
                  <span className={styles.modelListItemName}>
                    {m.name}
                    {m.supports_image === true && (
                      <Tag color="blue" style={{ fontSize: 11, marginLeft: 6 }}>
                        {t("models.tagImage", "图片")}
                      </Tag>
                    )}
                    {m.supports_video === true && (
                      <Tag
                        color="purple"
                        style={{ fontSize: 11, marginLeft: 4 }}
                      >
                        {t("models.tagVideo", "视频")}
                      </Tag>
                    )}
                    {m.supports_multimodal === false && (
                      <Tag style={{ fontSize: 11, marginLeft: 6 }}>
                        {t("models.tagTextOnly", "纯文本")}
                      </Tag>
                    )}
                    {m.supports_multimodal === null && (
                      <Tag
                        color="default"
                        style={{ fontSize: 11, marginLeft: 6 }}
                      >
                        {t("models.tagNotProbed", "未检测")}
                      </Tag>
                    )}
                  </span>
                  <span className={styles.modelListItemId}>{m.id}</span>
                </div>
                <div className={styles.modelListItemActions}>
                  {isDeletable ? (
                    <>
                      <Tag
                        color="blue"
                        style={{ fontSize: 11, marginRight: 4 }}
                      >
                        {t("models.userAdded")}
                      </Tag>
                      <Button
                        type="text"
                        size="small"
                        icon={<EyeOutlined />}
                        onClick={() => handleProbeMultimodal(m.id)}
                        loading={probingModelId === m.id}
                        style={{
                          marginRight: 4,
                          color: isDark ? "rgba(255,255,255,0.65)" : undefined,
                        }}
                      >
                        {t("models.probeMultimodal", "测试多模态")}
                      </Button>
                      <Button
                        type="text"
                        size="small"
                        icon={<ApiOutlined />}
                        onClick={() => handleTestModel(m.id)}
                        loading={testingModelId === m.id}
                        style={{
                          marginRight: 4,
                          color: isDark ? "rgba(255,255,255,0.65)" : undefined,
                        }}
                      >
                        {t("models.testConnection")}
                      </Button>
                      <Button
                        type="text"
                        size="small"
                        icon={<SettingOutlined />}
                        onClick={() => openModelConfig(m.id)}
                      >
                        {t("models.configure", "配置")}
                      </Button>
                      <Button
                        type="text"
                        size="small"
                        danger
                        icon={<DeleteOutlined />}
                        onClick={() => handleRemoveModel(m.id, m.name)}
                      />
                    </>
                  ) : (
                    <>
                      <Tag
                        color="green"
                        style={{ fontSize: 11, marginRight: 4 }}
                      >
                        {t("models.builtin")}
                      </Tag>
                      <Button
                        type="text"
                        size="small"
                        icon={<EyeOutlined />}
                        onClick={() => handleProbeMultimodal(m.id)}
                        loading={probingModelId === m.id}
                        style={{
                          marginRight: 4,
                          color: isDark ? "rgba(255,255,255,0.65)" : undefined,
                        }}
                      >
                        {t("models.probeMultimodal", "测试多模态")}
                      </Button>
                      <Button
                        type="text"
                        size="small"
                        icon={<ApiOutlined />}
                        onClick={() => handleTestModel(m.id)}
                        loading={testingModelId === m.id}
                        style={{
                          color: isDark ? "rgba(255,255,255,0.65)" : undefined,
                        }}
                      >
                        {t("models.testConnection")}
                      </Button>
                      <Button
                        type="text"
                        size="small"
                        icon={<SettingOutlined />}
                        onClick={() => openModelConfig(m.id)}
                      >
                        {t("models.configure", "配置")}
                      </Button>
                    </>
                  )}
                </div>
              </div>
            );
          })
        )}
      </div>

      <Modal
        rootClassName={`console-management-modal ${styles.runtimeConfigModal}`}
        title={
          <div className={styles.runtimeConfigTitle}>
            <span>{t("models.modelRuntimeConfig", "模型运行配置")}</span>
            {configModelId && (
              <span
                className={styles.runtimeConfigModelId}
                title={configModelId}
              >
                {configModelId}
              </span>
            )}
          </div>
        }
        open={configModelId !== null}
        onCancel={() => setConfigModelId(null)}
        onOk={saveModelConfig}
        confirmLoading={configSaving}
        okText={t("common.confirm", "确定")}
        cancelText={t("models.cancel")}
        width={720}
        destroyOnHidden
      >
        <Form
          form={configForm}
          layout="vertical"
          className={styles.runtimeConfigForm}
        >
          <div className={styles.runtimeConfigGrid}>
            <div className={styles.runtimeConfigColumn}>
              <Form.Item name="temperature" label="Temperature">
                <InputNumber
                  min={0}
                  step={0.1}
                  className={styles.runtimeConfigInput}
                />
              </Form.Item>
              <Form.Item name="top_p" label="Top P">
                <InputNumber
                  min={0}
                  max={1}
                  step={0.01}
                  className={styles.runtimeConfigInput}
                />
              </Form.Item>
              <Form.Item name="top_k" label="Top K">
                <InputNumber
                  min={0}
                  precision={0}
                  className={styles.runtimeConfigInput}
                />
              </Form.Item>
              <div className={styles.runtimeConfigGenerationHint}>
                {t(
                  "models.generationParameterDefaultHint",
                  "以上参数未设置时使用模型服务端默认设置",
                )}
              </div>
            </div>
            <div className={styles.runtimeConfigColumn}>
              <Form.Item
                name="max_input_length"
                label={t("models.maxInputLength", "最大输入长度")}
              >
                <InputNumber
                  min={1}
                  precision={0}
                  placeholder={t(
                    "models.maxInputLengthDefaultPlaceholder",
                    "128K（系统默认）",
                  )}
                  className={styles.runtimeConfigInput}
                />
              </Form.Item>
              <div
                className={styles.runtimeConfigPresets}
                aria-label="最大输入长度快捷选项"
              >
                {[32768, 65536, 131072].map((value) => (
                  <button
                    key={value}
                    type="button"
                    className={styles.runtimeConfigPreset}
                    onClick={() =>
                      configForm.setFieldValue("max_input_length", value)
                    }
                  >
                    {value / 1024}K
                  </button>
                ))}
              </div>
              <Form.Item
                name="max_output_length"
                label={t("models.maxOutputLength", "最大输出长度")}
              >
                <InputNumber
                  min={1}
                  precision={0}
                  placeholder={t(
                    "models.maxOutputLengthDefaultPlaceholder",
                    "模型服务端默认",
                  )}
                  className={styles.runtimeConfigInput}
                />
              </Form.Item>
              <div
                className={styles.runtimeConfigPresets}
                aria-label="最大输出长度快捷选项"
              >
                {[32768, 65536, 131072].map((value) => (
                  <button
                    key={value}
                    type="button"
                    className={styles.runtimeConfigPreset}
                    onClick={() =>
                      configForm.setFieldValue("max_output_length", value)
                    }
                  >
                    {value / 1024}K
                  </button>
                ))}
              </div>
            </div>
          </div>

          <div className={styles.runtimeConfigAdvanced}>
            <div className={styles.runtimeConfigAdvancedGrid}>
              <div className={styles.runtimeConfigThinkingRow}>
                <div>
                  <div className={styles.runtimeConfigSectionLabel}>
                    {t("models.supportsThinkingSwitch", "支持思考模式开关")}
                  </div>
                  <div className={styles.runtimeConfigHint}>
                    开启后，聊天界面会显示思考模式选择按钮，用户可按需选择是否启用
                  </div>
                </div>
                <Form.Item
                  name="supports_enable_thinking"
                  valuePropName="checked"
                  noStyle
                >
                  <Switch />
                </Form.Item>
              </div>
              <Form.Item
                name="supported_reasoning_efforts"
                label={t("models.reasoningEfforts", "支持的思考强度")}
                className={styles.runtimeConfigReasoning}
              >
                <Checkbox.Group className={styles.runtimeConfigReasoningGroup}>
                  {(["low", "high", "max"] as const).map((effort) => (
                    <Checkbox
                      key={effort}
                      value={effort}
                      className={styles.runtimeConfigReasoningOption}
                    >
                      {effort}
                    </Checkbox>
                  ))}
                </Checkbox.Group>
              </Form.Item>
            </div>
          </div>
        </Form>
      </Modal>

      {/* Add model section */}
      {adding ? (
        <div className={styles.modelAddForm}>
          <Form form={form} layout="vertical" style={{ marginBottom: 0 }}>
            <Form.Item
              name="id"
              label={t("models.modelIdLabel")}
              rules={[{ required: true, message: t("models.modelIdLabel") }]}
              style={{ marginBottom: 12 }}
            >
              <Input placeholder={t("models.modelIdPlaceholder")} />
            </Form.Item>
            <Form.Item
              name="name"
              label={t("models.modelNameLabel")}
              style={{ marginBottom: 12 }}
            >
              <Input placeholder={t("models.modelNamePlaceholder")} />
            </Form.Item>
            <div
              style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}
            >
              <Button
                size="small"
                onClick={() => {
                  setAdding(false);
                  form.resetFields();
                }}
              >
                {t("models.cancel")}
              </Button>
              <Button
                type="primary"
                size="small"
                loading={saving}
                onClick={handleAddModel}
              >
                {t("models.addModel")}
              </Button>
            </div>
          </Form>
        </div>
      ) : (
        <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
          <Button
            icon={<SyncOutlined />}
            onClick={handleDiscoverModels}
            loading={discovering}
            disabled={!canDiscover}
            style={{ flex: 1 }}
          >
            {t("models.discoverModels")}
          </Button>
          <Button
            type="dashed"
            icon={<PlusOutlined />}
            onClick={() => setAdding(true)}
            style={{ flex: 1 }}
          >
            {t("models.addModel")}
          </Button>
        </div>
      )}
    </Modal>
  );
}
