import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SpeakerPage } from "./SpeakerPage";
import type { SpeakerDetailView, SpeakerMeetingView, SpeakerRecord, SpeakerUpdate } from "../types";

function meeting(overrides: Partial<SpeakerMeetingView> = {}): SpeakerMeetingView {
  return {
    entry_id: "v-1",
    project: "GIS",
    meeting: "260903 - Photos and flows",
    labelled_segments: 132,
    hand_segments: 120,
    speech_sec: 640,
    voice_quality: "ok",
    segments: [
      { id: 12, start: 63.2, end: 70.1, text: " First thing said. " },
      { id: 15, start: 108, end: 111, text: "Second thing said." },
    ],
    segments_truncated: false,
    ...overrides,
  };
}

function detail(overrides: Partial<SpeakerDetailView> = {}): SpeakerDetailView {
  return {
    name: "Nikita",
    aliases: ["Никита"],
    bio: "Lead engineer",
    registered: true,
    projects: [
      { project: "GIS", meetings: 7, speech_sec: 3100, in_roster: true },
      { project: "ELS", meetings: 2, speech_sec: 59, in_roster: false },
    ],
    voice: { samples: 26, set_aside: 5, speech_sec: 12300 },
    meetings: [meeting()],
    ...overrides,
  };
}

function stored(overrides: Partial<SpeakerRecord> = {}): SpeakerRecord {
  return {
    name: "Nikita",
    aliases: ["Никита"],
    bio: "Lead engineer",
    registered: true,
    ...overrides,
  };
}

type Props = React.ComponentProps<typeof SpeakerPage>;

/** Plays App's role for the one thing the page hands back: the name it is
 * open under, which a rename changes. */
function Harness(props: Partial<Props>) {
  const [name, setName] = useState(props.name ?? "Nikita");
  return (
    <SpeakerPage
      onLoad={() => Promise.resolve(detail())}
      onSave={() => Promise.resolve(stored())}
      onDelete={() => Promise.resolve()}
      onBack={() => {}}
      onOpenRecording={() => {}}
      {...props}
      name={name}
      onRenamed={(renamed) => {
        setName(renamed);
        props.onRenamed?.(renamed);
      }}
    />
  );
}

async function renderPage(props: Partial<Props> = {}) {
  render(<Harness {...props} />);
  await screen.findByText("Speech you labelled");
}

