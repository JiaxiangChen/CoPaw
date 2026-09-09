import { create } from "zustand";
import { request } from "../api/request";
import type {
  ActiveModelsInfo,
  GetActiveModelsRequest,
  ModelRuntimeConfig,
  ProviderInfo,
} from "../api/types";
import { getIframeContext } from "./iframeStore";

const MODEL_DATA_TTL_MS = 5_000;

export interface ProviderModelData {
  providers: ProviderInfo[];
  activeModels: ActiveModelsInfo | null;
}

interface ProviderCacheEntry {
  providers: ProviderInfo[];
  loadedAt: number;
}

interface ActiveCacheEntry {
  activeModels: ActiveModelsInfo | null;
  loadedAt: number;
}

interface ProviderModelState {
  providers: ProviderInfo[];
  activeModels: ActiveModelsInfo | null;
  loading: boolean;
  error: string | null;
  loadModelData: (
    params?: GetActiveModelsRequest,
  ) => Promise<ProviderModelData>;
  loadActiveModelData: (
    params?: GetActiveModelsRequest,
  ) => Promise<ActiveModelsInfo | null>;
  setActiveModels: (activeModels: ActiveModelsInfo | null) => void;
  setModelRuntimeConfig: (
    providerId: string,
    modelId: string,
    config: ModelRuntimeConfig,
  ) => void;
  invalidate: (options?: { providers?: boolean; active?: boolean }) => void;
  reset: () => void;
}

const providerCache = new Map<string, ProviderCacheEntry>();
const activeCache = new Map<string, ActiveCacheEntry>();
const providerInflight = new Map<string, Promise<ProviderInfo[]>>();
const activeInflight = new Map<string, Promise<ActiveModelsInfo | null>>();
let providerCacheGeneration = 0;
let activeCacheGeneration = 0;

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

function runtimeIdentityKey(): string {
  const context = getIframeContext();
  return JSON.stringify({
    userId: context.userId,
    source: context.source,
    space: context.space,
    bbk: context.bbk,
    orgCode: context.orgCode,
    positionId: context.positionId,
    manager: context.manager,
    isSuperManager: context.isSuperManager,
    authHeaders: context.authHeaders,
  });
}

function activeParamsKey(params?: GetActiveModelsRequest): string {
  return JSON.stringify({
    scope: params?.scope ?? "effective",
    agent_id: params?.agent_id ?? null,
  });
}

function providerCacheKey(): string {
  return runtimeIdentityKey();
}

function activeCacheKey(params?: GetActiveModelsRequest): string {
  return `${runtimeIdentityKey()}::${activeParamsKey(params)}`;
}

function isFresh(entry: { loadedAt: number }): boolean {
  return Date.now() - entry.loadedAt < MODEL_DATA_TTL_MS;
}

function mergeModelRuntimeConfig(
  providers: ProviderInfo[],
  providerId: string,
  modelId: string,
  config: ModelRuntimeConfig,
): ProviderInfo[] {
  return providers.map((provider) =>
    provider.id === providerId
      ? {
          ...provider,
          model_configs: { ...provider.model_configs, [modelId]: config },
        }
      : provider,
  );
}

function readFreshProviders(key: string): ProviderInfo[] | null {
  const entry = providerCache.get(key);
  if (!entry) return null;
  if (!isFresh(entry)) {
    providerCache.delete(key);
    return null;
  }
  return entry.providers;
}

function readFreshActive(key: string): ActiveModelsInfo | null | undefined {
  const entry = activeCache.get(key);
  if (!entry) return undefined;
  if (!isFresh(entry)) {
    activeCache.delete(key);
    return undefined;
  }
  return entry.activeModels;
}

async function loadProviders(key: string): Promise<ProviderInfo[]> {
  const cached = readFreshProviders(key);
  if (cached) return cached;

  const pending = providerInflight.get(key);
  if (pending) return pending;

  const loadGeneration = providerCacheGeneration;
  const loadPromise: Promise<ProviderInfo[]> = request<ProviderInfo[]>(
    "/models",
  )
    .then((providers) => (Array.isArray(providers) ? providers : []))
    .then((providers) => {
      if (loadGeneration !== providerCacheGeneration) {
        return readFreshProviders(key) ?? [];
      }
      providerCache.set(key, { providers, loadedAt: Date.now() });
      return providers;
    })
    .finally(() => {
      if (providerInflight.get(key) === loadPromise) {
        providerInflight.delete(key);
      }
    });

  providerInflight.set(key, loadPromise);
  return loadPromise;
}

