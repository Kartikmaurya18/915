import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Badge, DeltaBadge, Signed } from "../src/ui/Badge";
import { Button } from "../src/ui/Button";
import { Field, Input } from "../src/ui/fields";
import { Segmented } from "../src/ui/Segmented";
import { cn } from "../src/ui/cn";

afterEach(cleanup);

describe("DeltaBadge: direction never depends on colour alone", () => {
  it("shows a sign, a word for screen readers and an arrow for gains", () => {
    const { container } = render(<DeltaBadge value={2.5} percent />);
    expect(container.textContent).toContain("+2.50%");
    expect(container.textContent).toContain("Up");
    expect(container.querySelector("svg")).toBeTruthy();
  });
  it("shows a minus and 'Down' for losses", () => {
    const { container } = render(<DeltaBadge value={-1.25} />);
    expect(container.textContent).toContain("-1.25");
    expect(container.textContent).toContain("Down");
  });
  it("says unchanged for zero and a dash for missing values", () => {
    const zero = render(<DeltaBadge value={0} />);
    expect(zero.container.textContent).toContain("Unchanged");
    zero.unmount();
    const none = render(<DeltaBadge value={null} />);
    expect(none.container.textContent).toBe("—");
  });
  it("Signed prefixes gains with + and leaves losses with their own minus", () => {
    const { container } = render(
      <>
        <Signed value={5} />
        <Signed value={-5} />
      </>,
    );
    expect(container.textContent).toBe("+5.00-5.00");
  });
  it("Badge renders its children", () => {
    render(<Badge tone="warn">Heads up</Badge>);
    expect(screen.getByText("Heads up")).toBeTruthy();
  });
});

describe("Segmented (tabs pattern)", () => {
  function Harness({ onChange = () => undefined }: { onChange?: (v: string) => void }) {
    const [value, setValue] = useState("a");
    return (
      <Segmented
        label="Choose"
        value={value}
        onChange={(v) => {
          setValue(v);
          onChange(v);
        }}
        options={[
          { value: "a", label: "A" },
          { value: "b", label: "B" },
          { value: "c", label: "C" },
        ]}
      />
    );
  }

  it("has one tab stop: only the selected tab is focusable", () => {
    render(<Harness />);
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((t) => t.getAttribute("tabindex"))).toEqual(["0", "-1", "-1"]);
    expect(tabs.map((t) => t.getAttribute("aria-selected"))).toEqual(["true", "false", "false"]);
    expect(screen.getByRole("tablist").getAttribute("aria-label")).toBe("Choose");
  });

  it("arrow keys move and select, wrapping at the ends; Home and End jump", () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const list = screen.getByRole("tablist");
    fireEvent.keyDown(list, { key: "ArrowRight" });
    expect(onChange).toHaveBeenLastCalledWith("b");
    fireEvent.keyDown(list, { key: "ArrowRight" });
    fireEvent.keyDown(list, { key: "ArrowRight" });
    expect(onChange).toHaveBeenLastCalledWith("a"); // wrapped
    fireEvent.keyDown(list, { key: "ArrowLeft" });
    expect(onChange).toHaveBeenLastCalledWith("c");
    fireEvent.keyDown(list, { key: "Home" });
    expect(onChange).toHaveBeenLastCalledWith("a");
    fireEvent.keyDown(list, { key: "End" });
    expect(onChange).toHaveBeenLastCalledWith("c");
  });

  it("moves keyboard focus to the newly selected tab", () => {
    render(<Harness />);
    fireEvent.keyDown(screen.getByRole("tablist"), { key: "ArrowRight" });
    expect(document.activeElement).toBe(screen.getByRole("tab", { name: "B" }));
  });

  it("ignores unrelated keys", () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    fireEvent.keyDown(screen.getByRole("tablist"), { key: "x" });
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("Button", () => {
  it("blocks clicks and announces busy while loading", () => {
    const onClick = vi.fn();
    render(
      <Button loading onClick={onClick}>
        Save
      </Button>,
    );
    const button = screen.getByRole("button", { name: "Save" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.getAttribute("aria-busy")).toBe("true");
    fireEvent.click(button);
    expect(onClick).not.toHaveBeenCalled();
  });
  it("is a plain button by default so it never submits a form by accident", () => {
    render(<Button>Go</Button>);
    expect(screen.getByRole("button").getAttribute("type")).toBe("button");
  });
});

describe("Field", () => {
  it("connects the label and the error to the control", () => {
    render(
      <Field label="Quantity" hint="Whole numbers" error="Enter a quantity.">
        {(p) => <Input {...p} />}
      </Field>,
    );
    const input = screen.getByLabelText("Quantity");
    expect(input.getAttribute("aria-invalid")).toBe("true");
    const described = input.getAttribute("aria-describedby") ?? "";
    expect(document.getElementById(described)?.textContent).toBe("Enter a quantity."); // the error replaces the hint
    expect(screen.getByRole("alert").textContent).toBe("Enter a quantity.");
  });
  it("shows the hint when there is no error", () => {
    render(
      <Field label="Strike" hint="At the money by default">
        {(p) => <Input {...p} />}
      </Field>,
    );
    const input = screen.getByLabelText("Strike");
    expect(input.getAttribute("aria-invalid")).toBeNull();
    expect(document.getElementById(input.getAttribute("aria-describedby")!)?.textContent).toBe("At the money by default");
  });
});

describe("cn", () => {
  it("joins truthy class names", () => {
    expect(cn("a", false, null, undefined, "b")).toBe("a b");
  });
});
