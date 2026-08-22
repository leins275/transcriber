import styles from "./VaultPanel.module.css";
import { VaultList } from "./VaultList";
import type { VaultMeetingView } from "../types";

export type VaultPanelProps = {
  entries: VaultMeetingView[];
  onReveal: (entryId: string) => void;
};

/**
 * The vault browser: a persistent list of meetings already ingested into
 * the configured vault, beneath the session's Jobs ledger and in the same
 * ruled-row Ledger grammar (JobsPanel/JobList/JobRow) -- a heading with the
 * running count, and the ruled row list underneath. Renders nothing when
 * the vault is empty: the hero drop zone already owns that empty state
 * (App.tsx), so this section simply does not mount rather than duplicating
 * it. Presentational only: no invoke, no listen, no fetch -- App.tsx owns
 * fetching (on startup and after ingest) via `api.listVault`.
 */
export function VaultPanel({ entries, onReveal }: VaultPanelProps) {
  if (entries.length === 0) return null;

  return (
    <section className={styles.panel} aria-label="Vault" role="region">
      <div className={styles.heading}>
        <h2 className={styles.title}>Vault</h2>
        <span className={styles.count}>{entries.length} in vault</span>
      </div>
      <div className={styles.list}>
        <VaultList entries={entries} onReveal={onReveal} />
      </div>
    </section>
  );
}
