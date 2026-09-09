import { describe, expect, it } from "vitest";
import type { ActiveModelsInfo, ProviderInfo } from "@/api/types";
import {
  buildExecutionModelOptions,
  buildTenantDefaultModelLabel,
  formatExecutionModelLabel,
} from "./useExecutionModelOptions";

const providerFixtures = [
  {
    id: "openai",
    name: "OpenAI",
    api_key_prefix: "sk-",
    chat_model: "gpt-5.4",
    models: [
      {
        id: "gpt-5.4",
        name: "GPT-5.4",
        supports_multimodal: null,
        supports_image: null,
        supports_video: null,
      },
    ],
    extra_models: [
      {
        id: "gpt-4.1",
        name: "GPT-4.1",
        supports_multimodal: null,
        supports_image: null,
        supports_video: null,
      },
    ],
    is_custom: false,
    is_local: false,
    support_model_discovery: true,
    support_connection_check: true,
    freeze_url: false,
    require_api_key: true,
    api_key: "sk-test",
    base_url: "https://api.openai.com",
  },
  {
    id: "empty",
    name: "Empty",
    api_key_prefix: "sk-",
    chat_model: "",
    models: [],
    extra_models: [],
    is_custom: false,
    is_local: false,
    support_model_discovery: false,
    support_connection_check: false,
    freeze_url: false,
    require_api_key: true,
    api_key: "sk-empty",
    base_url: "https://example.com",
  },
] satisfies ProviderInfo[];

describe("useExecutionModelOptions helpers", () => {
  it("builds options from configured provider models and extra models", () => {
    const options = buildExecutionModelOptions(providerFixtures);

    expect(options).toEqual([
      {
        key: "openai::gpt-5.4",
        providerId: "openai",
        providerName: "OpenAI",
        model: "gpt-5.4",
        modelName: "GPT-5.4",
        label: "OpenAI / GPT-5.4",
      },
      {
        key: "openai::gpt-4.1",
        providerId: "openai",
        providerName: "OpenAI",
        model: "gpt-4.1",
        modelName: "GPT-4.1",
        label: "OpenAI / GPT-4.1",
      },
    ]);
  });

  it("formats tenant default label from the active model", () => {
    const options = buildExecutionModelOptions(providerFixtures);
    const activeModels: ActiveModelsInfo = {
      active_llm: {
        provider_id: "openai",
        model: "gpt-5.4",
      },
    };

    expect(buildTenantDefaultModelLabel(activeModels, options)).toBe(
      "租户默认模型 (OpenAI / GPT-5.4)",
    );
  });

  it("formats explicit model labels and falls back to tenant default label", () => {
    const options = buildExecutionModelOptions(providerFixtures);

    expect(
      formatExecutionModelLabel(
        { provider_id: "openai", model: "gpt-4.1" },
        options,
        "租户默认模型 (OpenAI / GPT-5.4)",
      ),
    ).toBe("OpenAI / GPT-4.1");
    expect(
      formatExecutionModelLabel(
        undefined,
        options,
        "租户默认模型 (OpenAI / GPT-5.4)",
      ),
    ).toBe("租户默认模型 (OpenAI / GPT-5.4)");
  });
});
