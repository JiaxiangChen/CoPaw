import { request } from "../request";
import type { SkillSpec } from "../types";

// Instance types
export interface InstanceConfig {
  id: string;
  name: string;
  url: string;
  enabled: boolean;
  latest_backup?: {
    date: string;
    hour: number;
    user_count: number;
  };
}

export interface BatchTaskItemStatus {
  instance_id: string;
  instance_name: string;
  status: "pending" | "running" | "success" | "failed";
  task_id?: string;
  message: string;
  error?: string;
}

export interface BatchTaskStatus {
  batch_id: string;
  task_type: "backup" | "restore";
  created_at: string;
  total: number;
  completed: number;
  success: number;
  failed: number;
  status: "pending" | "running" | "completed" | "partial" | "failed";
  items: BatchTaskItemStatus[];
  backup_date?: string;
  backup_hour?: number;
}

export const backupBatchApi = {
  // Get all instances
  listInstances: () =>
    request<{ instances: InstanceConfig[]; total: number }>("/backup/batch/instances"),

  // Update instances config
  updateInstances: (instances: InstanceConfig[]) =>
    request<{ success: boolean; message: string }>("/backup/batch/instances", {
      method: "PUT",
      body: JSON.stringify({ instances }),
    }),

  // Batch backup
  batchBackup: (params?: {
    instance_ids?: string[];
    backup_date?: string;
    backup_hour?: number;
  }) =>
    request<BatchTaskStatus>("/backup/batch/upload", {
      method: "POST",
      body: JSON.stringify(params || {}),
    }),

  // Batch restore
  batchRestore: (params?: {
    instance_ids?: string[];
    backup_date?: string;
    backup_hour?: number;
  }) =>
    request<BatchTaskStatus>("/backup/batch/download", {
      method: "POST",
      body: JSON.stringify(params || {}),
    }),

  // Get batch task status
  getBatchTask: (batchId: string) =>
    request<BatchTaskStatus>(`/backup/batch/tasks/${batchId}`),

  // List batch tasks
  listBatchTasks: (limit = 20) =>
    request<{ tasks: BatchTaskStatus[]; total: number }>(
      `/backup/batch/tasks?limit=${limit}`
    ),

  // Get latest backups
  getLatestBackups: () =>
    request<Record<string, { date: string; hour: number; user_count: number }>>(
      "/backup/batch/latest-backups"
    ),
};