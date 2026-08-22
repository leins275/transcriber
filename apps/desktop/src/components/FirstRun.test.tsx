import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FirstRun } from "./FirstRun";

describe("FirstRun", () => {
  it("asks the user to pick a meetings folder as step one, before it is chosen", () => {
    render(<FirstRun meetingsRoot={null} onChooseFolder={() => {}} modelStep={null} />);
    expect(screen.getByText(/choose where meetings live/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /choose folder/i })).toBeInTheDocument();
  });

  it("invokes onChooseFolder from a keyboard-reachable button", async () => {
    const onChooseFolder = vi.fn();
    const user = userEvent.setup();
    render(<FirstRun meetingsRoot={null} onChooseFolder={onChooseFolder} modelStep={null} />);
    await user.click(screen.getByRole("button", { name: /choose folder/i }));
    expect(onChooseFolder).toHaveBeenCalledTimes(1);
  });

  it("exposes no drop affordance", () => {
    render(<FirstRun meetingsRoot={null} onChooseFolder={() => {}} modelStep={null} />);
    expect(screen.queryByRole("region", { name: /drop/i })).not.toBeInTheDocument();
  });

  it("once a folder is chosen, shows it with a Change control instead of the folder button", () => {
    // Note: a plain JSX attribute string does not process JS escapes the way
    // a `{"..."}` expression does, so the path is passed as an expression
    // here to keep the single backslash it needs.
    render(
      <FirstRun meetingsRoot={"D:\\MeetingVault"} onChooseFolder={() => {}} modelStep={null} />,
    );
    expect(screen.getByText("D:\\MeetingVault")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /change/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^choose folder/i })).not.toBeInTheDocument();
  });

  it("renders the model-download step content once it is known", () => {
    render(
      <FirstRun
        meetingsRoot={"D:\\MeetingVault"}
        onChooseFolder={() => {}}
        modelStep={<p>the model step content</p>}
      />,
    );
    expect(screen.getByText("the model step content")).toBeInTheDocument();
  });

  it("shows a waiting placeholder for step two while the model step is not yet known", () => {
    render(<FirstRun meetingsRoot={null} onChooseFolder={() => {}} modelStep={null} />);
    expect(screen.getByText(/waiting for the transcription service/i)).toBeInTheDocument();
  });

  it("describes the GPU runtime as step three and optional", () => {
    render(<FirstRun meetingsRoot={null} onChooseFolder={() => {}} modelStep={null} />);
    expect(screen.getByText(/gpu acceleration/i)).toBeInTheDocument();
    expect(screen.getByText(/optional/i)).toBeInTheDocument();
  });
});
