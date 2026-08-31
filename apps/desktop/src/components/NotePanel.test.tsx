import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NotePanel } from "./NotePanel";
import type { NoteView } from "../types";

function buildNote(overrides: Partial<NoteView> = {}): NoteView {
  return {
    entry_id: "v-1",
    path: "D:\\Meetings\\ELS\\260812 - Security issue\\note.md",
    markdown: null,
    ...overrides,
  };
}

const noSave = () => Promise.resolve();

describe("NotePanel", () => {
  it("renders a note that exists, with an Edit button", async () => {
    render(
      <NotePanel
        entryId="v-1"
        onLoad={() => Promise.resolve(buildNote({ markdown: "# Agenda\n\nFollow up." }))}
        onSave={noSave}
      />,
    );

    expect(await screen.findByText(/Follow up\./)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument();
  });

  it("offers Add note and names the exact path when there is none", async () => {
    render(<NotePanel entryId="v-1" onLoad={() => Promise.resolve(buildNote())} onSave={noSave} />);

    expect(await screen.findByText(/no note for this meeting yet/i)).toBeInTheDocument();
    expect(screen.getByText(/note\.md/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add note" })).toBeInTheDocument();
  });

  it("enters edit mode from the empty state and saves the draft", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(<NotePanel entryId="v-1" onLoad={() => Promise.resolve(buildNote())} onSave={onSave} />);

    await user.click(await screen.findByRole("button", { name: "Add note" }));
    await user.type(screen.getByRole("textbox", { name: "Meeting note" }), "remember the demo");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(onSave).toHaveBeenCalledWith("v-1", "remember the demo");
    // Back in view mode, rendering what was just saved.
    expect(await screen.findByText(/remember the demo/)).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Meeting note" })).not.toBeInTheDocument();
  });

  it("previews the draft with the markdown renderer and returns to editing", async () => {
    const user = userEvent.setup();
    render(<NotePanel entryId="v-1" onLoad={() => Promise.resolve(buildNote())} onSave={noSave} />);

    await user.click(await screen.findByRole("button", { name: "Add note" }));
    await user.type(screen.getByRole("textbox", { name: "Meeting note" }), "# Big heading");
    await user.click(screen.getByRole("button", { name: "Preview" }));

    expect(screen.getByRole("heading", { name: "Big heading" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Meeting note" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Keep editing" }));
    expect(screen.getByRole("textbox", { name: "Meeting note" })).toHaveValue("# Big heading");
  });

  it("cancel drops the draft and keeps the saved note", async () => {
    const user = userEvent.setup();
    render(
      <NotePanel
        entryId="v-1"
        onLoad={() => Promise.resolve(buildNote({ markdown: "kept" }))}
        onSave={noSave}
      />,
    );

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.type(screen.getByRole("textbox", { name: "Meeting note" }), " discarded");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.getByText("kept")).toBeInTheDocument();
  });

  it("reports dirty on edit and clean after save", async () => {
    const onDirtyChange = vi.fn();
    const user = userEvent.setup();
    render(
      <NotePanel
        entryId="v-1"
        onLoad={() => Promise.resolve(buildNote())}
        onSave={noSave}
        onDirtyChange={onDirtyChange}
      />,
    );

    await user.click(await screen.findByRole("button", { name: "Add note" }));
    await user.type(screen.getByRole("textbox", { name: "Meeting note" }), "x");
    expect(onDirtyChange).toHaveBeenLastCalledWith(true);

    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
  });

  it("reports its content up for the page's per-tab Copy", async () => {
    const onContentChange = vi.fn();
    render(
      <NotePanel
        entryId="v-1"
        onLoad={() => Promise.resolve(buildNote({ markdown: "copy me" }))}
        onSave={noSave}
        onContentChange={onContentChange}
      />,
    );
    await screen.findByText("copy me");

    expect(onContentChange).toHaveBeenLastCalledWith("copy me");
  });

  it("surfaces a save failure and stays in edit mode with the draft intact", async () => {
    const user = userEvent.setup();
    render(
      <NotePanel
        entryId="v-1"
        onLoad={() => Promise.resolve(buildNote())}
        onSave={() => Promise.reject({ kind: "io", message: "disk full" })}
      />,
    );

    await user.click(await screen.findByRole("button", { name: "Add note" }));
    await user.type(screen.getByRole("textbox", { name: "Meeting note" }), "precious");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/disk full/i);
    expect(screen.getByRole("textbox", { name: "Meeting note" })).toHaveValue("precious");
  });

  it("surfaces a read failure instead of pretending there is no note", async () => {
    render(
      <NotePanel
        entryId="v-1"
        onLoad={() => Promise.reject({ kind: "io", message: "permission denied" })}
        onSave={noSave}
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(/permission denied/i);
    expect(screen.queryByText(/no note for this meeting yet/i)).not.toBeInTheDocument();
  });
});
