//! SQLite job ledger (FR-5, NFR-7, NFR-8).
//!
//! Port of `services/transcription/src/transcription/ledger.py`, against the
//! same database file: `<app_dir>/data/jobs.sqlite3` already holds a user's
//! job history, so the schema, the `user_version` gate and the migration list
//! are reproduced rather than redesigned.
//!
//! One row per job, inserted at submission time and only ever `UPDATE`d --
//! never re-inserted, never deleted. WAL mode lets the engine worker write
//! while status polling reads.

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use chrono::{SecondsFormat, Utc};
use rusqlite::{params, Connection, OptionalExtension, Row};

/// The schema this build writes and is willing to read.
pub const SCHEMA_VERSION: i64 = 2;

const DDL: &str = "
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    status TEXT NOT NULL,
    job_type TEXT NOT NULL DEFAULT 'transcribe',
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    device TEXT NOT NULL,
    source_path TEXT NOT NULL,
    output_path TEXT NOT NULL,
    audio_duration_sec REAL,
    elapsed_sec REAL,
    realtime_factor REAL,
    cost_usd REAL,
    currency TEXT,
    language TEXT,
    segment_count INTEGER,
    filtered_segment_count INTEGER,
    error_kind TEXT,
    error_message TEXT,
    result_json TEXT,
    meeting_json TEXT,
    service_version TEXT NOT NULL
)
";

/// Columns added by each schema version bump, applied in order to a database
/// created by an older build. `ALTER TABLE ... ADD COLUMN` is the entire
/// migration story on purpose: rows are never rewritten, so a v1 row keeps its
/// meaning (`job_type` backfills to 'transcribe' via the column default).
const MIGRATIONS: &[(i64, &[&str])] = &[(
    // v1 -> v2: job-type discriminator + the result manifest for
    // non-transcribe jobs, which write artifacts rather than transcript.json.
    2,
    &[
        "ALTER TABLE jobs ADD COLUMN job_type TEXT NOT NULL DEFAULT 'transcribe'",
        "ALTER TABLE jobs ADD COLUMN result_json TEXT",
    ],
)];

/// Why the ledger could not be opened or written.
#[derive(Debug, thiserror::Error)]
pub enum LedgerError {
    /// The database was written by a newer build. Refusing is the point: a
    /// column this build does not know about must not be silently dropped
    /// from the user's history.
    #[error("{path}: ledger schema version {found} is newer than this build supports (max {max})")]
    SchemaTooNew { path: PathBuf, found: i64, max: i64 },
    #[error("unable to open ledger database {path}: {source}")]
    Open {
        path: PathBuf,
        #[source]
        source: rusqlite::Error,
    },
    #[error(transparent)]
    Sqlite(#[from] rusqlite::Error),
    #[error("unable to create ledger directory {path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
}

/// One row of the ledger, as the UI and the service seam read it.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct LedgerRow {
    pub job_id: String,
    pub status: String,
    pub job_type: String,
    pub created_at: Option<String>,
    pub started_at: Option<String>,
    pub finished_at: Option<String>,
    pub provider: Option<String>,
    pub model: Option<String>,
    pub device: Option<String>,
    pub source_path: Option<String>,
    pub output_path: Option<String>,
    pub audio_duration_sec: Option<f64>,
    pub elapsed_sec: Option<f64>,
    pub realtime_factor: Option<f64>,
    pub language: Option<String>,
    pub segment_count: Option<i64>,
    pub error_kind: Option<String>,
    pub error_message: Option<String>,
    pub result_json: Option<String>,
    pub service_version: Option<String>,
}

/// What a job records when it is first accepted.
#[derive(Debug, Clone)]
pub struct NewJob<'a> {
    pub job_id: &'a str,
    pub job_type: &'a str,
    pub provider: &'a str,
    pub model: &'a str,
    pub device: &'a str,
    pub source_path: &'a str,
    pub output_path: &'a str,
    pub language: Option<&'a str>,
    pub meeting_json: Option<&'a str>,
}

/// What a successful job records when it ends.
#[derive(Debug, Clone, Default)]
pub struct Success<'a> {
    pub elapsed_sec: f64,
    /// Left `None` by the LLM job types, which describe their output in
    /// `result_json` instead.
    pub audio_duration_sec: Option<f64>,
    pub segment_count: Option<i64>,
    pub filtered_segment_count: i64,
    pub result_json: Option<&'a str>,
}

/// The sqlite-backed job ledger. One instance per open connection.
#[derive(Debug)]
pub struct Ledger {
    db_path: PathBuf,
    conn: Mutex<Connection>,
    service_version: String,
}

