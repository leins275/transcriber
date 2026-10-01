//! The speakers database: `list_speakers`, `speaker_detail`, `save_speaker`
//! and `delete_speaker`.
//!
//! The registry (`<vault root>/people.json`) and everything derived from
//! the meetings' labels belong to the service; this module is a proxy over
//! its `/v1/people` routes. A person has no id -- any of their names
//! addresses them -- so the commands take names and the service resolves
//! them.
//!
//! The one thing added here is the id-not-path rule: the service names a
//! person's meetings by vault-root-relative directory, and the detail view
//! swaps each for the opaque entry id `list_vault` issued, with the same
//! reverse lookup `search_vault` uses. A meeting that resolves to no listed
//! entry keeps `entry_id: None` and is shown without a link -- unlike a
//! search hit it is not dropped, because the page is a record of where the
//! person spoke, not a list of places to go.

use std::collections::HashMap;
use std::path::PathBuf;

use serde::Serialize;

use crate::error::AppError;
use crate::service::{
    Person, PersonDetail, PersonProject, PersonRecord, PersonSegment, PersonUpdate, PersonVoice,
    ServiceError,
};

use super::search::resolve_hit_dir;
use super::AppState;

/// One meeting a person was labelled in: the service's row with its
/// `meeting_dir` replaced by the entry id the UI navigates by.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct SpeakerMeetingView {
    /// The vault entry id (`list_vault`'s), never a path. `None` when the
    /// meeting is not in the current listing.
    pub entry_id: Option<String>,
    pub project: String,
    /// The meeting's folder name.
    pub meeting: String,
    pub labelled_segments: u64,
    pub hand_segments: u64,
    pub speech_sec: f64,
    /// `"ok" | "unconfirmed" | "partial" | "short" | "conflict"`, or `None`
    /// when the meeting holds no voice sample of this person.
    pub voice_quality: Option<String>,
    /// For a `conflict`: whose voice the sample sounds like.
    pub voice_conflicts_with: Option<String>,
    pub segments: Vec<PersonSegment>,
    pub segments_truncated: bool,
}

/// The speaker page's data.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct SpeakerDetailView {
    pub name: String,
    pub aliases: Vec<String>,
    pub bio: String,
    pub registered: bool,
    pub projects: Vec<PersonProject>,
    pub voice: PersonVoice,
    /// Newest first, as the service answers them.
    pub meetings: Vec<SpeakerMeetingView>,
}

/// A `400` is the service refusing the operator's input (an empty or
/// overlong name, a rename onto another person): its own sentence is the
/// message, without the "service error 400" wrapping a real fault gets.
fn map_people_error(err: ServiceError) -> AppError {
    match err {
        ServiceError::Http {
            status: 400,
            message,
        } => AppError::invalid_argument(message),
        other => super::llm::map_service_error(other),
    }
}

fn required_name(name: &str) -> Result<String, AppError> {
    let trimmed = name.trim();
    if trimmed.is_empty() {
        return Err(AppError::invalid_argument("a speaker needs a name"));
    }
    Ok(trimmed.to_string())
}

/// `list_speakers` -- everybody the database knows, registered or only
/// labelled, with their vault-wide counts.
pub async fn list_speakers_handler(state: &AppState) -> Result<Vec<Person>, AppError> {
    let service = state.service.read().await.clone();
    service.list_people().await.map_err(map_people_error)
}

/// The reverse lookup `meeting path -> entry id`. The speaker page can be
/// the first thing opened after a start, so an empty index is filled by
/// listing the vault once; a listing that fails (no root yet) simply leaves
/// every meeting without a link.
async fn entry_ids_by_path(state: &AppState) -> HashMap<PathBuf, String> {
    if state.vault_index.read().await.is_empty() {
        let _ = super::list_vault_handler(state).await;
    }
    state
        .vault_index
        .read()
        .await
        .iter()
        .map(|(id, path)| (path.clone(), id.clone()))
        .collect()
}

