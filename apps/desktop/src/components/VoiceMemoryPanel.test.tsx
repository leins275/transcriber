import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { VoiceMemoryPanel, setAsideReason, voiceDetail } from "./VoiceMemoryPanel";
import type { VoiceSampleView, VoiceStatusView, VoiceSummaryView } from "../types";

function sample(overrides: Partial<VoiceSampleView> = {}): VoiceSampleView {
  return {
    label: "Speaker 1",
    name: "Anna",
    speech_sec: 120,
    quality: "ok",
    conflicts_with: null,
    ...overrides,
  };
}

function voice(overrides: Partial<VoiceSummaryView> = {}): VoiceSummaryView {
  return {
    name: "Anna",
    samples: 2,
    here: 2,
    other_projects: [],
    speech_sec: 300,
    set_aside: 0,
    ...overrides,
  };
}

function status(overrides: Partial<VoiceStatusView> = {}): VoiceStatusView {
  return {
    project: "ACME",
    roster_only: false,
    updated_at: 1_790_000_000,
    rescanned: [],
    rescanned_elsewhere: 0,
    voices: [voice()],
    meetings: [
      { name: "260901 - Planning", state: "named", scanned_at: 1_790_000_000, voices: [sample()] },
    ],
    ...overrides,
  };
}

function renderPanel(
  loaded: VoiceStatusView | Promise<VoiceStatusView>,
  props: Partial<React.ComponentProps<typeof VoiceMemoryPanel>> = {},
) {
  const onLoad = vi.fn().mockReturnValue(Promise.resolve(loaded));
  const onConfirm = vi.fn().mockResolvedValue(undefined);
  const view = render(
    <VoiceMemoryPanel
      project="ACME"
      currentMeeting="260901 - Planning"
      onLoad={onLoad}
      onConfirm={onConfirm}
      reloadToken={0}
      {...props}
    />,
  );
  return { onLoad, onConfirm, ...view };
}

describe("voiceDetail", () => {
  it("says what recognition has to go on and where it came from", () => {
    expect(voiceDetail(voice())).toBe("2 samples · 5m 0s of speech");
    expect(voiceDetail(voice({ here: 1, other_projects: ["GIS", "MP"] }))).toBe(
      "2 samples · 5m 0s of speech · here and in GIS, MP",
    );
    expect(voiceDetail(voice({ here: 0, other_projects: ["GIS"], set_aside: 1 }))).toBe(
      "2 samples · 5m 0s of speech · known from GIS · 1 set aside",
    );
  });

  it("is plain about a person the memory cannot recognize", () => {
    expect(voiceDetail(voice({ samples: 0, here: 0, speech_sec: 0 }))).toBe("no voice sample yet");
    expect(voiceDetail(voice({ samples: 0, here: 0, speech_sec: 0, set_aside: 3 }))).toBe(
      "no usable sample — 3 samples set aside",
    );
  });
});

describe("setAsideReason", () => {
  it("gives each reason in the operator's words", () => {
    expect(setAsideReason(sample({ quality: "unconfirmed" }))).toBe(
      "named automatically, not confirmed",
    );
    expect(setAsideReason(sample({ quality: "partial" }))).toBe("named on only part of this voice");
    expect(setAsideReason(sample({ quality: "short", speech_sec: 4 }))).toBe(
      "too little speech (4s)",
    );
    expect(setAsideReason(sample({ quality: "conflict", conflicts_with: "Boris" }))).toBe(
      "sounds like Boris",
    );
  });
});

