import React from "react";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ExpertSelector from "./index";

const mocks = vi.hoisted(() => {
  const messageError = vi.fn();
  return {
    listExperts: vi.fn(),
    messageError,
    message: { error: messageError },
  };
});

vi.mock("../../../api/modules/experts", () => ({
  expertsApi: {
    listExperts: mocks.listExperts,
  },
}));

vi.mock("../../../hooks/useAppMessage", () => ({
  useAppMessage: () => ({
    message: mocks.message,
  }),
}));

vi.mock("@agentscope-ai/icons", () => ({
  SparkDownLine: () => <span data-testid="expert-selector-arrow" />,
}));

describe("ExpertSelector inline menu", () => {
  beforeEach(() => {
    mocks.listExperts.mockResolvedValue([
      {
        definition_id: "expert-1",
        enabled: true,
        valid: true,
        definition: {
          name: "专家一号",
          description: "处理复杂问题",
        },
      },
    ]);
    mocks.messageError.mockReset();
  });

  afterEach(() => {
    cleanup();
    mocks.listExperts.mockReset();
  });

  it("selects an expert from the inline quick menu", async () => {
    const onChange = vi.fn();

    render(
      <ExpertSelector
        experts={[
          {
            id: "expert-1",
            name: "专家一号",
            description: "处理复杂问题",
          },
        ]}
        loading={false}
        inline
        planModeEnabled={false}
        selectedExpertId={null}
        onChange={onChange}
        onDisablePlanMode={vi.fn()}
      />,
    );

    const expert = await screen.findByRole("menuitem");
    expect(expert).toHaveTextContent("专家一号");
    fireEvent.click(expert);

    await waitFor(() => {
      expect(onChange).toHaveBeenCalledWith("expert-1");
    });
  });

  it("disables inline expert options while Goal Mode is enabled", async () => {
    const onChange = vi.fn();

    render(
      <ExpertSelector
        experts={[
          {
            id: "expert-1",
            name: "专家一号",
            description: "处理复杂问题",
          },
        ]}
        loading={false}
        inline
        planModeEnabled={false}
        goalModeEnabled
        selectedExpertId={null}
        onChange={onChange}
        onDisablePlanMode={vi.fn()}
      />,
    );

    const expert = await screen.findByRole("menuitem");
    expect(expert).toBeDisabled();

    fireEvent.click(expert);
    await waitFor(() => {
      expect(onChange).not.toHaveBeenCalled();
    });
  });

  it("renders the experts supplied by ChatPage without fetching again", async () => {
    mocks.listExperts.mockResolvedValue([]);

    render(
      <ExpertSelector
        experts={[
          {
            id: "expert-1",
            name: "专家一号",
            description: "处理复杂问题",
          },
        ]}
        loading={false}
        inline
        planModeEnabled={false}
        selectedExpertId={null}
        onChange={vi.fn()}
        onDisablePlanMode={vi.fn()}
      />,
    );

    expect(await screen.findByRole("menuitem")).toHaveTextContent("专家一号");
    expect(mocks.listExperts).not.toHaveBeenCalled();
  });
});

it.each([false, true])(
  "keeps the selected expert on repeat click without offering default Agent (persistent=%s)",
  (persistent) => {
    const onChange = vi.fn();
    const view = render(
      <ExpertSelector
        experts={[{ id: "e", name: "专家", description: "" }]}
        persistent={persistent}
        selectedExpertId="e"
        planModeEnabled={false}
        onChange={onChange}
        onDisablePlanMode={vi.fn()}
        inline
      />,
    );
    fireEvent.click(
      within(view.container).getByRole("menuitem", { name: "专家" }),
    );
    expect(onChange).toHaveBeenLastCalledWith("e");
    expect(
      within(view.container).queryByRole("menuitem", { name: /默认 Agent/ }),
    ).toBeNull();
  },
);
it("shows an unavailable bound identity even when filtered from the selectable list", () => {
  render(
    <ExpertSelector
      experts={[]}
      persistent
      selectedExpertId="old-id"
      boundExpertName="已撤回专家"
      unavailable
      planModeEnabled={false}
      onChange={vi.fn()}
      onDisablePlanMode={vi.fn()}
      inline
    />,
  );
  expect(screen.getByText(/已撤回专家.*不可用/)).toBeInTheDocument();
  expect(screen.getByRole("menuitem", { name: /已撤回专家/ })).toBeDisabled();
});