/// `speaker_detail` -- one person by any of their names.
pub async fn speaker_detail_handler(
    state: &AppState,
    name: &str,
) -> Result<SpeakerDetailView, AppError> {
    let name = required_name(name)?;
    let service = state.service.read().await.clone();
    let detail: PersonDetail = service
        .person_detail(&name)
        .await
        .map_err(map_people_error)?;

    let root = state
        .settings
        .read()
        .await
        .meetings_root
        .clone()
        .map(PathBuf::from);
    let by_path = entry_ids_by_path(state).await;

    Ok(SpeakerDetailView {
        name: detail.name,
        aliases: detail.aliases,
        bio: detail.bio,
        registered: detail.registered,
        projects: detail.projects,
        voice: detail.voice,
        meetings: detail
            .meetings
            .into_iter()
            .map(|meeting| SpeakerMeetingView {
                entry_id: root
                    .as_ref()
                    .and_then(|root| resolve_hit_dir(root, &meeting.meeting_dir))
                    .and_then(|absolute| by_path.get(&absolute).cloned()),
                project: meeting.project,
                meeting: meeting.meeting,
                labelled_segments: meeting.labelled_segments,
                hand_segments: meeting.hand_segments,
                speech_sec: meeting.speech_sec,
                voice_quality: meeting.voice_quality,
                voice_conflicts_with: meeting.voice_conflicts_with,
                segments: meeting.segments,
                segments_truncated: meeting.segments_truncated,
            })
            .collect(),
    })
}

/// `save_speaker` -- creates the person, or renames them / replaces their
/// aliases / replaces their bio. A field left `None` is left alone.
pub async fn save_speaker_handler(
    state: &AppState,
    name: &str,
    new_name: Option<String>,
    aliases: Option<Vec<String>>,
    bio: Option<String>,
) -> Result<PersonRecord, AppError> {
    let name = required_name(name)?;
    let new_name = new_name.as_deref().map(required_name).transpose()?;
    let service = state.service.read().await.clone();
    service
        .save_person(PersonUpdate {
            name,
            new_name,
            aliases,
            bio,
        })
        .await
        .map_err(map_people_error)
}

/// `delete_speaker` -- drops the registry entry. Meeting labels are not
/// touched, so a person still labelled somewhere stays listed, unregistered.
pub async fn delete_speaker_handler(state: &AppState, name: &str) -> Result<bool, AppError> {
    let name = required_name(name)?;
    let service = state.service.read().await.clone();
    service.delete_person(&name).await.map_err(map_people_error)
}

// -- `#[tauri::command]` wrappers -------------------------------------------

#[tauri::command]
pub async fn list_speakers(state: tauri::State<'_, AppState>) -> Result<Vec<Person>, AppError> {
    list_speakers_handler(&state).await
}

#[tauri::command]
pub async fn speaker_detail(
    state: tauri::State<'_, AppState>,
    name: String,
) -> Result<SpeakerDetailView, AppError> {
    speaker_detail_handler(&state, &name).await
}

#[tauri::command]
pub async fn save_speaker(
    state: tauri::State<'_, AppState>,
    name: String,
    new_name: Option<String>,
    aliases: Option<Vec<String>>,
    bio: Option<String>,
) -> Result<PersonRecord, AppError> {
    save_speaker_handler(&state, &name, new_name, aliases, bio).await
}

#[tauri::command]
pub async fn delete_speaker(
    state: tauri::State<'_, AppState>,
    name: String,
) -> Result<bool, AppError> {
    delete_speaker_handler(&state, &name).await
}

#[cfg(test)]
mod tests {
    use std::path::Path;
    use std::sync::Arc;

    use tempfile::tempdir;

    use crate::commands::{list_vault_handler, ServiceStatusSink, ServiceStatusView};
    use crate::config::Settings;
    use crate::error::ErrorKind;
    use crate::service::fake::FakeService;
    use crate::service::PersonMeeting;

    use super::*;

    fn run<F: std::future::Future>(future: F) -> F::Output {
        tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .expect("build tokio runtime")
            .block_on(future)
    }

    struct NoopStatusSink;
    impl ServiceStatusSink for NoopStatusSink {
        fn emit(&self, _status: &ServiceStatusView) {}
    }

    struct NoopEventSink;
    impl crate::jobs::EventSink for NoopEventSink {
        fn emit(&self, _snapshot: &crate::jobs::JobSnapshot) {}
    }

    fn state_with_root(root: &Path, service: Arc<FakeService>) -> AppState {
        let settings = Settings {
            meetings_root: Some(root.to_string_lossy().into_owned()),
            ..Settings::default()
        };
        AppState::new(
            root.to_path_buf(),
            root.to_path_buf(),
            settings,
            root.to_path_buf(),
            service,
            None,
            false,
            Arc::new(NoopEventSink),
            Arc::new(NoopStatusSink),
        )
    }

