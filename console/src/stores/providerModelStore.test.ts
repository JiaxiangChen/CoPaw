import { beforeEach, describe, expect, it, vi } from "vitest";
import { request } from "../api/request";
import { useProviderModelStore } from "./providerModelStore";
import { useIframeStore } from "./iframeStore";
import type { ProviderInfo } from "../api/types";

vi.mock("../api/request", () => ({
  request: vi.fn(),
}));

function provider(
  id: string,
  models: ProviderInfo["models"] = [],
): ProviderInfo {
  return {
    id,
    name: id,
    api_key_prefix: "",
    chat_model: "",
    models,
    extra_models: [],
    is_custom: false,
    is_local: false,
    support_model_discovery: false,
    support_connection_check: false,
    freeze_url: false,
    require_api_key: false,
    api_key: "",
    base_url: "http://localhost",
  };
}

describe("providerModelStore", () => {
  beforeEach(() => {
    vi.useRealTimers();
    useProviderModelStore.getState().reset();
    useIframeStore.setState({
      source: "source-a",
      space: "space-a",
      orgCode: "org-a",
      bbk: "bbk-a",
      userId: "user-a",
      authHeaders: [],
    });
    vi.clearAllMocks();
  });

  it("deduplicates concurrent provider and active model loads for the same runtime request identity", async () => {
    vi.mocked(request)
      .mockResolvedValueOnce([
        provider("openai", [
          {
            id: "gpt-4",
            name: "GPT-4",
            supports_multimodal: true,
            supports_image: true,
            supports_video: false,
          },
        ]),
      ])
      .mockResolvedValueOnce({
        active_llm: { provider_id: "openai", model: "gpt-4" },
      });

    const store = useProviderModelStore.getState();
    const [first, second] = await Promise.all([
      store.loadModelData(),
      store.loadModelData(),
    ]);

    expect(first.activeModels?.active_llm?.model).toBe("gpt-4");
    expect(second.providers[0]?.id).toBe("openai");
    expect(request).toHaveBeenCalledTimes(2);
    expect(request).toHaveBeenCalledWith("/models");
    expect(request).toHaveBeenCalledWith("/models/active?scope=effective");
  });

  it("serves cached model data within the ttl and reloads after it expires", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_000);
    vi.mocked(request)
      .mockResolvedValueOnce([provider("openai")])
      .mockResolvedValueOnce({
        active_llm: { provider_id: "openai", model: "gpt-4" },
      })
      .mockResolvedValueOnce([provider("qwen")])
      .mockResolvedValueOnce({
        active_llm: { provider_id: "qwen", model: "qwen-max" },
      });

    const store = useProviderModelStore.getState();
    await store.loadModelData();
    vi.setSystemTime(4_000);
    const cached = await store.loadModelData();
    vi.setSystemTime(7_000);
    const reloaded = await store.loadModelData();

    expect(cached.providers[0]?.id).toBe("openai");
    expect(reloaded.providers[0]?.id).toBe("qwen");
    expect(request).toHaveBeenCalledTimes(4);
  });

  it("shares provider loads across different active model params", async () => {
    vi.mocked(request)
      .mockImplementationOnce(async () => [provider("openai")])
      .mockImplementationOnce(async () => ({
        active_llm: { provider_id: "openai", model: "gpt-4" },
      }))
      .mockImplementationOnce(async () => ({
        active_llm: { provider_id: "openai", model: "gpt-4o" },
      }));

    const store = useProviderModelStore.getState();
    await Promise.all([
      store.loadModelData({ scope: "effective", agent_id: "agent-a" }),
      store.loadModelData({ scope: "effective", agent_id: "agent-b" }),
    ]);

    expect(request).toHaveBeenCalledTimes(3);
    expect(request).toHaveBeenCalledWith("/models");
    expect(request).toHaveBeenCalledWith(
      "/models/active?scope=effective&agent_id=agent-a",
    );
    expect(request).toHaveBeenCalledWith(
      "/models/active?scope=effective&agent_id=agent-b",
    );
  });

  it("loads active model data without fetching the provider list", async () => {
    vi.mocked(request).mockResolvedValueOnce({
      active_llm: { provider_id: "openai", model: "gpt-4" },
    });

    const store = useProviderModelStore.getState();
    const activeModels = await store.loadActiveModelData({
      scope: "effective",
    });

    expect(activeModels?.active_llm?.model).toBe("gpt-4");
    expect(useProviderModelStore.getState().activeModels).toBe(activeModels);
    expect(useProviderModelStore.getState().providers).toEqual([]);
    expect(request).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledWith("/models/active?scope=effective");
  });

  it("does not repopulate provider state or cache from an invalidated in-flight load", async () => {
    let resolveStaleProviders: (providers: ProviderInfo[]) => void = () => {};
    let providerCalls = 0;

    vi.mocked(request).mockImplementation(async (url) => {
      if (url === "/models") {
        providerCalls += 1;
        if (providerCalls === 1) {
          return new Promise<ProviderInfo[]>((resolve) => {
            resolveStaleProviders = resolve;
          });
        }
        return [provider("fresh")];
      }

      return {
        active_llm: { provider_id: "openai", model: "gpt-4" },
      };
    });

    const store = useProviderModelStore.getState();
    const pendingLoad = store.loadModelData();

    expect(request).toHaveBeenCalledWith("/models");

    store.invalidate({ providers: true, active: false });
    resolveStaleProviders([provider("stale")]);
    await pendingLoad;

    expect(useProviderModelStore.getState().providers).toEqual([]);

    const reloaded = await store.loadModelData();

    expect(reloaded.providers[0]?.id).toBe("fresh");
    expect(providerCalls).toBe(2);
  });

  it("keeps a saved model runtime configuration when the model is selected again", async () => {
    vi.mocked(request)
      .mockResolvedValueOnce([
        {
          ...provider("openai"),
          model_configs: {
            "gpt-5": {
              temperature: 0.2,
              supports_enable_thinking: true,
              supported_reasoning_efforts: ["low", "high"],
              enable_thinking: false,
              reasoning_effort: null,
            },
          },
        },
      ])
      .mockResolvedValueOnce({
        active_llm: { provider_id: "openai", model: "gpt-5" },
      });

    const store = useProviderModelStore.getState();
    await store.loadModelData();
    store.setModelRuntimeConfig("openai", "gpt-5", {
      temperature: 0.2,
      supports_enable_thinking: true,
      supported_reasoning_efforts: ["low", "high"],
      enable_thinking: true,
      reasoning_effort: "high",
    });

    const reselected = useProviderModelStore
      .getState()
      .providers.find((item) => item.id === "openai")
      ?.model_configs?.["gpt-5"];

    expect(reselected).toMatchObject({
      enable_thinking: true,
      reasoning_effort: "high",
    });
  });
});
