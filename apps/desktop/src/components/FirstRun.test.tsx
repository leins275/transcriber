import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FirstRun } from "./FirstRun";

describe("FirstRun", () => {
  it("asks the user to pick a meetings folder", () => {
    render(<FirstRun onChooseFolder={() => {}} />);
    expect(screen.getByText(/meetings folder/i)).toBeInTheDocument();
  });

  it("invokes onChooseFolder from a keyboard-reachable button", async () => {
    const onChooseFolder = vi.fn();
    const user = userEvent.setup();
    render(<FirstRun onChooseFolder={onChooseFolder} />);
    await user.click(screen.getByRole("button", { name: /choose.*folder/i }));
    expect(onChooseFolder).toHaveBeenCalledTimes(1);
  });

  it("exposes no drop affordance", () => {
    render(<FirstRun onChooseFolder={() => {}} />);
    expect(screen.queryByRole("region", { name: /drop/i })).not.toBeInTheDocument();
  });
});