    fn make_meeting(root: &Path, project: &str, name: &str) {
        let dir = root.join(project).join(name);
        std::fs::create_dir_all(&dir).expect("mkdir");
        std::fs::write(dir.join("source.mp4"), b"bytes").expect("write source");
    }

    fn meeting(project: &str, name: &str) -> PersonMeeting {
        PersonMeeting {
            project: project.to_string(),
            meeting: name.to_string(),
            meeting_dir: format!("{project}/{name}"),
            labelled_segments: 3,
            hand_segments: 2,
            speech_sec: 12.5,
            voice_quality: Some("ok".to_string()),
            voice_conflicts_with: None,
            segments: vec![PersonSegment {
                id: 7,
                start: 63.2,
                end: 70.1,
                text: "hello".to_string(),
            }],
            segments_truncated: true,
        }
    }

    fn nikita(meetings: Vec<PersonMeeting>) -> PersonDetail {
        PersonDetail {
            name: "Nikita".to_string(),
            aliases: vec!["Никита".to_string()],
            bio: "Lead".to_string(),
            registered: true,
            projects: vec![PersonProject {
                project: "GIS".to_string(),
                meetings: 2,
                speech_sec: 25.0,
                in_roster: true,
            }],
            voice: PersonVoice {
                samples: 1,
                set_aside: 0,
                speech_sec: 12.5,
            },
            meetings,
        }
    }

    #[test]
    fn list_speakers_passes_the_services_people_through() {
        run(async {
            let root = tempdir().expect("tempdir");
            let fake = Arc::new(FakeService::new());
            fake.set_people(vec![nikita(vec![meeting("GIS", "260903 - Flows")])]);
            let state = state_with_root(root.path(), fake);

            let people = list_speakers_handler(&state).await.expect("list");

            assert_eq!(people.len(), 1);
            assert_eq!(people[0].name, "Nikita");
            assert_eq!(people[0].projects, vec!["GIS".to_string()]);
            assert_eq!(people[0].meetings, 1);
            assert_eq!(people[0].hand_segments, 2);
            assert_eq!(people[0].voice_samples, 1);
        });
    }

    #[test]
    fn detail_maps_each_meeting_to_its_entry_id_and_keeps_an_unresolvable_one_unlinked() {
        run(async {
            let root = tempdir().expect("tempdir");
            make_meeting(root.path(), "GIS", "260903 - Flows");
            let fake = Arc::new(FakeService::new());
            fake.set_people(vec![nikita(vec![
                meeting("GIS", "260903 - Flows"),
                // Labelled once, gone from the vault since.
                meeting("GIS", "260801 - Ghost"),
                // A directory that would escape the root must never resolve.
                PersonMeeting {
                    meeting_dir: "../outside".to_string(),
                    ..meeting("GIS", "260701 - Escape")
                },
            ])]);
            let state = state_with_root(root.path(), fake);
            let entries = list_vault_handler(&state).await.expect("list vault");
            let id = entries[0].id.clone();

            // Addressed by an alias: any of the person's names works.
            let detail = speaker_detail_handler(&state, "Никита")
                .await
                .expect("detail");

            assert_eq!(detail.name, "Nikita");
            assert_eq!(detail.bio, "Lead");
            assert!(detail.projects[0].in_roster);
            assert_eq!(detail.meetings.len(), 3, "no meeting is dropped");
            assert_eq!(detail.meetings[0].entry_id.as_deref(), Some(id.as_str()));
            assert_eq!(detail.meetings[0].segments[0].text, "hello");
            assert!(detail.meetings[0].segments_truncated);
            assert_eq!(detail.meetings[1].entry_id, None);
            assert_eq!(detail.meetings[1].meeting, "260801 - Ghost");
            assert_eq!(detail.meetings[2].entry_id, None);
        });
    }