fn now() -> String {
    // Matches Python's `datetime.now(UTC).isoformat()`: microsecond precision
    // and a `+00:00` offset rather than `Z`, so old and new rows sort and read
    // alike.
    Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false)
}

impl Ledger {
    /// Open (creating if needed) the ledger at `db_path`, migrating an older
    /// schema forward.
    pub fn open(db_path: impl AsRef<Path>) -> Result<Self, LedgerError> {
        Self::open_with_version(db_path, env!("CARGO_PKG_VERSION"))
    }

    /// Same as [`Ledger::open`], with the `service_version` stamped on new
    /// rows made explicit (tests pin it; the app never passes it).
    pub fn open_with_version(
        db_path: impl AsRef<Path>,
        service_version: &str,
    ) -> Result<Self, LedgerError> {
        let db_path = db_path.as_ref().to_path_buf();
        if let Some(parent) = db_path.parent() {
            // A fresh app folder has no `data/` yet, and sqlite will not
            // create it (FR-10).
            std::fs::create_dir_all(parent).map_err(|source| LedgerError::Io {
                path: parent.to_path_buf(),
                source,
            })?;
        }

        let conn = Connection::open(&db_path).map_err(|source| LedgerError::Open {
            path: db_path.clone(),
            source,
        })?;
        conn.pragma_update(None, "journal_mode", "WAL")?;

        let ledger = Ledger {
            db_path,
            conn: Mutex::new(conn),
            service_version: service_version.to_string(),
        };
        ledger.init_schema()?;
        Ok(ledger)
    }