describe("SpeakerPage", () => {
  it("shows who this is: name, database state, aliases and totals", async () => {
    await renderPage({
      onLoad: () => Promise.resolve(detail({ meetings: [meeting(), meeting({ meeting: "x" })] })),
    });

    expect(screen.getByRole("heading", { name: "Nikita" })).toBeInTheDocument();
    expect(screen.getByText("in the database")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove alias Никита" })).toBeInTheDocument();
    expect(screen.getByText(/Adding another speaker's name merges the two/)).toBeInTheDocument();
    const totals = screen.getByLabelText("Totals");
    expect(within(totals).getByText("2")).toBeInTheDocument();
    expect(within(totals).getByText("21m 20s")).toBeInTheDocument();
    expect(within(totals).getByText("26")).toBeInTheDocument();
  });

  it("marks a person who is only named in labels as not in the database yet", async () => {
    await renderPage({ onLoad: () => Promise.resolve(detail({ registered: false })) });

    expect(screen.getByText("not in the database yet")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove from database" })).not.toBeInTheDocument();
  });

  it("goes back to the Speakers table", async () => {
    const user = userEvent.setup();
    const onBack = vi.fn();
    await renderPage({ onBack });

    await user.click(screen.getByRole("button", { name: "← Speakers" }));

    expect(onBack).toHaveBeenCalledTimes(1);
  });

  it("lists each meeting's hand-labelled segments with their timecodes", async () => {
    await renderPage();

    const meetings = screen.getByRole("list", { name: "Meetings" });
    const block = within(meetings).getByRole("listitem");
    expect(within(block).getByText("GIS")).toBeInTheDocument();
    expect(within(block).getByText("1:03")).toBeInTheDocument();
    expect(within(block).getByText("First thing said.")).toBeInTheDocument();
    expect(within(block).getByText("1:48")).toBeInTheDocument();
    expect(
      within(block).getByText(
        "120 of 132 segments labelled by hand · 10m 40s · voice sample in use",
      ),
    ).toBeInTheDocument();
    expect(within(block).queryByText(/Showing the first/)).not.toBeInTheDocument();
  });

  it("opens the recording a meeting's title points at, by entry id", async () => {
    const user = userEvent.setup();
    const onOpenRecording = vi.fn();
    await renderPage({ onOpenRecording });

    await user.click(screen.getByRole("button", { name: /Photos and flows/ }));

    expect(onOpenRecording).toHaveBeenCalledWith("v-1");
  });

  it("shows a meeting that is not in the library as plain text, without a link", async () => {
    await renderPage({
      onLoad: () =>
        Promise.resolve(
          detail({ meetings: [meeting({ entry_id: null, segments_truncated: true })] }),
        ),
    });

    expect(screen.getByText(/Photos and flows/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Photos and flows/ })).not.toBeInTheDocument();
    // Nothing to open, so the note does not send the operator there.
    expect(screen.getByText("Showing the first 2 of 120.")).toBeInTheDocument();
  });

  it("says how many segments a truncated meeting shows", async () => {
    await renderPage({
      onLoad: () => Promise.resolve(detail({ meetings: [meeting({ segments_truncated: true })] })),
    });

    expect(
      screen.getByText("Showing the first 2 of 120. Open the recording for the rest."),
    ).toBeInTheDocument();
  });

  it("explains a voice sample that is set aside, and says when there is none", async () => {
    await renderPage({
      onLoad: () =>
        Promise.resolve(
          detail({
            meetings: [
              meeting({ meeting: "260901 - A", voice_quality: "conflict" }),
              meeting({ meeting: "260902 - B", voice_quality: "unconfirmed" }),
              meeting({ meeting: "260903 - C", voice_quality: "short" }),
              meeting({ meeting: "260904 - D", voice_quality: null }),
            ],
          }),
        ),
    });

    expect(
      screen.getByText("voice sample set aside: sounds like someone else"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("voice sample set aside: named automatically, not confirmed"),
    ).toBeInTheDocument();
    expect(screen.getByText("voice sample set aside: too little speech")).toBeInTheDocument();
    expect(screen.getByText(/· no voice sample$/)).toBeInTheDocument();
    expect(screen.queryByText(/voice sample in use/)).not.toBeInTheDocument();
  });

  it("lists the projects with meetings, speech and roster membership", async () => {
    await renderPage();

    const gis = screen.getByRole("row", { name: /GIS/ });
    expect(within(gis).getByText(/on the roster/)).toBeInTheDocument();
    expect(within(gis).getByText("7")).toBeInTheDocument();
    expect(within(gis).getByText("51m 40s")).toBeInTheDocument();
    const els = screen.getByRole("row", { name: /ELS/ });
    expect(within(els).queryByText(/on the roster/)).not.toBeInTheDocument();
  });

  it("shows the voice memory: samples in use and set aside", async () => {
    await renderPage();

    expect(screen.getByText("26 · 3h 25m")).toBeInTheDocument();
    expect(screen.getByText("Set aside").nextElementSibling).toHaveTextContent("5");
  });

  it("renames through the pencil and carries on under the new name", async () => {
    const user = userEvent.setup();
    const onSave = vi
      .fn<(name: string, update: SpeakerUpdate) => Promise<SpeakerRecord>>()
      .mockResolvedValue(stored({ name: "Nikita L", aliases: ["Никита", "Nikita"] }));
    const onLoad = vi.fn((name: string) =>
      Promise.resolve(name === "Nikita L" ? detail({ name: "Nikita L" }) : detail()),
    );
    const onRenamed = vi.fn();
    await renderPage({ onSave, onLoad, onRenamed });

    await user.click(screen.getByRole("button", { name: "Rename speaker" }));
    const input = screen.getByRole("textbox", { name: "Name" });
    await user.clear(input);
    await user.type(input, " Nikita L ");
    await user.click(screen.getByRole("button", { name: "Rename" }));

    await waitFor(() => expect(onSave).toHaveBeenCalledWith("Nikita", { newName: "Nikita L" }));
    expect(onRenamed).toHaveBeenCalledWith("Nikita L");
    expect(await screen.findByRole("heading", { name: "Nikita L" })).toBeInTheDocument();
    expect(onLoad).toHaveBeenLastCalledWith("Nikita L");
    expect(screen.queryByRole("textbox", { name: "Name" })).not.toBeInTheDocument();
  });

  it("saves nothing when the rename form is submitted unchanged", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn(() => Promise.resolve(stored()));
    await renderPage({ onSave });

    await user.click(screen.getByRole("button", { name: "Rename speaker" }));
    await user.click(screen.getByRole("button", { name: "Rename" }));

    expect(onSave).not.toHaveBeenCalled();
    expect(screen.getByRole("heading", { name: "Nikita" })).toBeInTheDocument();
  });

  it("adds an alias by saving the whole alias list, then reads the person again", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn(() => Promise.resolve(stored({ aliases: ["Никита", "Nik"] })));
    const onLoad = vi
      .fn<(name: string) => Promise<SpeakerDetailView>>()
      .mockResolvedValueOnce(detail())
      .mockResolvedValue(detail({ aliases: ["Никита", "Nik"] }));
    await renderPage({ onSave, onLoad });

    await user.click(screen.getByRole("button", { name: "+ Add a name" }));
    await user.type(
      screen.getByRole("textbox", { name: "Another name for this speaker" }),
      " Nik {Enter}",
    );

    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith("Nikita", { aliases: ["Никита", "Nik"] }),
    );
    expect(await screen.findByRole("button", { name: "Remove alias Nik" })).toBeInTheDocument();
    expect(onLoad).toHaveBeenCalledTimes(2);
  });

  it("removes an alias by saving the list without it", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn(() => Promise.resolve(stored({ aliases: [] })));
    await renderPage({ onSave });

    await user.click(screen.getByRole("button", { name: "Remove alias Никита" }));

    await waitFor(() => expect(onSave).toHaveBeenCalledWith("Nikita", { aliases: [] }));
  });

  it("saves the bio only on an explicit Save, and only when it changed", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn(() => Promise.resolve(stored({ bio: "Lead engineer, GIS" })));
    await renderPage({ onSave });
    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeDisabled();
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();

    await user.type(screen.getByRole("textbox", { name: "About" }), ", GIS");

    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
    expect(onSave).not.toHaveBeenCalled();

    await user.click(save);

    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith("Nikita", { bio: "Lead engineer, GIS" }),
    );
  });

  it("shows why a save was refused and keeps what was typed", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn(() =>
      Promise.reject({ kind: "invalid_argument", message: "that name belongs to another person" }),
    );
    await renderPage({ onSave });

    await user.click(screen.getByRole("button", { name: "Rename speaker" }));
    const input = screen.getByRole("textbox", { name: "Name" });
    await user.clear(input);
    await user.type(input, "Anna{Enter}");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "that name belongs to another person",
    );
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("Anna");
  });

  it("removes from the database only after a confirmation that labels stay", async () => {
    const user = userEvent.setup();
    const onDelete = vi.fn(() => Promise.resolve());
    await renderPage({ onDelete });

    await user.click(screen.getByRole("button", { name: "Remove from database" }));

    expect(onDelete).not.toHaveBeenCalled();
    expect(screen.getByText(/Labels in meetings are not touched/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Remove" }));

    await waitFor(() => expect(onDelete).toHaveBeenCalledWith("Nikita"));
  });

  it("backs out of the removal on Cancel", async () => {
    const user = userEvent.setup();
    const onDelete = vi.fn(() => Promise.resolve());
    await renderPage({ onDelete });

    await user.click(screen.getByRole("button", { name: "Remove from database" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onDelete).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Remove from database" })).toBeInTheDocument();
  });

  it("reports a person that cannot be read", async () => {
    render(
      <Harness
        onLoad={() => Promise.reject({ kind: "service", message: "no such person: Ghost" })}
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("no such person: Ghost");
    expect(screen.queryByText("Speech you labelled")).not.toBeInTheDocument();
  });
});