    #[test]
    fn detail_lists_the_vault_itself_when_nothing_has_been_listed_yet() {
        run(async {
            let root = tempdir().expect("tempdir");
            make_meeting(root.path(), "GIS", "260903 - Flows");
            let fake = Arc::new(FakeService::new());
            fake.set_people(vec![nikita(vec![meeting("GIS", "260903 - Flows")])]);
            let state = state_with_root(root.path(), fake);

            // No `list_vault` call first: the handler has to seed the index.
            let detail = speaker_detail_handler(&state, "Nikita")
                .await
                .expect("detail");

            let entry_id = detail.meetings[0]
                .entry_id
                .clone()
                .expect("the meeting resolves once the vault is listed");
            assert!(state.vault_index.read().await.contains_key(&entry_id));
        });
    }

    #[test]
    fn the_detail_view_never_serializes_a_meeting_directory() {
        run(async {
            let root = tempdir().expect("tempdir");
            make_meeting(root.path(), "GIS", "260903 - Flows");
            let fake = Arc::new(FakeService::new());
            fake.set_people(vec![nikita(vec![meeting("GIS", "260903 - Flows")])]);
            let state = state_with_root(root.path(), fake);

            let detail = speaker_detail_handler(&state, "Nikita")
                .await
                .expect("detail");
            let json = serde_json::to_value(&detail).expect("serialize");

            let first = &json["meetings"][0];
            assert!(first.get("meeting_dir").is_none());
            assert!(first["entry_id"].is_string());
        });
    }

    #[test]
    fn an_unknown_speaker_is_a_service_error_and_a_blank_name_never_reaches_the_service() {
        run(async {
            let root = tempdir().expect("tempdir");
            let state = state_with_root(root.path(), Arc::new(FakeService::new()));

            let unknown = speaker_detail_handler(&state, "Ghost")
                .await
                .expect_err("unknown name");
            assert_eq!(unknown.kind(), ErrorKind::Service);
            assert!(unknown.message().contains("404"));

            let blank = speaker_detail_handler(&state, "   ")
                .await
                .expect_err("blank name");
            assert_eq!(blank.kind(), ErrorKind::InvalidArgument);
        });
    }

    #[test]
    fn save_creates_renames_and_reports_a_refused_name_in_the_services_words() {
        run(async {
            let root = tempdir().expect("tempdir");
            let fake = Arc::new(FakeService::new());
            let state = state_with_root(root.path(), fake.clone());

            let created = save_speaker_handler(&state, " Anna ", None, None, None)
                .await
                .expect("create");
            assert_eq!(created.name, "Anna");
            assert!(created.registered);
            save_speaker_handler(&state, "Boris", None, None, None)
                .await
                .expect("create");

            let renamed = save_speaker_handler(
                &state,
                "Anna",
                Some("Anna K".to_string()),
                None,
                Some("PM".to_string()),
            )
            .await
            .expect("rename");
            assert_eq!(renamed.name, "Anna K");
            assert_eq!(renamed.aliases, vec!["Anna".to_string()]);
            assert_eq!(renamed.bio, "PM");

            let clash =
                save_speaker_handler(&state, "Anna K", Some("Boris".to_string()), None, None)
                    .await
                    .expect_err("a rename onto another person");
            assert_eq!(clash.kind(), ErrorKind::InvalidArgument);
            assert_eq!(clash.message(), "that name belongs to another person");

            let blank = save_speaker_handler(&state, "Anna K", Some("  ".to_string()), None, None)
                .await
                .expect_err("a blank new name");
            assert_eq!(blank.kind(), ErrorKind::InvalidArgument);
        });
    }

    #[test]
    fn delete_reports_whether_an_entry_was_removed() {
        run(async {
            let root = tempdir().expect("tempdir");
            let state = state_with_root(root.path(), Arc::new(FakeService::new()));
            save_speaker_handler(&state, "Anna", None, None, None)
                .await
                .expect("create");

            assert!(delete_speaker_handler(&state, "Anna")
                .await
                .expect("delete"));
            assert!(!delete_speaker_handler(&state, "Anna")
                .await
                .expect("delete again"));
            assert!(list_speakers_handler(&state)
                .await
                .expect("list")
                .is_empty());
        });
    }

    #[test]
    fn a_service_that_is_down_is_reported_as_unavailable() {
        run(async {
            let root = tempdir().expect("tempdir");
            let fake = Arc::new(FakeService::new());
            fake.set_down(true);
            let state = state_with_root(root.path(), fake);

            let err = list_speakers_handler(&state).await.expect_err("down");
            assert_eq!(err.kind(), ErrorKind::ServiceUnavailable);
        });
    }
}
