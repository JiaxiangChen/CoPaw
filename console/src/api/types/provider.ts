export interface ModelInfo {
  id: string;
  name: string;
  supports_multimodal: boolean | null;
  supports_image: boolean | null;
  supports_video: boolean | null;
}

export type ReasoningEffort = "low" | "high" | "max";

export interface ModelRuntimeConfig {
  temperature?: number | null;
  top_p?: number | null;
  top_k?: number | null;
  max_input_length?: number | null;
  max_output_length?: number | null;
  supports_enable_thinking: boolean;
  supported_reasoning_efforts: ReasoningEffort[];
  enable_thinking: boolean;
  reasoning_effort?: ReasoningEffort | null;
}

export type ModelRuntimeConfigUpdate = Partial<ModelRuntimeConfig>;

export interface ProviderInfo {
  id: string;
  name: string;
  api_key_prefix: string;
  chat_model: string;
  /** Built-in models (for built-in providers) or all models (for custom). */
  models: ModelInfo[];
  /** User-added models (deletable). Only populated for built-in providers. */
  extra_models: ModelInfo[];
  is_custom: boolean;
  is_local: boolean;
  /** Whether this provider supports fetching available models from the provider's API. */
  support_model_discovery: boolean;
  /** Whether this provider supports checking connection to the API without model configuration. */
  support_connection_check: boolean;
  /** True when the base_url should be frozen (not editable). */
  freeze_url: boolean;
  /** True when an API key is required for this provider. */
  require_api_key: boolean;
  api_key: string;
  base_url: string;
  model_configs?: Record<string, ModelRuntimeConfig>;
}

export interface ProviderConfigRequest {
  api_key?: string;
  base_url?: string;
  chat_model?: string;
}

export interface ModelSlotConfig {
  provider_id: string;
  model: string;
}

export interface ActiveModelsInfo {
  active_llm?: ModelSlotConfig;
}

export interface DistributionTenantListResponse {
  tenant_ids: string[];
}

export interface ActiveModelDistributionRequest {
  target_tenant_ids: string[];
  overwrite: boolean;
}

export interface ActiveModelDistributionTenantResult {
  tenant_id: string;
  success: boolean;
  bootstrapped: boolean;
  provider_updated?: string;
  active_llm_updated?: ModelSlotConfig;
  error?: string;
}

export interface ActiveModelDistributionResponse {
  task_id: string;
  status: string;
  reused?: boolean;
  source_active_llm: ModelSlotConfig;
  results: ActiveModelDistributionTenantResult[];
}

/* ---- Providers full distribution ---- */

export interface ProvidersDistributionRequest {
  target_tenant_ids: string[];
  overwrite: boolean;
}

export interface ProvidersDistributionTenantResult {
  tenant_id: string;
  success: boolean;
  bootstrapped: boolean;
  error?: string;
}

export interface ProvidersDistributionResponse {
  task_id: string;
  status: string;
  reused?: boolean;
  source_tenant_id?: string;
  results: ProvidersDistributionTenantResult[];
}

export type ActiveModelScope = "effective" | "global" | "agent";

export interface GetActiveModelsRequest {
  scope?: ActiveModelScope;
  agent_id?: string;
}

export type ActiveModelWriteScope = "global" | "agent";

export interface ModelSlotRequest {
  provider_id: string;
  model: string;
  /** @deprecated 'agent' scope is deprecated. Use 'global' for tenant-level active model. */
  scope: ActiveModelWriteScope;
  /** @deprecated agent_id is no longer used. Models are now tenant-scoped. */
  agent_id?: string;
}

/* ---- Custom provider CRUD ---- */

export interface CreateCustomProviderRequest {
  id: string;
  name: string;
  default_base_url?: string;
  api_key_prefix?: string;
  chat_model?: string;
  models?: ModelInfo[];
}

export interface AddModelRequest {
  id: string;
  name: string;
}

/* ---- Local models ---- */

export interface LocalModelInfo {
  id: string;
  name: string;
  size_bytes: number;
  downloaded: boolean;
  source: LocalDownloadSource;
}

export type LocalDownloadSource = "huggingface" | "modelscope" | "auto";

export interface LocalServerStatus {
  available: boolean;
  installable: boolean;
  installed: boolean;
  port: number | null;
  model_name: string | null;
  message: string | null;
}

export interface LocalDownloadProgress {
  status:
    | "idle"
    | "pending"
    | "downloading"
    | "canceling"
    | "completed"
    | "failed"
    | "cancelled";
  model_name: string | null;
  downloaded_bytes: number;
  total_bytes: number | null;
  speed_bytes_per_sec: number;
  source: LocalDownloadSource | null;
  error: string | null;
  local_path: string | null;
}

export interface LocalActionResponse {
  status: string;
  message: string;
}

export interface StartLocalServerRequest {
  model_id: string;
}

/* ---- Test Connection ---- */

export interface TestConnectionResponse {
  success: boolean;
  message: string;
}

export interface TestProviderRequest {
  api_key?: string;
  base_url?: string;
  chat_model?: string;
}

export interface TestModelRequest {
  model_id: string;
}

export interface DiscoverModelsResponse {
  success: boolean;
  message: string;
  models: ModelInfo[];
  added_count: number;
}

export interface ProbeMultimodalResponse {
  supports_image: boolean;
  supports_video: boolean;
  supports_multimodal: boolean;
  image_message: string;
  video_message: string;
}
