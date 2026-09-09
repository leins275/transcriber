import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ProjectRosterView } from "../types";

// The api module is the designed test seam for state hooks (the same choice
// `useChat.test.ts` documents): it is the single IPC boundary, and everything
// the hook does on this side of it is pure state.
vi.mock("../api", () => ({
  api: {
    readProjectRoster: vi.fn(),
    saveProjectRoster: vi.fn(),
  },
}));

import { api } from "../api";
import { useProjectRoster } from "./useProjectRoster";

const readProjectRoster = vi.mocked(api.readProjectRoster);
const saveProjectRoster = vi.mocked(api.saveProjectRoster);

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
};

function deferred<T>(): Deferred<T> {
  let resolve: (value: T) => void = () => {};
  let reject: (reason: unknown) => void = () => {};
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function renderForProject(project: string | null) {
  return renderHook(({ project }) => useProjectRoster(project), {
    initialProps: { project },
  });
}

describe("useProjectRoster", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    readProjectRoster.mockResolvedValue({ mode: "open", names: [] });
    saveProjectRoster.mockResolvedValue({ mode: "open", names: [] });
  });

  it("exposes the roster the open recording's project carries", async () => {
    readProjectRoster.mockResolvedValue({ mode: "roster", names: ["Anna", "Maxim"] });

    const { result } = renderForProject("ACME");

    await waitFor(() =>
      expect(result.current.roster).toEqual({ mode: "roster", names: ["Anna", "Maxim"] }),
    );
    expect(readProjectRoster).toHaveBeenCalledTimes(1);
    expect(readProjectRoster).toHaveBeenCalledWith("ACME");
  });

  it("a recording outside any project is open to any name without a vault read", async () => {
    const { result } = renderForProject(null);

    await act(async () => {});

    expect(readProjectRoster).not.toHaveBeenCalled();
    expect(result.current.roster).toEqual({ mode: "open", names: [] });
  });

  it("a roster that cannot be read leaves the project open to any name", async () => {
    const read = deferred<ProjectRosterView>();
    readProjectRoster.mockReturnValue(read.promise);
    const { result } = renderForProject("ACME");

    await act(async () => {
      read.reject({ kind: "internal", message: "roster unreadable" });
    });

    expect(result.current.roster).toEqual({ mode: "open", names: [] });
  });

  it("moving to another project shows that project's roster", async () => {
    const rosters: Record<string, ProjectRosterView> = {
      ACME: { mode: "roster", names: ["Anna"] },
      GLOBEX: { mode: "roster", names: ["Maxim"] },
    };
    readProjectRoster.mockImplementation(async (project: string) => rosters[project]);
    const { result, rerender } = renderForProject("ACME");
    await waitFor(() => expect(result.current.roster).toEqual(rosters.ACME));

    rerender({ project: "GLOBEX" });

    await waitFor(() => expect(result.current.roster).toEqual(rosters.GLOBEX));
  });

  it("a late answer for the project just left never overwrites the current roster", async () => {
    const reads: Record<string, Deferred<ProjectRosterView>> = {
      ACME: deferred<ProjectRosterView>(),
      GLOBEX: deferred<ProjectRosterView>(),
    };
    readProjectRoster.mockImplementation((project: string) => reads[project].promise);
    const { result, rerender } = renderForProject("ACME");
    rerender({ project: "GLOBEX" });

    await act(async () => {
      reads.GLOBEX.resolve({ mode: "roster", names: ["Maxim"] });
      reads.ACME.resolve({ mode: "roster", names: ["Anna"] });
    });

    expect(result.current.roster).toEqual({ mode: "roster", names: ["Maxim"] });
  });

  it("saving a roster adopts the normalized list the vault stored", async () => {
    saveProjectRoster.mockResolvedValue({ mode: "roster", names: ["Anna", "Maxim"] });
    const { result } = renderForProject("ACME");
    await waitFor(() => expect(readProjectRoster).toHaveBeenCalledWith("ACME"));

    await act(async () => {
      await result.current.save({ mode: "roster", names: ["  Anna ", "anna", "", "Maxim"] });
    });

    expect(saveProjectRoster).toHaveBeenCalledWith("ACME", {
      mode: "roster",
      names: ["  Anna ", "anna", "", "Maxim"],
    });
    expect(result.current.roster).toEqual({ mode: "roster", names: ["Anna", "Maxim"] });
  });

  it("a refused save reaches the editor and leaves the roster as it was", async () => {
    readProjectRoster.mockResolvedValue({ mode: "roster", names: ["Anna"] });
    saveProjectRoster.mockRejectedValue({ kind: "invalid_argument", message: "boom" });
    const { result } = renderForProject("ACME");
    await waitFor(() => expect(result.current.roster).toEqual({ mode: "roster", names: ["Anna"] }));

    let refusal: unknown;
    await act(async () => {
      refusal = await result.current
        .save({ mode: "roster", names: ["Anna", "Olga"] })
        .catch((error: unknown) => error);
    });

    expect(refusal).toEqual({ kind: "invalid_argument", message: "boom" });
    expect(result.current.roster).toEqual({ mode: "roster", names: ["Anna"] });
  });
});
