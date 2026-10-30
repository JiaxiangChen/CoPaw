import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Drawer,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tag,
  Typography,
} from "antd";
import { PlusOutlined } from "@ant-design/icons";
import api from "../../api";
import {
  expertsApi,
  type Expert,
  type ExpertPayload,
} from "../../api/modules/experts";
import { useAppMessage } from "../../hooks/useAppMessage";
import { marketApi, type Category } from "../../api/modules/market";
import { useIframeStore } from "../../stores/iframeStore";
import { useAgentStore } from "../../stores/agentStore";

const emptyPayload: ExpertPayload = {
  name: "",
  description: "",
  instruction: "",
  trigger_keywords: [],
  skills: [],
  mcps: null,
  tools: {},
  model: null,
  budget: {},
};

type ExpertFormValues = Omit<Partial<ExpertPayload>, "model"> & {
  keywordsText?: string;
  skillsText?: string[];
  mcpsText?: string[];
  model?: ExpertPayload["model"] | string;
};

type PublishFormValues = {
  category_id?: number;
  bbk_ids?: string;
};

const splitList = (value?: string) =>
  value
    ?.split(",")
    .map((item) => item.trim())
    .filter(Boolean) || [];

export const payloadFromValues = (values: ExpertFormValues): ExpertPayload => {
  const model =
    typeof values.model === "string"
      ? (() => {
          const separator = values.model.indexOf("::");
          return separator > 0
            ? {
                provider: values.model.slice(0, separator),
                id: values.model.slice(separator + 2),
              }
            : null;
        })()
      : values.model || null;
  return {
    name: values.name || "",
    description: values.description || "",
    instruction: values.instruction || "",
    trigger_keywords: splitList(values.keywordsText),
    skills: values.skillsText || [],
    mcps: values.mcpsText?.length ? values.mcpsText : null,
    tools: {},
    model,
    budget: {},
  };
};