describe("VoiceMemoryPanel", () => {
  it("reads the project's memory and lists who is known", async () => {
    const { onLoad } = renderPanel(
      status({ voices: [voice(), voice({ name: "Boris", samples: 0, here: 0, speech_sec: 0 })] }),
    );

    const voices = await screen.findByRole("list", { name: "Known voices" });
    expect(onLoad).toHaveBeenCalledWith("ACME");
    expect(within(voices).getByText("Anna")).toBeInTheDocument();
    expect(within(voices).getByText("2 samples · 5m 0s of speech")).toBeInTheDocument();
    expect(within(voices).getByText("no voice sample yet")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(/^Up to date · last change read /);
  });

  it("shows which meetings the read just picked up", async () => {
    renderPanel(status({ rescanned: ["260901 - Planning"], rescanned_elsewhere: 2 }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Just re-read 1 changed meeting · 2 meetings re-read in other projects",
    );
    const meetings = screen.getByRole("list", { name: "Meetings", hidden: true });
    expect(within(meetings).getByText("re-read just now")).toBeInTheDocument();
    expect(within(meetings).getByText("this meeting")).toBeInTheDocument();
  });

  it("says why a meeting contributes nothing", async () => {
    renderPanel(
      status({
        meetings: [
          {
            name: "260903 - Mixed",
            state: "named",
            scanned_at: 1,
            voices: [
              sample(),
              sample({ name: "Dmitry", quality: "conflict", conflicts_with: "Boris" }),
            ],
          },
          { name: "260902 - Unnamed", state: "unnamed", scanned_at: 1, voices: [] },
          { name: "260901 - Raw", state: "no_voices", scanned_at: 1, voices: [] },
          { name: "260831 - Shell", state: "no_transcript", scanned_at: null, voices: [] },
        ],
      }),
    );

    const meetings = await screen.findByRole("list", { name: "Meetings", hidden: true });
    expect(within(meetings).getByText("Anna, Dmitry (sounds like Boris)")).toBeInTheDocument();
    expect(within(meetings).getByText("voices found, nobody named yet")).toBeInTheDocument();
    expect(within(meetings).getByText("no voice data — run Identify speakers")).toBeInTheDocument();
    expect(within(meetings).getByText("no transcript")).toBeInTheDocument();
  });

  it("offers to confirm machine-given names of the open meeting, then reads again", async () => {
    const user = userEvent.setup();
    const before = status({
      meetings: [
        {
          name: "260901 - Planning",
          state: "named",
          scanned_at: 1,
          voices: [sample({ quality: "unconfirmed" })],
        },
      ],
    });
    const onLoad = vi
      .fn()
      .mockResolvedValueOnce(before)
      .mockResolvedValueOnce(status({ rescanned: ["260901 - Planning"] }));
    const onConfirm = vi.fn().mockResolvedValue(undefined);
    render(
      <VoiceMemoryPanel
        project="ACME"
        currentMeeting="260901 - Planning"
        onLoad={onLoad}
        onConfirm={onConfirm}
        reloadToken={0}
      />,
    );

    await user.click(await screen.findByRole("button", { name: "Confirm these names" }));

    expect(onConfirm).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(onLoad).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Confirm these names" })).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("status")).toHaveTextContent("Just re-read 1 changed meeting");
  });

  it("does not offer confirmation for another meeting's machine-given names", async () => {
    renderPanel(
      status({
        meetings: [
          {
            name: "260830 - Elsewhere",
            state: "named",
            scanned_at: 1,
            voices: [sample({ quality: "unconfirmed" })],
          },
        ],
      }),
    );

    await screen.findByRole("list", { name: "Known voices" });
    expect(screen.queryByRole("button", { name: "Confirm these names" })).not.toBeInTheDocument();
  });

  it("reads again when the reload token moves", async () => {
    const { onLoad, rerender } = renderPanel(status());
    await screen.findByRole("list", { name: "Known voices" });

    rerender(
      <VoiceMemoryPanel
        project="ACME"
        currentMeeting="260901 - Planning"
        onLoad={onLoad}
        onConfirm={vi.fn()}
        reloadToken={1}
      />,
    );

    await waitFor(() => expect(onLoad).toHaveBeenCalledTimes(2));
  });

  it("explains an empty memory and a strict roster", async () => {
    renderPanel(status({ voices: [], meetings: [], roster_only: true, updated_at: null }));

    expect(await screen.findByText(/Nobody is known yet/)).toBeInTheDocument();
    expect(screen.getByText(/roster is strict/)).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Up to date");
  });

  it("shows a failed read instead of a stale picture", async () => {
    renderPanel(Promise.reject({ kind: "service", message: "service is not running" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("service is not running");
    expect(screen.getByRole("status")).toHaveTextContent("Unavailable");
  });
});
