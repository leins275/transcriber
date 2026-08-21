import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SettingsBar } from "./SettingsBar";
import type { SettingsView } from "../types";

function buildSettings(overrides: Partial<SettingsView> = {}): SettingsView {
  return {
    meetings_root: "D:\\Meetings",
    meetings_root_exists: true,
    service_base_url: null,
    supported_extensions: [".mp4", ".wav"],
    config_error: null,
    ...overrides,
  };
}

describe("SettingsBar", () => {
  it("shows the meetings-root path and a Change control", async () => {
    const onChangeRoot = vi.fn();
    const user = userEvent.setup();
    render(<SettingsBar settings={buildSettings()} onChangeRoot={onChangeRoot} />);
    expect(screen.getByText("D:\\Meetings")).toBeInTheDocument();
    const change = screen.getByRole("button", { name: /change/i });
    await user.click(change);
    expect(onChangeRoot).toHaveBeenCalledTimes(1);
  });

  it("shows an actionable warning when the meetings-root no longer exists", () => {
    render(
      <SettingsBar
        settings={buildSettings({ meetings_root_exists: false })}
        onChangeRoot={() => {}}
      />,
    );
    expect(screen.getByText(/no longer exists|missing|not found/i)).toBeInTheDocument();
  });

  it("does not warn when the meetings-root exists", () => {
    render(<SettingsBar settings={buildSettings()} onChangeRoot={() => {}} />);
    expect(screen.queryByText(/no longer exists|missing|not found/i)).not.toBeInTheDocument();
  });
});