    fn init_schema(&self) -> Result<(), LedgerError> {
        let conn = self.lock();
        let current: i64 = conn.query_row("PRAGMA user_version", [], |row| row.get(0))?;
        if current > SCHEMA_VERSION {
            return Err(LedgerError::SchemaTooNew {
                path: self.db_path.clone(),
                found: current,
                max: SCHEMA_VERSION,
            });
        }

        // Migrate an existing older database *before* the CREATE TABLE IF NOT
        // EXISTS below (a no-op once the table exists) so the DDL and the
        // migrated table agree on the column set. `user_version` 0 means a
        // fresh database with no table yet -- nothing to migrate.
        if current > 0 {
            for (version, statements) in MIGRATIONS {
                if *version > current && *version <= SCHEMA_VERSION {
                    for statement in *statements {
                        conn.execute(statement, [])?;
                    }
                }
            }
        }

        conn.execute(DDL, [])?;
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC)",
            [],
        )?;
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)",
            [],
        )?;
        if current < SCHEMA_VERSION {
            conn.pragma_update(None, "user_version", SCHEMA_VERSION)?;
        }
        Ok(())
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Connection> {
        // A poisoned lock means a previous caller panicked mid-write. The
        // connection itself is still usable and the alternative -- refusing
        // every later write -- would lose the rest of the session's history.
        self.conn.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Insert the one row for this job, in `queued` status.
    pub fn insert_job(&self, job: &NewJob<'_>) -> Result<(), LedgerError> {
        self.lock().execute(
            "INSERT INTO jobs (
                 job_id, created_at, status, job_type, provider, model, device,
                 source_path, output_path, language, meeting_json, service_version
             ) VALUES (?1, ?2, 'queued', ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
            params![
                job.job_id,
                now(),
                job.job_type,
                job.provider,
                job.model,
                job.device,
                job.source_path,
                job.output_path,
                job.language,
                job.meeting_json,
                self.service_version,
            ],
        )?;
        Ok(())
    }

    /// Flip a row to `running`, optionally recording the device the job
    /// actually resolved to -- which is not known at insert time, because
    /// loading the engine is deferred until the job starts.
    pub fn mark_running(&self, job_id: &str, device: Option<&str>) -> Result<(), LedgerError> {
        let conn = self.lock();
        match device {
            Some(device) => conn.execute(
                "UPDATE jobs SET status='running', started_at=?1, device=?2 WHERE job_id=?3",
                params![now(), device, job_id],
            )?,
            None => conn.execute(
                "UPDATE jobs SET status='running', started_at=?1 WHERE job_id=?2",
                params![now(), job_id],
            )?,
        };
        Ok(())
    }

    /// Flip a row to `succeeded`.
    pub fn finish_succeeded(&self, job_id: &str, outcome: &Success<'_>) -> Result<(), LedgerError> {
        // Guarded against a zero duration as well as a missing one: dividing
        // by a zero-length recording would store an infinity.
        let realtime_factor = match outcome.audio_duration_sec {
            Some(duration) if duration != 0.0 => Some(outcome.elapsed_sec / duration),
            _ => None,
        };
        self.lock().execute(
            "UPDATE jobs SET
                 status='succeeded',
                 finished_at=?1,
                 elapsed_sec=?2,
                 audio_duration_sec=?3,
                 realtime_factor=?4,
                 segment_count=?5,
                 filtered_segment_count=?6,
                 cost_usd=NULL,
                 currency=NULL,
                 result_json=?7
             WHERE job_id=?8",
            params![
                now(),
                outcome.elapsed_sec,
                outcome.audio_duration_sec,
                realtime_factor,
                outcome.segment_count,
                outcome.filtered_segment_count,
                outcome.result_json,
                job_id,
            ],
        )?;
        Ok(())
    }

    /// Flip a row to `failed`, attributing the failure.
    pub fn finish_failed(
        &self,
        job_id: &str,
        elapsed_sec: Option<f64>,
        kind: wire::ErrorKind,
        message: &str,
    ) -> Result<(), LedgerError> {
        self.lock().execute(
            "UPDATE jobs SET
                 status='failed', finished_at=?1, elapsed_sec=?2, error_kind=?3, error_message=?4
             WHERE job_id=?5",
            params![now(), elapsed_sec, kind.as_str(), message, job_id],
        )?;
        Ok(())
    }

    /// Flip a row to `cancelled` -- a terminal state distinct from `failed`,
    /// because a user stopping a job is not the same as a job breaking.
    pub fn finish_cancelled(
        &self,
        job_id: &str,
        elapsed_sec: Option<f64>,
    ) -> Result<(), LedgerError> {
        self.lock().execute(
            "UPDATE jobs SET
                 status='cancelled', finished_at=?1, elapsed_sec=?2, error_kind='cancelled'
             WHERE job_id=?3",
            params![now(), elapsed_sec, job_id],
        )?;
        Ok(())
    }

    /// Flip rows left `queued`/`running` by a previous run to `failed`
    /// (NFR-7), returning how many were reconciled.
    ///
    /// In-process engines make this matter more than it did with a sidecar: a
    /// native crash inside whisper.cpp or llama.cpp takes the whole app down,
    /// and this is what stops the next launch from showing a job as eternally
    /// running.
    pub fn reconcile_interrupted(&self) -> Result<usize, LedgerError> {
        let changed = self.lock().execute(
            "UPDATE jobs SET
                 status='failed',
                 finished_at=?1,
                 error_kind='internal',
                 error_message='job was interrupted by a service restart'
             WHERE status IN ('queued', 'running')",
            params![now()],
        )?;
        Ok(changed)
    }

    /// One row by id.
    pub fn get_job(&self, job_id: &str) -> Result<Option<LedgerRow>, LedgerError> {
        let conn = self.lock();
        let row = conn
            .query_row("SELECT * FROM jobs WHERE job_id=?1", [job_id], |row| {
                read_row(row)
            })
            .optional()?;
        Ok(row)
    }

    /// Rows newest first, optionally capped and filtered by status.
    pub fn list_jobs(
        &self,
        limit: Option<u32>,
        status: Option<&str>,
    ) -> Result<Vec<LedgerRow>, LedgerError> {
        let mut query = String::from("SELECT * FROM jobs");
        if status.is_some() {
            query.push_str(" WHERE status = ?1");
        }
        // `rowid` breaks ties: two jobs submitted inside the same microsecond
        // would otherwise come back in an arbitrary order.
        query.push_str(" ORDER BY created_at DESC, rowid DESC");
        if limit.is_some() {
            query.push_str(if status.is_some() {
                " LIMIT ?2"
            } else {
                " LIMIT ?1"
            });
        }

        let conn = self.lock();
        let mut stmt = conn.prepare(&query)?;
        let rows = match (status, limit) {
            (Some(status), Some(limit)) => stmt.query_map(params![status, limit], read_row)?,
            (Some(status), None) => stmt.query_map(params![status], read_row)?,
            (None, Some(limit)) => stmt.query_map(params![limit], read_row)?,
            (None, None) => stmt.query_map([], read_row)?,
        }
        .collect::<Result<Vec<_>, _>>()?;
        Ok(rows)
    }
}

