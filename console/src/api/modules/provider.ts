import { request } from "../request";
import { useProviderModelStore } from "../../stores/providerModelStore";
import type {
  ProviderInfo,
  ProviderConfigRequest,
  ActiveModelsInfo,
  ActiveModelDistributionRequest,
  ActiveModelDistributionResponse,
  GetActiveModelsRequest,
  ModelSlotRequest,
  ModelRuntimeConfig,
  ModelRuntimeConfigUpdate,
  CreateCustomProviderRequest,
  AddModelRequest,
  TestConnectionResponse,
  TestProviderRequest,
  TestModelRequest,
  DiscoverModelsResponse,
  ProbeMultimodalResponse,
  DistributionTenantListResponse,
  ProvidersDistributionRequest,
  ProvidersDistributionResponse,
} from "../types";

function buildActiveModelQuery(params?: GetActiveModelsRequest): string {
  if (!params?.scope && !params?.agent_id) {
    return "/models/active";
  }

  const searchParams = new URLSearchParams();
  if (params.scope) {
    searchParams.set("scope", params.scope);
  }
  if (params.agent_id) {
    searchParams.set("agent_id", params.agent_id);
  }

  return `/models/active?${searchParams.toString()}`;
}

export const providerApi = {
  listProviders: () => request<ProviderInfo[]>("/models"),

  configureProvider: async (
    providerId: string,
    body: ProviderConfigRequest,
  ) => {
    const result = await request<ProviderInfo>(
      `/models/${encodeURIComponent(providerId)}/config`,
      {
        method: "PUT",
        body: JSON.stringify(body),
      },
    );
    useProviderModelStore
      .getState()
      .invalidate({ providers: true, active: false });
    return result;
  },

  getActiveModels: (params?: GetActiveModelsRequest) =>
    request<ActiveModelsInfo>(buildActiveModelQuery(params)),

  setActiveLlm: async (body: ModelSlotRequest) => {
    const result = await request<ActiveModelsInfo>("/models/active", {
      method: "PUT",
      body: JSON.stringify(body),
    });
    useProviderModelStore.getState().setActiveModels(result);
    return result;
  },

  listActiveModelDistributionTenants: () =>
    request<DistributionTenantListResponse>("/models/distribution/tenants"),

  distributeActiveLlm: async (body: ActiveModelDistributionRequest) => {
    const result = await request<ActiveModelDistributionResponse>(
      "/models/distribution/active-llm",
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    );
    useProviderModelStore
      .getState()
      .invalidate({ providers: false, active: true });
    return result;
  },

  distributeProviders: async (body: ProvidersDistributionRequest) => {
    const result = await request<ProvidersDistributionResponse>(
      "/models/distribution/providers",
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    );
    useProviderModelStore.getState().invalidate();
    return result;
  },

  /* ---- Custom provider CRUD ---- */

  createCustomProvider: async (body: CreateCustomProviderRequest) => {
    const result = await request<ProviderInfo>("/models/custom-providers", {
      method: "POST",
      body: JSON.stringify(body),
    });
    useProviderModelStore
      .getState()
      .invalidate({ providers: true, active: false });
    return result;
  },

  deleteCustomProvider: async (providerId: string) => {
    const result = await request<ProviderInfo[]>(
      `/models/custom-providers/${encodeURIComponent(providerId)}`,
      { method: "DELETE" },
    );
    useProviderModelStore.getState().invalidate();
    return result;
  },

  /* ---- Model CRUD (works for both built-in and custom providers) ---- */

  addModel: async (providerId: string, body: AddModelRequest) => {
    const result = await request<ProviderInfo>(
      `/models/${encodeURIComponent(providerId)}/models`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    );
    useProviderModelStore
      .getState()
      .invalidate({ providers: true, active: false });
    return result;
  },

  removeModel: async (providerId: string, modelId: string) => {
    const result = await request<ProviderInfo>(
      `/models/${encodeURIComponent(providerId)}/models/${encodeURIComponent(
        modelId,
      )}`,
      { method: "DELETE" },
    );
    useProviderModelStore.getState().invalidate();
    return result;
  },

  getModelRuntimeConfig: (providerId: string, modelId: string) =>
    request<ModelRuntimeConfig>(
      `/models/${encodeURIComponent(providerId)}/models/${encodeURIComponent(
        modelId,
      )}/config`,
    ),

  updateModelRuntimeConfig: async (
    providerId: string,
    modelId: string,
    body: ModelRuntimeConfigUpdate,
  ) => {
    const result = await request<ModelRuntimeConfig>(
      `/models/${encodeURIComponent(providerId)}/models/${encodeURIComponent(
        modelId,
      )}/config`,
      { method: "PUT", body: JSON.stringify(body) },
    );
    useProviderModelStore
      .getState()
      .invalidate({ providers: true, active: false });
    return result;
  },

  /* ---- Test Connection ---- */

  testProviderConnection: (providerId: string, body?: TestProviderRequest) =>
    request<TestConnectionResponse>(
      `/models/${encodeURIComponent(providerId)}/test`,
      {
        method: "POST",
        body: body ? JSON.stringify(body) : undefined,
      },
    ),

  testModelConnection: (providerId: string, body: TestModelRequest) =>
    request<TestConnectionResponse>(
      `/models/${encodeURIComponent(providerId)}/models/test`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  discoverModels: (providerId: string, body?: TestProviderRequest) =>
    request<DiscoverModelsResponse>(
      `/models/${encodeURIComponent(providerId)}/discover`,
      {
        method: "POST",
        body: body ? JSON.stringify(body) : undefined,
      },
    ),

  probeMultimodal: (providerId: string, modelId: string) =>
    request<ProbeMultimodalResponse>(
      `/models/${encodeURIComponent(providerId)}/models/${encodeURIComponent(
        modelId,
      )}/probe-multimodal`,
      { method: "POST" },
    ),
};
