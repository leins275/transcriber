import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { EMPTY_ROSTER } from "../lib/roster";
import type { AppError, ProjectRosterView } from "../types";

/**
 * The speaker roster of the project a recording belongs to.
 *
 * The roster governs what the transcript's speaker name controls offer, so it
 * follows the open recording: a new project re-reads `<PROJECT>/roster.json`,
 * and a recording outside any project (`unsorted`) needs no read at all -- it
 * is open to any name by definition.
 *
 * Reading is best-effort. A project with no roster file, an unreadable one, or
 * a backend that refuses the call all leave naming unrestricted rather than
 * blocking the page: `EMPTY_ROSTER` is both the "no file" answer and the
 * failure fallback.
 *
 * Saving is not best-effort -- the editor must be able to tell the operator
 * that a name was refused -- so `save` rejects with the `AppError` the command
 * produced and leaves the roster on screen as it was. On success the hook
 * adopts the *returned* view: the command normalizes (trims, drops blanks,
 * collapses case-insensitive duplicates), and that stored list, not the draft,
 * is what the project now carries.
 */
export function useProjectRoster(project: string | null): {
  roster: ProjectRosterView;
  save: (roster: ProjectRosterView) => Promise<void>;
} {
  const [roster, setRoster] = useState<ProjectRosterView>(EMPTY_ROSTER);
  // The project a settled request must still match to be worth storing.
  const currentProject = useRef(project);

  useEffect(() => {
    currentProject.current = project;
    // Until this project's own roster lands, no other project's roster may
    // restrict its names.
    setRoster(EMPTY_ROSTER);
    if (project === null) return;
    let cancelled = false;
    api
      .readProjectRoster(project)
      .then((view) => {
        if (!cancelled) setRoster(view ?? EMPTY_ROSTER);
      })
      .catch(() => {
        if (!cancelled) setRoster(EMPTY_ROSTER);
      });
    return () => {
      // A late answer belongs to the project we just left.
      cancelled = true;
    };
  }, [project]);

  const save = useCallback(
    async (draft: ProjectRosterView): Promise<void> => {
      if (project === null) {
        const error: AppError = {
          kind: "invalid_argument",
          message: "This recording is not in a project, so it has no speaker roster.",
        };
        throw error;
      }
      const stored = await api.saveProjectRoster(project, draft);
      if (currentProject.current === project) setRoster(stored ?? EMPTY_ROSTER);
    },
    [project],
  );

  return { roster, save };
}