fn read_row(row: &Row<'_>) -> rusqlite::Result<LedgerRow> {
    Ok(LedgerRow {
        job_id: row.get("job_id")?,
        status: row.get("status")?,
        job_type: row.get("job_type")?,
        created_at: row.get("created_at")?,
        started_at: row.get("started_at")?,
        finished_at: row.get("finished_at")?,
        provider: row.get("provider")?,
        model: row.get("model")?,
        device: row.get("device")?,
        source_path: row.get("source_path")?,
        output_path: row.get("output_path")?,
        audio_duration_sec: row.get("audio_duration_sec")?,
        elapsed_sec: row.get("elapsed_sec")?,
        realtime_factor: row.get("realtime_factor")?,
        language: row.get("language")?,
        segment_count: row.get("segment_count")?,
        error_kind: row.get("error_kind")?,
        error_message: row.get("error_message")?,
        result_json: row.get("result_json")?,
        service_version: row.get("service_version")?,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ledger() -> (tempfile::TempDir, Ledger) {
        let dir = tempfile::tempdir().unwrap();
        let ledger = Ledger::open_with_version(dir.path().join("data/jobs.sqlite3"), "0.9.0")
            .expect("open ledger");
        (dir, ledger)
    }

    fn new_job<'a>(job_id: &'a str) -> NewJob<'a> {
        NewJob {
            job_id,
            job_type: "transcribe",
            provider: "local",
            model: "large-v3",
            device: "auto",
            source_path: "C:\\vault\\ELS\\260812 - Demo\\source.mp4",
            output_path: "C:\\vault\\ELS\\260812 - Demo",
            language: Some("ru"),
            meeting_json: None,
        }
    }

    #[test]
    fn opening_creates_the_missing_data_directory() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("data").join("jobs.sqlite3");
        Ledger::open(&path).expect("open");
        assert!(path.is_file());
    }

    #[test]
    fn a_job_moves_queued_to_running_to_succeeded() {
        let (_dir, ledger) = ledger();
        ledger.insert_job(&new_job("j1")).unwrap();
        assert_eq!(ledger.get_job("j1").unwrap().unwrap().status, "queued");

        ledger.mark_running("j1", Some("cuda")).unwrap();
        let row = ledger.get_job("j1").unwrap().unwrap();
        assert_eq!(row.status, "running");
        assert_eq!(row.device.as_deref(), Some("cuda"));
        assert!(row.started_at.is_some());

        ledger
            .finish_succeeded(
                "j1",
                &Success {
                    elapsed_sec: 3.0,
                    audio_duration_sec: Some(12.0),
                    segment_count: Some(4),
                    ..Default::default()
                },
            )
            .unwrap();
        let row = ledger.get_job("j1").unwrap().unwrap();
        assert_eq!(row.status, "succeeded");
        assert_eq!(row.realtime_factor, Some(0.25));
        assert_eq!(row.segment_count, Some(4));
        assert!(row.finished_at.is_some());
    }

    #[test]
    fn a_zero_length_recording_stores_no_realtime_factor() {
        // Not a hypothetical: dividing by it would store an infinity, which
        // sqlite keeps and the UI would render.
        let (_dir, ledger) = ledger();
        ledger.insert_job(&new_job("j1")).unwrap();
        ledger
            .finish_succeeded(
                "j1",
                &Success {
                    elapsed_sec: 3.0,
                    audio_duration_sec: Some(0.0),
                    ..Default::default()
                },
            )
            .unwrap();
        assert_eq!(ledger.get_job("j1").unwrap().unwrap().realtime_factor, None);
    }

    #[test]
    fn failure_records_the_attributed_kind_and_message() {
        let (_dir, ledger) = ledger();
        ledger.insert_job(&new_job("j1")).unwrap();
        ledger
            .finish_failed("j1", Some(1.5), wire::ErrorKind::AudioDecode, "bad file")
            .unwrap();

        let row = ledger.get_job("j1").unwrap().unwrap();
        assert_eq!(row.status, "failed");
        assert_eq!(row.error_kind.as_deref(), Some("audio_decode"));
        assert_eq!(row.error_message.as_deref(), Some("bad file"));
    }

    #[test]
    fn cancellation_is_its_own_terminal_state() {
        let (_dir, ledger) = ledger();
        ledger.insert_job(&new_job("j1")).unwrap();
        ledger.finish_cancelled("j1", Some(0.5)).unwrap();

        let row = ledger.get_job("j1").unwrap().unwrap();
        assert_eq!(row.status, "cancelled");
        assert_eq!(row.error_kind.as_deref(), Some("cancelled"));
    }

    #[test]
    fn reconcile_fails_only_the_jobs_a_crash_left_open() {
        let (_dir, ledger) = ledger();
        ledger.insert_job(&new_job("queued")).unwrap();
        ledger.insert_job(&new_job("running")).unwrap();
        ledger.mark_running("running", None).unwrap();
        ledger.insert_job(&new_job("done")).unwrap();
        ledger
            .finish_succeeded(
                "done",
                &Success {
                    elapsed_sec: 1.0,
                    ..Default::default()
                },
            )
            .unwrap();

        assert_eq!(ledger.reconcile_interrupted().unwrap(), 2);
        assert_eq!(ledger.get_job("queued").unwrap().unwrap().status, "failed");
        assert_eq!(
            ledger
                .get_job("running")
                .unwrap()
                .unwrap()
                .error_message
                .as_deref(),
            Some("job was interrupted by a service restart")
        );
        assert_eq!(ledger.get_job("done").unwrap().unwrap().status, "succeeded");
    }

    #[test]
    fn listing_is_newest_first_and_filterable() {
        let (_dir, ledger) = ledger();
        for id in ["a", "b", "c"] {
            ledger.insert_job(&new_job(id)).unwrap();
        }
        ledger.finish_cancelled("b", None).unwrap();

        let all = ledger.list_jobs(None, None).unwrap();
        assert_eq!(all.len(), 3);
        // Same-microsecond inserts tie on created_at; rowid decides, newest
        // first.
        assert_eq!(all[0].job_id, "c");

        let cancelled = ledger.list_jobs(None, Some("cancelled")).unwrap();
        assert_eq!(cancelled.len(), 1);
        assert_eq!(cancelled[0].job_id, "b");

        assert_eq!(ledger.list_jobs(Some(2), None).unwrap().len(), 2);
        assert_eq!(
            ledger.list_jobs(Some(1), Some("queued")).unwrap().len(),
            1,
            "limit and status must compose"
        );
    }

    #[test]
    fn a_v1_database_migrates_forward_keeping_its_rows() {
        // The real upgrade path: a ledger written before the LLM job types
        // existed has neither `job_type` nor `result_json`.
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("jobs.sqlite3");
        {
            let conn = Connection::open(&path).unwrap();
            conn.execute(
                "CREATE TABLE jobs (
                     job_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, started_at TEXT,
                     finished_at TEXT, status TEXT NOT NULL, provider TEXT NOT NULL,
                     model TEXT NOT NULL, device TEXT NOT NULL, source_path TEXT NOT NULL,
                     output_path TEXT NOT NULL, audio_duration_sec REAL, elapsed_sec REAL,
                     realtime_factor REAL, cost_usd REAL, currency TEXT, language TEXT,
                     segment_count INTEGER, filtered_segment_count INTEGER, error_kind TEXT,
                     error_message TEXT, meeting_json TEXT, service_version TEXT NOT NULL
                 )",
                [],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO jobs (job_id, created_at, status, provider, model, device,
                                   source_path, output_path, service_version)
                 VALUES ('old', '2026-01-01T00:00:00+00:00', 'succeeded', 'local', 'large-v3',
                         'cpu', 'a.mp4', 'out', '0.5.0')",
                [],
            )
            .unwrap();
            conn.pragma_update(None, "user_version", 1).unwrap();
        }

        let ledger = Ledger::open(&path).expect("migrate v1 forward");
        let row = ledger
            .get_job("old")
            .unwrap()
            .expect("the old row survives");
        assert_eq!(row.status, "succeeded");
        // Backfilled by the column default rather than by rewriting rows.
        assert_eq!(row.job_type, "transcribe");
        assert_eq!(row.result_json, None);

        let conn = ledger.lock();
        let version: i64 = conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(version, SCHEMA_VERSION);
    }

    #[test]
    fn a_database_from_a_newer_build_is_refused_not_downgraded() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("jobs.sqlite3");
        {
            let conn = Connection::open(&path).unwrap();
            conn.pragma_update(None, "user_version", SCHEMA_VERSION + 1)
                .unwrap();
        }
        assert!(matches!(
            Ledger::open(&path),
            Err(LedgerError::SchemaTooNew { .. })
        ));
    }

    #[test]
    fn reopening_an_existing_ledger_keeps_its_history() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("jobs.sqlite3");
        {
            let ledger = Ledger::open(&path).unwrap();
            ledger.insert_job(&new_job("j1")).unwrap();
        }
        let ledger = Ledger::open(&path).unwrap();
        assert!(ledger.get_job("j1").unwrap().is_some());
    }
}
