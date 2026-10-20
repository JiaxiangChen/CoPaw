import { render, screen, waitFor } from "@testing-library/react";
import { useEffect } from "react";
import { describe, expect, it, vi } from "vitest";
import { Form } from "@agentscope-ai/design";
import { CronJobFormBody } from "./CronJobFormBody";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => key,
  }),
}));

describe("CronJobFormBody", () => {
  it("新版编辑可以锁定启停字段，旧表单默认不锁定", () => {
    const Wrapper = ({ disabled }: { disabled: boolean }) => {
      const [form] = Form.useForm();
      return (
        <Form form={form}>
          <CronJobFormBody
            form={form}
            executionModelOptions={[]}
            executionModelLoading={false}
            tenantDefaultModelLabel="默认"
            enabledDisabled={disabled}
          />
        </Form>
      );
    };
    const { rerender } = render(<Wrapper disabled />);
    expect(screen.getByRole("switch")).toBeDisabled();
    rerender(<Wrapper disabled={false} />);
    expect(screen.getByRole("switch")).not.toBeDisabled();
  });
  it("does not render the batch dispatch switch in the create/edit form", () => {
    const Wrapper = () => {
      const [form] = Form.useForm();

      return (
        <Form form={form}>
          <CronJobFormBody
            form={form}
            executionModelOptions={[]}
            executionModelLoading={false}
            tenantDefaultModelLabel="Tenant default"
          />
        </Form>
      );
    };

    render(<Wrapper />);

    expect(screen.queryByText("批调度")).not.toBeInTheDocument();
    expect(
      screen.queryByText("cronJobs.broadcastDispatchIntents"),
    ).not.toBeInTheDocument();
  });

  it("renders bound skill ids as an optional multi-select", () => {
    const Wrapper = () => {
      const [form] = Form.useForm();

      return (
        <Form form={form}>
          <CronJobFormBody
            form={form}
            executionModelOptions={[]}
            executionModelLoading={false}
            tenantDefaultModelLabel="Tenant default"
            skillOptions={[
              {
                value: "skill-a",
                label: "数据分析 (skill-a)",
              },
            ]}
            skillOptionsLoading={false}
          />
        </Form>
      );
    };

    render(<Wrapper />);

    const field = screen.getByLabelText("绑定技能ID");
    expect(field.closest(".ant-select")).toHaveClass("ant-select-multiple");
    expect(screen.queryByPlaceholderText("skill-a, skill_b")).toBeNull();
  });

  it("shows the workflow type as a skill task without Agent input fields", () => {
    const Wrapper = () => {
      const [form] = Form.useForm();
      return (
        <Form form={form} initialValues={{ task_type: "workflow" }}>
          <CronJobFormBody
            form={form}
            executionModelOptions={[]}
            executionModelLoading={false}
            tenantDefaultModelLabel="Tenant default"
          />
        </Form>
      );
    };

    render(<Wrapper />);

    expect(screen.getByText("技能任务")).toBeInTheDocument();
    expect(screen.queryByLabelText("cronJobs.requestInput")).toBeNull();
    expect(screen.queryByLabelText("cronJobs.text")).toBeNull();
  });

  it("shows existing bound skill labels after options load asynchronously", async () => {
    const Wrapper = ({
      skillOptions,
    }: {
      skillOptions: Array<{ value: string; label: string }>;
    }) => {
      const [form] = Form.useForm();

      useEffect(() => {
        form.setFieldsValue({ skillIds: ["skill-a"] });
      }, [form]);

      return (
        <Form form={form}>
          <CronJobFormBody
            form={form}
            executionModelOptions={[]}
            executionModelLoading={false}
            tenantDefaultModelLabel="Tenant default"
            skillOptions={skillOptions}
            skillOptionsLoading={false}
          />
        </Form>
      );
    };

    const { rerender } = render(<Wrapper skillOptions={[]} />);

    rerender(
      <Wrapper
        skillOptions={[
          {
            value: "skill-a",
            label: "分析技能 (skill-a)",
          },
        ]}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("分析技能 (skill-a)")).toBeInTheDocument();
    });
  });
});
