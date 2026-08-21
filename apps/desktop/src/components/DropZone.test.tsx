import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { DropZone } from "./DropZone";

describe("DropZone", () => {
  it("renders the idle state", () => {
    render(<DropZone state="idle" disabled={false} onChooseFile={() => {}} />);
    expect(screen.getByRole("region", { name: /drop/i })).toHaveAttribute("data-state", "idle");
  });

  it("renders a distinct hovering state while a drag is over the window", () => {
    render(<DropZone state="hovering" disabled={false} onChooseFile={() => {}} />);
    expect(screen.getByRole("region", { name: /drop/i })).toHaveAttribute("data-state", "hovering");
  });

  it("renders a distinct working state once files are dropped", () => {
    render(<DropZone state="working" disabled={false} onChooseFile={() => {}} />);
    expect(screen.getByRole("region", { name: /drop/i })).toHaveAttribute("data-state", "working");
  });

  it("reverts from hovering back to idle when the drag leaves", () => {
    const { rerender } = render(
      <DropZone state="hovering" disabled={false} onChooseFile={() => {}} />,
    );
    expect(screen.getByRole("region", { name: /drop/i })).toHaveAttribute("data-state", "hovering");
    rerender(<DropZone state="idle" disabled={false} onChooseFile={() => {}} />);
    expect(screen.getByRole("region", { name: /drop/i })).toHaveAttribute("data-state", "idle");
  });

  it("invokes onChooseFile from a keyboard-reachable button", async () => {
    const onChooseFile = vi.fn();
    const user = userEvent.setup();
    render(<DropZone state="idle" disabled={false} onChooseFile={onChooseFile} />);
    await user.click(screen.getByRole("button", { name: /choose file/i }));
    expect(onChooseFile).toHaveBeenCalledTimes(1);
  });

  it("renders a folder prompt and no drop affordance when disabled by first-run", () => {
    render(<DropZone state="idle" disabled onChooseFile={() => {}} />);
    expect(screen.getByText(/choose a meetings folder/i)).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: /drop/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /choose file/i })).not.toBeInTheDocument();
  });
});
