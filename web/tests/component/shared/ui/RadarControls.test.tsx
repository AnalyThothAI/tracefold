import { RadarControls } from "@shared/ui/RadarControls";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

describe("RadarControls", () => {
  it("uses shared toggle primitives for radar window changes", () => {
    const onWindowChange = vi.fn();

    render(<RadarControls windowKey="1h" onWindowChange={onWindowChange} />);

    const windowGroup = screen.getByLabelText("radar window");
    expect(windowGroup).toHaveAttribute("data-slot", "toggle-group");
    expect(within(windowGroup).getByRole("radio", { name: "1h" })).toHaveAttribute(
      "data-state",
      "on",
    );

    fireEvent.click(within(windowGroup).getByRole("radio", { name: "4h" }));
    expect(onWindowChange).toHaveBeenCalledWith("4h");
    onWindowChange.mockClear();
    fireEvent.click(within(windowGroup).getByRole("radio", { name: "1h" }));
    expect(onWindowChange).not.toHaveBeenCalled();
    expect(screen.queryByLabelText("token flow scope")).not.toBeInTheDocument();
  });
});
