import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MeetingEditor } from "./MeetingEditor";
import type { VaultMeetingView } from "../types";

function buildEntry(overrides: Partial<VaultMeetingView> = {}): VaultMeetingView {
  return {
    id: "v-1",
    project: null,
    meeting_name: "260822 - source",
    meeting_dir: "D:\\Meetings\\unsorted\\260822 - source",
    has_source: true,
    has_transcript: true,
    ...overrides,
  };
}

function renderEditor(props: Partial<React.ComponentProps<typeof MeetingEditor>> = {}) {
  const defaults = {
    entry: buildEntry(),
    projects: ["ELS", "GIS"],
    onSave: () => Promise.resolve(),
    onCancel: () => {},
  };
  return render(<MeetingEditor {...defaults} {...props} />);
}

describe("MeetingEditor", () => {
  it("seeds date and title from the meeting's own folder name", () => {
    renderEditor();
    expect(screen.getByLabelText(/date/i)).toHaveValue("260822");
    expect(screen.getByLabelText(/title/i)).toHaveValue("source");
  });

  it("puts a non-conforming folder name entirely in the title", () => {
    renderEditor({ entry: buildEntry({ meeting_name: "recording final v2" }) });
    expect(screen.getByLabelText(/date/i)).toHaveValue("");
    expect(screen.getByLabelText(/title/i)).toHaveValue("recording final v2");
  });

  it("offers every existing project plus Unsorted and a new code", () => {
    renderEditor();
    const options = screen.getAllByRole("option").map((option) => option.textContent);
    expect(options).toEqual(["Unsorted", "ELS", "GIS", "New project…"]);
  });

  it("files an unsorted recording under a chosen project", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderEditor({ onSave });

    await user.selectOptions(screen.getByLabelText(/project/i), "ELS");
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    expect(onSave).toHaveBeenCalledWith({ project: "ELS", date: "260822", title: "source" });
  });

  it("moves a filed recording back to unsorted", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderEditor({
      entry: buildEntry({ project: "ELS", meeting_name: "260812 - Security issue" }),
      onSave,
    });

    await user.selectOptions(screen.getByLabelText(/project/i), "Unsorted");
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    expect(onSave).toHaveBeenCalledWith({
      project: null,
      date: "260812",
      title: "Security issue",
    });
  });

  it("accepts a project code that does not exist in the vault yet", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderEditor({ onSave });

    await user.selectOptions(screen.getByLabelText(/project/i), "New project…");
    await user.type(screen.getByLabelText(/new code/i), "GIS");
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    expect(onSave).toHaveBeenCalledWith({ project: "GIS", date: "260822", title: "source" });
  });

  it("changes the date", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderEditor({ onSave });

    const date = screen.getByLabelText(/date/i);
    await user.clear(date);
    await user.type(date, "260814");
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    expect(onSave).toHaveBeenCalledWith({ project: null, date: "260814", title: "source" });
  });

  it("blocks Save on an empty title or a malformed date", async () => {
    const user = userEvent.setup();
    renderEditor();

    await user.clear(screen.getByLabelText(/title/i));
    expect(screen.getByRole("button", { name: /^save$/i })).toBeDisabled();

    await user.type(screen.getByLabelText(/title/i), "Weekly sync");
    const date = screen.getByLabelText(/date/i);
    await user.clear(date);
    await user.type(date, "2608");
    expect(screen.getByRole("button", { name: /^save$/i })).toBeDisabled();
  });

  it("previews the folder the save would produce", async () => {
    const user = userEvent.setup();
    renderEditor();

    await user.selectOptions(screen.getByLabelText(/project/i), "ELS");

    expect(screen.getByText(/ELS\\260822 - source/)).toBeInTheDocument();
  });

  it("shows the backend's refusal verbatim instead of guessing at the rules", async () => {
    const onSave = vi.fn().mockRejectedValue({
      kind: "invalid_argument",
      message: "requested meeting name is not usable: title contains a character Windows forbids",
    });
    const user = userEvent.setup();
    renderEditor({ onSave });

    await user.click(screen.getByRole("button", { name: /^save$/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/Windows forbids/);
  });

  it("cancels without saving", async () => {
    const onSave = vi.fn();
    const onCancel = vi.fn();
    const user = userEvent.setup();
    renderEditor({ onSave, onCancel });

    await user.click(screen.getByRole("button", { name: /cancel/i }));

    expect(onCancel).toHaveBeenCalled();
    expect(onSave).not.toHaveBeenCalled();
  });
});