export default function ExpertsPage() {
  const sourceId = useIframeStore((state) => state.source) || "default";
  const isManager = useIframeStore((state) => state.manager);
  const selectedAgent = useAgentStore((state) => state.selectedAgent);
  const [items, setItems] = useState<Expert[]>([]);
  const [skillOptions, setSkillOptions] = useState<
    { label: string; value: string }[]
  >([]);
  const [mcpOptions, setMcpOptions] = useState<
    { label: string; value: string }[]
  >([]);
  const [modelOptions, setModelOptions] = useState<
    { label: string; value: string }[]
  >([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [previewToml, setPreviewToml] = useState("");
  const [selected, setSelected] = useState<Expert | null>(null);
  const [publishTarget, setPublishTarget] = useState<Expert | null>(null);
  const [publishOpen, setPublishOpen] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [categories, setCategories] = useState<Category[]>([]);
  const [form] = Form.useForm<ExpertFormValues>();
  const [publishForm] = Form.useForm<PublishFormValues>();
  const { message } = useAppMessage();
  const load = async () => {
    setLoading(true);
    try {
      setItems(await expertsApi.listExperts());
    } catch (error) {
      message.error(error instanceof Error ? error.message : "加载专家失败");
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => {
    void load();
    void api
      .listEffectiveSkills()
      .then((items) =>
        setSkillOptions(
          items.map((item) => ({ label: item.name, value: item.name })),
        ),
      )
      .catch(() => {});
    void api
      .listMCPClients()
      .then((items) =>
        setMcpOptions(
          items
            .filter((item) => item.enabled)
            .map((item) => ({ label: item.name || item.key, value: item.key })),
        ),
      )
      .catch(() => {});
    void api
      .listProviders()
      .then((providers) => {
        const available = providers.flatMap((provider) =>
          provider.models.concat(provider.extra_models).map((model) => ({
            label: `${provider.id} / ${model.id}`,
            value: `${provider.id}::${model.id}`,
          })),
        );
        setModelOptions((current) => {
          const preserved = current.filter(
            (option) =>
              option.value &&
              !available.some((candidate) => candidate.value === option.value),
          );
          return [
            { label: "继承当前聊天模型", value: "" },
            ...available,
            ...preserved,
          ];
        });
      })
      .catch(() => {});
  }, []);
  const openPublish = async (expert: Expert) => {
    setPublishTarget(expert);
    publishForm.resetFields();
    setPublishOpen(true);
    try {
      setCategories(await marketApi.listCategories(sourceId));
    } catch {
      setCategories([]);
    }
  };
  const publish = async (overwrite = false) => {
    if (!publishTarget) return;
    const values = await publishForm.validateFields();
    setPublishing(true);
    try {
      const published = await marketApi.publishExpert(sourceId, {
        definition_id: publishTarget.definition_id,
        agent_id: selectedAgent,
        category_id: values.category_id,
        bbk_ids: splitList(values.bbk_ids),
        overwrite,
      });
      message.success(
        published.version_unchanged
          ? "专家内容未变化，社区版本保持不变"
          : `已同步到专家社区 v${published.version}`,
      );
      setPublishOpen(false);
      setPublishTarget(null);
    } catch (error) {
      const requestError = error as Error & { status?: number };
      if (requestError.status === 409 && !overwrite) {
        Modal.confirm({
          title: "社区中已有同名专家",
          content: "确认后将作为该社区专家的新版本继续发布，保留历史版本。",
          okText: "确认覆盖",
          cancelText: "取消",
          onOk: () => publish(true),
        });
        return;
      }
      message.error(error instanceof Error ? error.message : "同步专家失败");
    } finally {
      setPublishing(false);
    }
  };
  const open = (expert?: Expert) => {
    const definition = expert?.definition;
    const model = definition?.agent_owned?.model;
    if (model) {
      const value = `${model.provider}::${model.id}`;
      setModelOptions((options) =>
        options.some((option) => option.value === value)
          ? options
          : [
              ...options,
              {
                label: `${model.provider} / ${model.id}（当前不可用）`,
                value,
              },
            ],
      );
    }
    setSelected(expert || null);
    setPreviewToml(expert?.toml || "");
    setDrawerOpen(true);
    form.setFieldsValue(
      definition
        ? {
            ...emptyPayload,
            name: definition.name,
            description: definition.description,
            instruction: definition.instruction,
            trigger_keywords: definition.trigger_keywords,
            keywordsText: definition.trigger_keywords.join(", "),
            skillsText: definition.agent_owned?.declared_skills || [],
            mcpsText: definition.agent_owned?.declared_mcps || [],
            model: model ? `${model.provider}::${model.id}` : "",
          }
        : emptyPayload,
    );
  };
  const payloadFromForm = async (): Promise<ExpertPayload> =>
    payloadFromValues(await form.validateFields());
  const save = async () => {
    const payload = await payloadFromForm();
    setSaving(true);
    try {
      const saved = selected
        ? await expertsApi.updateExpert(
            selected.definition_id,
            payload,
            selected.revision,
          )
        : await expertsApi.createExpert(payload);
      setSelected(saved);
      setPreviewToml(saved.toml);
      message.success("专家配置已保存；启停将在下一轮主 Agent 生效");
      await load();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };
  const preview = async () => {
    try {
      setPreviewToml(
        (await expertsApi.previewExpert(await payloadFromForm())).toml,
      );
    } catch (error) {
      message.error(error instanceof Error ? error.message : "配置校验失败");
    }
  };
  const toggle = async (expert: Expert, enabled: boolean) => {
    try {
      await (enabled
        ? expertsApi.enableExpert(expert.definition_id, expert.revision)
        : expertsApi.disableExpert(expert.definition_id, expert.revision));
      await load();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "状态更新失败");
    }
  };
  return (
    <section style={{ padding: 24 }}>
      <Space
        style={{
          width: "100%",
          justifyContent: "space-between",
          marginBottom: 16,
        }}
      >
        <div>
          <Typography.Title level={3} style={{ margin: 0 }}>
            专家
          </Typography.Title>
          <Typography.Text type="secondary">
            配置当前 Agent 的可复用子代理专家；仅启用配置会在下一轮生效。
          </Typography.Text>
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => open()}>
          新建专家
        </Button>
      </Space>
      <Spin spinning={loading}>
        {items.length ? (
          <Table
            rowKey="definition_id"
            pagination={false}
            dataSource={items}
            columns={[
              {
                title: "名称",
                render: (_, item) =>
                  item.definition?.name || item.definition_id,
              },
              {
                title: "说明",
                render: (_, item) => (
                  <Space direction="vertical" size={2}>
                    <span>
                      {item.definition?.description || item.validation_error}
                    </span>
                    {item.definition?.agent_owned?.community ? (
                      <Tag color="blue">
                        社区已接收 · v
                        {item.definition.agent_owned.community.version}
                      </Tag>
                    ) : null}
                  </Space>
                ),
              },
              {
                title: "关键词",
                render: (_, item) =>
                  ((item.definition?.trigger_keywords as string[]) || []).map(
                    (word) => <Tag key={word}>{word}</Tag>,
                  ),
              },
              {
                title: "状态",
                render: (_, item) =>
                  item.valid ? (
                    <Switch
                      checked={item.enabled}
                      onChange={(checked) => void toggle(item, checked)}
                    />
                  ) : (
                    <Tag color="error">配置无效</Tag>
                  ),
              },
              {
                title: "操作",
                render: (_, item) => (
                  <Space>
                    <Button
                      type="link"
                      disabled={!item.valid}
                      onClick={() => open(item)}
                    >
                      编辑
                    </Button>
                    {isManager && item.valid ? (
                      <Button type="link" onClick={() => void openPublish(item)}>
                        同步到专家社区
                      </Button>
                    ) : null}
                    <Popconfirm
                      title="删除此专家？"
                      onConfirm={() =>
                        void expertsApi
                          .deleteExpert(item.definition_id, item.revision)
                          .then(load)
                      }
                    >
                      <Button type="link" danger>
                        删除
                      </Button>
                    </Popconfirm>
                  </Space>
                ),
              },
            ]}
          />
        ) : (
          <Empty description="还没有自定义专家" />
        )}
      </Spin>
      <Drawer
        title={selected ? "编辑专家" : "新建专家"}
        width={560}
        open={drawerOpen}
        onClose={() => {
          setDrawerOpen(false);
          setSelected(null);
          form.resetFields();
        }}
        extra={
          <Space>
            <Button onClick={() => void preview()}>预览 TOML</Button>
            <Button type="primary" loading={saving} onClick={() => void save()}>
              保存
            </Button>
          </Space>
        }
      >
        <Form layout="vertical" form={form}>
          <Form.Item name="name" label="调用名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item
            name="description"
            label="说明"
            rules={[{ required: true }]}
          >
            <Input />
          </Form.Item>
          <Form.Item
            name="instruction"
            label="专家指令"
            rules={[{ required: true }]}
          >
            <Input.TextArea rows={8} />
          </Form.Item>
          <Form.Item name="keywordsText" label="关键词">
            <Input placeholder="用逗号分隔" />
          </Form.Item>
          <Form.Item name="skillsText" label="Skills">
            <Select
              mode="multiple"
              options={skillOptions}
              disabled={Boolean(selected?.definition?.agent_owned?.community)}
            />
          </Form.Item>
          <Form.Item name="mcpsText" label="MCP">
            <Select
              mode="multiple"
              options={mcpOptions}
              placeholder="留空则不使用 MCP"
              disabled={Boolean(selected?.definition?.agent_owned?.community)}
            />
          </Form.Item>
          <Form.Item name="model" label="模型">
            <Select
              allowClear
              options={modelOptions}
              placeholder="继承当前聊天模型"
            />
          </Form.Item>
        </Form>
        {selected && !selected.valid ? (
          <Alert type="error" message={selected.validation_error} />
        ) : null}
        {previewToml ? (
          <Typography.Paragraph copyable>
            <pre>{previewToml}</pre>
          </Typography.Paragraph>
        ) : null}
      </Drawer>
      <Modal
        title={publishTarget ? `同步“${publishTarget.definition?.name || publishTarget.definition_id}”` : "同步到专家社区"}
        open={publishOpen}
        confirmLoading={publishing}
        okText="同步"
        onOk={() => void publish()}
        onCancel={() => {
          setPublishOpen(false);
          setPublishTarget(null);
        }}
      >
        <Form layout="vertical" form={publishForm}>
          <Form.Item name="category_id" label="社区分类">
            <Select
              allowClear
              options={categories.map((category) => ({
                label: category.name,
                value: category.id,
              }))}
            />
          </Form.Item>
          <Form.Item name="bbk_ids" label="BBK">
            <Input placeholder="可选，多个 BBK 用逗号分隔" />
          </Form.Item>
        </Form>
      </Modal>
    </section>
  );
}
