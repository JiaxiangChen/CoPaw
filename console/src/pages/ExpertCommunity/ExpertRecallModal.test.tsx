import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ExpertRecallModal } from "./ExpertRecallModal";

const mocks = vi.hoisted(() => ({
  getExpertDistributions: vi.fn(),
  recallExpert: vi.fn(),
}));

vi.mock("../../api/modules/market", () => ({
  marketApi: mocks,
}));

vi.mock("../../components/TenantSelector", () => ({
  TenantSelector: ({ allowedTenantIds }: { allowedTenantIds: string[] }) => (
    <div data-testid="holder-list">{allowedTenantIds.join(",")}</div>
  ),
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

describe("ExpertRecallModal", () => {
  it("ignores a holder response for an older expert after switching items", async () => {
    const first = deferred<Array<{ target_user_id: string }>>();
    const second = deferred<Array<{ target_user_id: string }>>();
    mocks.getExpertDistributions
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);

    const { rerender } = render(
      <ExpertRecallModal
        open
        sourceId="source"
        itemId="expert-a"
        itemName="A"
        onClose={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );
    rerender(
      <ExpertRecallModal
        open
        sourceId="source"
        itemId="expert-b"
        itemName="B"
        onClose={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    second.resolve([{ target_user_id: "holder-b" }]);
    await waitFor(() =>
      expect(screen.getByTestId("holder-list")).toHaveTextContent("holder-b"),
    );

    first.resolve([{ target_user_id: "holder-a" }]);
    await waitFor(() =>
      expect(screen.getByTestId("holder-list")).toHaveTextContent("holder-b"),
    );
    expect(screen.getByTestId("holder-list")).not.toHaveTextContent("holder-a");
  });
});
