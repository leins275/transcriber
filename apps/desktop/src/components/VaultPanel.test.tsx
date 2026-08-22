import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { VaultPanel } from "./VaultPanel";
import type { VaultMeetingView } from "../types";

function buildEntry(id: string): VaultMeetingView {
  return {
    id,
    project: "ELS",
    meeting_name: "260812 - Security issue",
    meeting_dir: "D:\\Meetings\\ELS\\260812 - Security issue",
    has_source: true,
    has_transcript: true,
  };
}

describe("VaultPanel", () => {
  it("exposes a Vault region and the entry count", () => {
    const entries = [buildEntry("a"), buildEntry("b")];
    render(<VaultPanel entries={entries} onReveal={() => {}} />);
    expect(screen.getByRole("region", { name: /vault/i })).toBeInTheDocument();
    expect(screen.getByText(/2 in vault/i)).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
  });

  it("renders nothing when the vault is empty (the hero drop zone owns the empty state)", () => {
    const { container } = render(<VaultPanel entries={[]} onReveal={() => {}} />);
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole("region", { name: /vault/i })).not.toBeInTheDocument();
  });
});
