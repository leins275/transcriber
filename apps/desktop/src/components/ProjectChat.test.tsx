import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ProjectChat } from "./ProjectChat";
import type { ChatDisplayMessage } from "../state/useChat";

function renderChat(props: Partial<React.ComponentProps<typeof ProjectChat>> = {}) {
  const defaults = {
    project: "ELS",
    messages: [] as ChatDisplayMessage[],
    streaming: false,
    error: null,
    onSend: () => {},
    onStop: () => {},
    onOpenSource: () => {},
  };
  return render(<ProjectChat {...defaults} {...props} />);
}

describe("ProjectChat", () => {
  it("sends a typed question on Enter and clears the draft", async () => {
    const onSend = vi.fn();
    const user = userEvent.setup();
    renderChat({ onSend });

    const box = screen.getByRole("textbox", { name: /ask about/i });
    await user.type(box, "когда дедлайн?{Enter}");

    expect(onSend).toHaveBeenCalledWith("когда дедлайн?");
    expect(box).toHaveValue("");
  });

  it("shows Stop instead of Send while an answer streams", async () => {
    const onStop = vi.fn();
    const user = userEvent.setup();
    renderChat({
      streaming: true,
      onStop,
      messages: [
        { role: "user", content: "q" },
        { role: "assistant", content: "part of an ans" },
      ],
    });

    expect(screen.queryByRole("button", { name: /send/i })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /stop/i }));

    expect(onStop).toHaveBeenCalled();
  });

  it("renders assistant markdown in a live log and opens a cited source", async () => {
    const onOpenSource = vi.fn();
    const user = userEvent.setup();
    renderChat({
      onOpenSource,
      messages: [
        { role: "user", content: "когда дедлайн?" },
        {
          role: "assistant",
          content: "# Answer\n\nВ пятницу [S1].",
          sources: [
            {
              entry_id: "v-1",
              kind: "transcript",
              meeting_name: "260831 - Weekly sync",
              project: "ELS",
              snippet: "…",
              score: 0.5,
              start_sec: 12,
              timestamp: "0:12",
            },
          ],
        },
      ],
    });

    expect(screen.getByRole("log")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Answer" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /open 260831 - weekly sync/i }));
    expect(onOpenSource).toHaveBeenCalledWith("v-1");
  });

  it("a disabled reason disables the form and explains itself", () => {
    renderChat({ disabledReason: "Chat needs the local language model." });

    expect(screen.getByRole("textbox", { name: /ask about/i })).toBeDisabled();
    expect(screen.getByText(/needs the local language model/i)).toBeInTheDocument();
  });

  it("surfaces a stream error as an alert", () => {
    renderChat({ error: "the model exploded" });

    expect(screen.getByRole("alert")).toHaveTextContent(/the model exploded/i);
  });
});
