import { useState, useEffect, useCallback } from "react";
import {
  Button,
  Card,
  Table,
  Tag,
  Space,
  Modal,
  Form,
  Input,
  Switch,
  message,
  Progress,
  Tooltip,
  Popconfirm,
} from "@agentscope-ai/design";
import {
  CloudUploadOutlined,
  CloudDownloadOutlined,
  PlusOutlined,
  DeleteOutlined,
  EditOutlined,
  ReloadOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  LoadingOutlined,
  ClockCircleOutlined,
} from "@ant-design/icons";
import { backupBatchApi, InstanceConfig, BatchTaskStatus } from "../../../api/modules/backupBatch";
import styles from "./index.module.less";

interface InstanceFormData {
  id: string;
  name: string;
  url: string;
  enabled: boolean;
}

function BackupPage() {
  const [instances, setInstances] = useState<InstanceConfig[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editingInstance, setEditingInstance] = useState<InstanceConfig | null>(null);
  const [batchTask, setBatchTask] = useState<BatchTaskStatus | null>(null);
  const [historyTasks, setHistoryTasks] = useState<BatchTaskStatus[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [form] = Form.useForm<InstanceFormData>();
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  const [operationLoading, setOperationLoading] = useState(false);

  const fetchInstances = useCallback(async () => {
    setLoading(true);
    try {
      const data = await backupBatchApi.listInstances();
      setInstances(data.instances || []);
    } catch (error) {
      console.error("Failed to fetch instances", error);
      message.error("获取容器列表失败");
    } finally {
      setLoading(false);
    }
  }, []);

  const fetchHistoryTasks = useCallback(async () => {
    setHistoryLoading(true);
    try {
      const data = await backupBatchApi.listBatchTasks(10);
      setHistoryTasks(data.tasks || []);
    } catch (error) {
      console.error("Failed to fetch history tasks", error);
    } finally {
      setHistoryLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchInstances();
    fetchHistoryTasks();
  }, [fetchInstances, fetchHistoryTasks]);

  // Poll batch task status
  useEffect(() => {
    if (!batchTask || batchTask.status === "completed" || batchTask.status === "failed" || batchTask.status === "partial") {
      return;
    }

    const interval = setInterval(async () => {
      try {
        const task = await backupBatchApi.getBatchTask(batchTask.batch_id);
        setBatchTask(task);
        if (task.status === "completed" || task.status === "failed" || task.status === "partial") {
          fetchInstances();
          fetchHistoryTasks();
        }
      } catch (error) {
        console.error("Failed to poll task status", error);
      }
    }, 2000);

    return () => clearInterval(interval);
  }, [batchTask, fetchInstances, fetchHistoryTasks]);

  const handleAddInstance = () => {
    setEditingInstance(null);
    form.resetFields();
    form.setFieldsValue({ enabled: true });
    setModalOpen(true);
  };

  const handleEditInstance = (record: InstanceConfig) => {
    setEditingInstance(record);
    form.setFieldsValue(record);
    setModalOpen(true);
  };

  const handleDeleteInstance = (id: string) => {
    const newInstances = instances.filter((i) => i.id !== id);
    saveInstances(newInstances);
  };

  const handleSaveInstance = async (values: InstanceFormData) => {
    let newInstances: InstanceConfig[];
    if (editingInstance) {
      newInstances = instances.map((i) =>
        i.id === editingInstance.id ? { ...values } : i
      );
    } else {
      if (instances.some((i) => i.id === values.id)) {
        message.error("实例ID已存在");
        return;
      }
      newInstances = [...instances, values];
    }
    await saveInstances(newInstances);
    setModalOpen(false);
  };

  const saveInstances = async (newInstances: InstanceConfig[]) => {
    try {
      await backupBatchApi.updateInstances(newInstances);
      message.success("保存成功");
      setInstances(newInstances);
    } catch (error) {
      console.error("Failed to save instances", error);
      message.error("保存失败");
    }
  };

  const handleBatchBackup = async () => {
    const targetInstances = selectedRowKeys.length > 0
      ? instances.filter((i) => selectedRowKeys.includes(i.id) && i.enabled)
      : instances.filter((i) => i.enabled);

    if (targetInstances.length === 0) {
      message.warning("没有可备份的容器实例");
      return;
    }

    Modal.confirm({
      title: "确认批量备份",
      content: `将对 ${targetInstances.length} 个容器实例执行备份操作，确认继续？`,
      onOk: async () => {
        setOperationLoading(true);
        try {
          const task = await backupBatchApi.batchBackup({
            instance_ids: selectedRowKeys.length > 0 ? selectedRowKeys as string[] : undefined,
          });
          setBatchTask(task);
          message.success("批量备份任务已启动");
        } catch (error) {
          console.error("Failed to start batch backup", error);
          message.error("启动批量备份失败");
        } finally {
          setOperationLoading(false);
        }
      },
    });
  };

  const handleBatchRestore = async () => {
    const targetInstances = selectedRowKeys.length > 0
      ? instances.filter((i) => selectedRowKeys.includes(i.id) && i.enabled)
      : instances.filter((i) => i.enabled);

    if (targetInstances.length === 0) {
      message.warning("没有可恢复的容器实例");
      return;
    }

    Modal.confirm({
      title: "确认批量恢复",
      content: `将从最近备份恢复 ${targetInstances.length} 个容器实例，此操作将覆盖现有数据，确认继续？`,
      okType: "danger",
      onOk: async () => {
        setOperationLoading(true);
        try {
          const task = await backupBatchApi.batchRestore({
            instance_ids: selectedRowKeys.length > 0 ? selectedRowKeys as string[] : undefined,
          });
          setBatchTask(task);
          message.success("批量恢复任务已启动");
        } catch (error) {
          console.error("Failed to start batch restore", error);
          message.error("启动批量恢复失败");
        } finally {
          setOperationLoading(false);
        }
      },
    });
  };

  const getStatusTag = (status: string) => {
    const map: Record<string, { color: string; icon: React.ReactNode }> = {
      pending: { color: "default", icon: <ClockCircleOutlined /> },
      running: { color: "processing", icon: <LoadingOutlined spin /> },
      success: { color: "success", icon: <CheckCircleOutlined /> },
      completed: { color: "success", icon: <CheckCircleOutlined /> },
      failed: { color: "error", icon: <CloseCircleOutlined /> },
      partial: { color: "warning", icon: <CloseCircleOutlined /> },
    };
    const config = map[status] || map.pending;
    return (
      <Tag color={config.color} icon={config.icon}>
        {status.toUpperCase()}
      </Tag>
    );
  };

  const columns = [
    {
      title: "实例ID",
      dataIndex: "id",
      key: "id",
      width: 120,
    },
    {
      title: "名称",
      dataIndex: "name",
      key: "name",
      width: 150,
    },
    {
      title: "URL",
      dataIndex: "url",
      key: "url",
      ellipsis: true,
    },
    {
      title: "状态",
      dataIndex: "enabled",
      key: "enabled",
      width: 80,
      render: (enabled: boolean) => (
        <Tag color={enabled ? "green" : "default"}>
          {enabled ? "启用" : "禁用"}
        </Tag>
      ),
    },
    {
      title: "最新备份",
      key: "latest_backup",
      width: 200,
      render: (_: unknown, record: InstanceConfig) => {
        if (!record.latest_backup) {
          return <span style={{ color: "#999" }}>暂无备份</span>;
        }
        const { date, hour, user_count } = record.latest_backup;
        return (
          <Tooltip title={`${user_count} 个用户`}>
            <span>
              {date} {String(hour).padStart(2, "0")}:00 ({user_count}用户)
            </span>
          </Tooltip>
        );
      },
    },
    {
      title: "操作",
      key: "action",
      width: 120,
      render: (_: unknown, record: InstanceConfig) => (
        <Space size="small">
          <Button
            type="link"
            size="small"
            icon={<EditOutlined />}
            onClick={() => handleEditInstance(record)}
          />
          <Popconfirm
            title="确定删除该实例配置？"
            onConfirm={() => handleDeleteInstance(record.id)}
          >
            <Button
              type="link"
              size="small"
              danger
              icon={<DeleteOutlined />}
            />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const rowSelection = {
    selectedRowKeys,
    onChange: (newSelectedRowKeys: React.Key[]) => {
      setSelectedRowKeys(newSelectedRowKeys);
    },
  };

  const progressPercent = batchTask
    ? Math.round((batchTask.completed / batchTask.total) * 100)
    : 0;

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <h1>备份管理</h1>
        <p>管理多容器实例的备份和恢复操作</p>
      </div>

      {/* Batch Task Progress */}
      {batchTask && batchTask.status !== "completed" && batchTask.status !== "failed" && batchTask.status !== "partial" && (
        <Card className={styles.progressCard}>
          <div className={styles.progressHeader}>
            <span>
              {batchTask.task_type === "backup" ? "批量备份" : "批量恢复"} 进度
            </span>
            <span>
              {batchTask.completed} / {batchTask.total}
            </span>
          </div>
          <Progress percent={progressPercent} status="active" />
          <div className={styles.taskItems}>
            {batchTask.items.map((item) => (
              <div key={item.instance_id} className={styles.taskItem}>
                <span>{item.instance_name}</span>
                {getStatusTag(item.status)}
              </div>
            ))}
          </div>
        </Card>
      )}

      {/* Actions */}
      <Card className={styles.actionsCard}>
        <Space size="middle">
          <Button
            type="primary"
            icon={<CloudUploadOutlined />}
            onClick={handleBatchBackup}
            loading={operationLoading}
            disabled={instances.filter((i) => i.enabled).length === 0}
          >
            一键备份
            {selectedRowKeys.length > 0 && ` (${selectedRowKeys.length})`}
          </Button>
          <Button
            type="default"
            icon={<CloudDownloadOutlined />}
            onClick={handleBatchRestore}
            loading={operationLoading}
            disabled={instances.filter((i) => i.enabled).length === 0}
          >
            一键恢复
            {selectedRowKeys.length > 0 && ` (${selectedRowKeys.length})`}
          </Button>
          <Button
            icon={<PlusOutlined />}
            onClick={handleAddInstance}
          >
            添加实例
          </Button>
          <Button
            icon={<ReloadOutlined />}
            onClick={() => { fetchInstances(); fetchHistoryTasks(); }}
          >
            刷新
          </Button>
        </Space>
      </Card>

      {/* Instances Table */}
      <Card title="容器实例列表" className={styles.tableCard}>
        <Table
          rowKey="id"
          columns={columns}
          dataSource={instances}
          loading={loading}
          rowSelection={rowSelection}
          pagination={false}
          size="middle"
        />
      </Card>

      {/* History Tasks */}
      <Card title="操作历史" className={styles.historyCard}>
        <Table
          rowKey="batch_id"
          columns={[
            {
              title: "操作类型",
              dataIndex: "task_type",
              key: "task_type",
              width: 100,
              render: (type: string) => (
                <Tag color={type === "backup" ? "blue" : "green"}>
                  {type === "backup" ? "备份" : "恢复"}
                </Tag>
              ),
            },
            {
              title: "状态",
              dataIndex: "status",
              key: "status",
              width: 120,
              render: (status: string) => getStatusTag(status),
            },
            {
              title: "成功/总数",
              key: "progress",
              width: 100,
              render: (_: unknown, record: BatchTaskStatus) => (
                <span>
                  {record.success}/{record.total}
                </span>
              ),
            },
            {
              title: "备份时间",
              key: "backup_time",
              width: 150,
              render: (_: unknown, record: BatchTaskStatus) => (
                <span>
                  {record.backup_date} {record.backup_hour ? String(record.backup_hour).padStart(2, "0") : ""}:00
                </span>
              ),
            },
            {
              title: "创建时间",
              dataIndex: "created_at",
              key: "created_at",
              width: 180,
              render: (time: string) => new Date(time).toLocaleString(),
            },
          ]}
          dataSource={historyTasks}
          loading={historyLoading}
          pagination={false}
          size="small"
        />
      </Card>

      {/* Add/Edit Modal */}
      <Modal
        title={editingInstance ? "编辑实例" : "添加实例"}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={() => form.submit()}
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={handleSaveInstance}
        >
          <Form.Item
            name="id"
            label="实例ID"
            rules={[
              { required: true, message: "请输入实例ID" },
              { pattern: /^[a-zA-Z0-9_-]+$/, message: "只允许字母、数字、下划线和连字符" },
            ]}
          >
            <Input placeholder="例如: instance-01" disabled={!!editingInstance} />
          </Form.Item>
          <Form.Item
            name="name"
            label="名称"
            rules={[{ required: true, message: "请输入名称" }]}
          >
            <Input placeholder="例如: 容器1-生产" />
          </Form.Item>
          <Form.Item
            name="url"
            label="URL"
            rules={[
              { required: true, message: "请输入URL" },
              { type: "url", message: "请输入有效的URL" },
            ]}
          >
            <Input placeholder="例如: https://app1.example.com" />
          </Form.Item>
          <Form.Item
            name="enabled"
            label="启用"
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

export default BackupPage;