import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { UpdateNotice } from "./UpdateNotice";
import type { UpdateState } from "../lib/update";

const update = { version: "0.3.0", notes: "Speakers in the reading view", date: null };

function renderNotice(
  state: UpdateState,
  props: Partial<React.ComponentProps<typeof UpdateNotice>> = {},
) {
  const defaults = {
    state,
    onInstall: () => {},
    onRestart: () => {},
    onDismiss: () => {},
  };
  return render(<UpdateNotice {...defaults} {...props} />);
}

describe("UpdateNotice", () => {
  it("renders nothing while checking or already current", () => {
    const { container, unmount } = renderNotice({ status: "checking" });
    expect(container).toBeEmptyDOMElement();
    unmount();

    const current = renderNotice({ status: "up-to-date" });
    expect(current.container).toBeEmptyDOMElement();
  });

  it("offers an available update as a status, not an alert", () => {
    // An offer, not a problem: `alert` would interrupt a screen reader
    // mid-sentence for something entirely optional.
    renderNotice({ status: "available", update });

    expect(screen.getByRole("status")).toHaveTextContent("0.3.0");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows the release notes when the manifest carried them", () => {
    renderNotice({ status: "available", update });
    expect(screen.getByText(/speakers in the reading view/i)).toBeInTheDocument();
  });

  it("installs on request", async () => {
    const onInstall = vi.fn();
    const user = userEvent.setup();
    renderNotice({ status: "available", update }, { onInstall });

    await user.click(screen.getByRole("button", { name: /install/i }));

    expect(onInstall).toHaveBeenCalled();
  });

  it("offers no way to cancel mid-download", () => {
    // Half an installer is worse than none; there is nothing useful to
    // offer here, so nothing is offered.
    renderNotice({ status: "downloading", update, percent: 40 });

    expect(screen.queryByRole("button", { name: /dismiss/i })).not.toBeInTheDocument();
    expect(screen.getByText(/40%/)).toBeInTheDocument();
  });

  it("asks for a restart once installed, and lets it wait", async () => {
    const onRestart = vi.fn();
    const onDismiss = vi.fn();
    const user = userEvent.setup();
    renderNotice({ status: "installed", update }, { onRestart, onDismiss });

    await user.click(screen.getByRole("button", { name: /restart now/i }));
    expect(onRestart).toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: /later/i }));
    expect(onDismiss).toHaveBeenCalled();
  });

  it("reports a failed check as an alert, since the app cannot tell if it is current", () => {
    renderNotice({ status: "error", message: "network unreachable" });

    expect(screen.getByRole("alert")).toHaveTextContent(/network unreachable/i);
  });

  it("can be dismissed", async () => {
    const onDismiss = vi.fn();
    const user = userEvent.setup();
    renderNotice({ status: "available", update }, { onDismiss });

    await user.click(screen.getByRole("button", { name: /dismiss/i }));

    expect(onDismiss).toHaveBeenCalled();
  });
});