async function loadActiveModels(
  key: string,
  params?: GetActiveModelsRequest,
): Promise<ActiveModelsInfo | null> {
  const cached = readFreshActive(key);
  if (cached !== undefined) return cached;

  const pending = activeInflight.get(key);
  if (pending) return pending;

  const loadGeneration = activeCacheGeneration;
  const loadPromise: Promise<ActiveModelsInfo | null> =
    request<ActiveModelsInfo>(
      buildActiveModelQuery(params ?? { scope: "effective" }),
    )
      .then((activeModels) => activeModels || null)
      .then((activeModels) => {
        if (loadGeneration !== activeCacheGeneration) {
          return readFreshActive(key) ?? null;
        }
        activeCache.set(key, { activeModels, loadedAt: Date.now() });
        return activeModels;
      })
      .finally(() => {
        if (activeInflight.get(key) === loadPromise) {
          activeInflight.delete(key);
        }
      });

  activeInflight.set(key, loadPromise);
  return loadPromise;
}

function initialState() {
  return {
    providers: [],
    activeModels: null,
    loading: false,
    error: null,
  };
}

export const useProviderModelStore = create<ProviderModelState>((set) => ({
  ...initialState(),

  async loadModelData(params) {
    const providerKey = providerCacheKey();
    const activeKey = activeCacheKey(params);
    const providerLoadGeneration = providerCacheGeneration;
    const activeLoadGeneration = activeCacheGeneration;
    set({ loading: true, error: null });
    return Promise.all([
      loadProviders(providerKey),
      loadActiveModels(activeKey, params),
    ])
      .then(([providers, activeModels]) => {
        const data: ProviderModelData = {
          providers,
          activeModels: activeModels || null,
        };
        set((state) => ({
          providers:
            providerLoadGeneration === providerCacheGeneration
              ? providers
              : state.providers,
          activeModels:
            activeLoadGeneration === activeCacheGeneration
              ? activeModels || null
              : state.activeModels,
          loading: false,
          error: null,
        }));
        return data;
      })
      .catch((error) => {
        const message = error instanceof Error ? error.message : String(error);
        set((state) => ({
          providers:
            providerLoadGeneration === providerCacheGeneration
              ? []
              : state.providers,
          activeModels:
            activeLoadGeneration === activeCacheGeneration
              ? null
              : state.activeModels,
          loading: false,
          error:
            providerLoadGeneration === providerCacheGeneration ||
            activeLoadGeneration === activeCacheGeneration
              ? message
              : state.error,
        }));
        throw error;
      });
  },

  async loadActiveModelData(params) {
    const activeKey = activeCacheKey(params);
    const activeLoadGeneration = activeCacheGeneration;
    const activeModels = await loadActiveModels(activeKey, params);
    set((state) => ({
      activeModels:
        activeLoadGeneration === activeCacheGeneration
          ? activeModels || null
          : state.activeModels,
    }));
    return activeModels;
  },

  setActiveModels(activeModels) {
    for (const [key] of activeCache) {
      activeCache.set(key, { activeModels, loadedAt: Date.now() });
    }
    set({ activeModels });
  },

  setModelRuntimeConfig(providerId, modelId, config) {
    const key = providerCacheKey();
    const cached = providerCache.get(key);
    if (cached) {
      providerCache.set(key, {
        providers: mergeModelRuntimeConfig(
          cached.providers,
          providerId,
          modelId,
          config,
        ),
        loadedAt: cached.loadedAt,
      });
    }
    set((state) => ({
      providers: mergeModelRuntimeConfig(
        state.providers,
        providerId,
        modelId,
        config,
      ),
    }));
  },

  invalidate(options) {
    const clearProviders = options?.providers ?? true;
    const clearActive = options?.active ?? true;

    if (clearProviders && clearActive) {
      providerCacheGeneration += 1;
      activeCacheGeneration += 1;
      providerCache.clear();
      activeCache.clear();
      providerInflight.clear();
      activeInflight.clear();
      set(initialState());
      return;
    }

    if (clearProviders) {
      providerCacheGeneration += 1;
      providerCache.clear();
      providerInflight.clear();
    }

    if (clearActive) {
      activeCacheGeneration += 1;
      activeCache.clear();
      activeInflight.clear();
    }

    set((state) => ({
      providers: clearProviders ? [] : state.providers,
      activeModels: clearActive ? null : state.activeModels,
    }));
  },

  reset() {
    providerCacheGeneration += 1;
    activeCacheGeneration += 1;
    providerCache.clear();
    activeCache.clear();
    providerInflight.clear();
    activeInflight.clear();
    set(initialState());
  },
}));
