use anyhow::{anyhow, bail, Context, Result};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use uuid::Uuid;

pub fn private_dir(path: &Path) -> Result<()> {
    #[cfg(target_os = "linux")]
    if std::env::var("SILICON_WSL").as_deref() == Ok("1") {
        validate_wsl_filesystem(path)?;
    }
    fs::create_dir_all(path)
        .with_context(|| format!("cannot create directory {}", path.display()))?;
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))
        .with_context(|| format!("cannot make {} private (mode 700)", path.display()))?;
    Ok(())
}

/// Check the actual filesystem before creating state, following existing symlinks.
#[cfg(target_os = "linux")]
pub(crate) fn validate_wsl_filesystem(path: &Path) -> Result<()> {
    use std::os::unix::ffi::OsStrExt;
    let absolute = std::path::absolute(path)
        .with_context(|| format!("cannot make {} absolute", path.display()))?;
    let mut probe = absolute.as_path();
    loop {
        let name = std::ffi::CString::new(probe.as_os_str().as_bytes())
            .with_context(|| format!("path {} contains a NUL byte", probe.display()))?;
        let mut filesystem = std::mem::MaybeUninit::<libc::statfs>::uninit();
        if unsafe { libc::statfs(name.as_ptr(), filesystem.as_mut_ptr()) } == 0 {
            let found = unsafe { filesystem.assume_init() }.f_type;
            if found != libc::EXT4_SUPER_MAGIC {
                bail!("Windows WSL installation requires configuration and private state inside the Silicon distribution's Linux filesystem; copy your project to /home/silicon first. Windows data files remain accessible through /mnt/c and other mounted drives. {} (for {}) is on filesystem type {found:#x}, not ext4", probe.display(), path.display());
            }
            return Ok(());
        }
        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::NotFound {
            return Err(error).with_context(|| {
                format!(
                    "inspect WSL configuration filesystem at {}",
                    probe.display()
                )
            });
        }
        probe = probe.parent().ok_or(error).with_context(|| {
            format!(
                "find WSL configuration filesystem for {}",
                absolute.display()
            )
        })?;
    }
}

pub fn write_json(path: &Path, value: &impl Serialize) -> Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| anyhow!("state path {} has no parent", path.display()))?;
    let saving = || format!("saving {} failed", path.display());
    private_dir(parent).with_context(saving)?;
    let staged = parent.join(format!(".{}.tmp", Uuid::new_v4()));
    let at = |step: &str, file: &Path| format!("{step} {}", file.display());
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&staged)
        .with_context(|| at("cannot create", &staged))
        .with_context(saving)?;
    let result = (|| -> Result<()> {
        serde_json::to_writer_pretty(&mut file, value)
            .with_context(|| at("cannot serialize state into", &staged))?;
        file.write_all(b"\n")
            .with_context(|| at("cannot write", &staged))?;
        file.sync_all()
            .with_context(|| at("cannot sync", &staged))?;
        fs::rename(&staged, path)
            .with_context(|| format!("cannot rename {} to {}", staged.display(), path.display()))?;
        File::open(parent)
            .and_then(|directory| directory.sync_all())
            .with_context(|| at("cannot sync directory", parent))?;
        Ok(())
    })();
    // A leftover staged copy is reported with the failure that left it behind.
    result.map_err(|error| match fs::remove_file(&staged) {
        Err(cleanup) if cleanup.kind() != std::io::ErrorKind::NotFound => error.context(format!(
            "saving {} failed and its staged copy {} remains ({cleanup})",
            path.display(),
            staged.display()
        )),
        _ => error.context(saving()),
    })
}

pub fn append_json(path: &Path, value: &impl Serialize) -> Result<()> {
    private_dir(
        path.parent()
            .ok_or_else(|| anyhow!("log {} has no parent", path.display()))?,
    )?;
    let mut data = serde_json::to_vec(value)
        .with_context(|| format!("cannot serialize a record for {}", path.display()))?;
    data.push(b'\n');
    OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .open(path)
        .and_then(|mut file| file.write_all(&data))
        .with_context(|| format!("cannot append to {}", path.display()))?;
    Ok(())
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Session {
    /// Logical address; the Omni session UUID never changes when an archive is named.
    pub id: String,
    pub session_id: Uuid,
    pub isi: String,
    pub title: String,
    pub description: String,
    pub first: DateTime<Utc>,
    pub last: DateTime<Utc>,
    pub archived_at: Option<DateTime<Utc>>,
    pub status: String,
    pub new_messages: u64,
    pub last_suggestion: Option<DateTime<Utc>>,
    pub messages_at_suggestion: u64,
    /// Ephemeral work returns its final output to the calling ISI and retires when
    /// idle, whether or not the session is kept.
    pub ephemeral: bool,
    /// Use-and-throw: global ephemeral work only. Session-addressed ephemeral work
    /// is created and reached by an id its caller holds, so it is retained and
    /// archived like a persistent session. Records written before this field
    /// existed are all persistent, so defaulting to kept is correct.
    #[serde(default)]
    pub disposable: bool,
}

impl Session {
    pub fn new(
        isi: &str,
        id: Option<&str>,
        title: &str,
        ephemeral: bool,
        disposable: bool,
    ) -> Self {
        let session_id = Uuid::new_v4();
        Self {
            id: id
                .map(str::to_owned)
                .unwrap_or_else(|| session_id.to_string()),
            session_id,
            isi: isi.to_owned(),
            title: title.to_owned(),
            description: String::new(),
            first: Utc::now(),
            last: Utc::now(),
            archived_at: None,
            status: "idle".into(),
            new_messages: 0,
            last_suggestion: None,
            messages_at_suggestion: 0,
            ephemeral,
            disposable,
        }
    }
    pub fn path(&self, home: &Path) -> PathBuf {
        home.join(".silicon/sessions")
            .join(if self.archived_at.is_some() {
                "archived"
            } else {
                "active"
            })
            .join(&self.isi)
            .join(format!("{}.json", self.session_id))
    }
    pub fn save(&self, home: &Path) -> Result<()> {
        if !self.disposable || self.archived_at.is_some() {
            write_json(&self.path(home), self)?;
        }
        Ok(())
    }
    pub fn archive(
        &mut self,
        home: &Path,
        id: Option<&str>,
        title: Option<&str>,
        description: Option<&str>,
    ) -> Result<()> {
        let active = self.path(home);
        if let Some(id) = id {
            if id.is_empty() {
                bail!("archive id cannot be empty");
            }
            self.id = id.to_owned();
        }
        if let Some(title) = title {
            self.title = title.to_owned();
        }
        if let Some(description) = description {
            self.description = description.to_owned();
        }
        self.archived_at = Some(Utc::now());
        self.last = Utc::now();
        self.status = "archived".into();
        self.save(home)?;
        if active.exists() {
            fs::remove_file(&active).with_context(|| {
                format!(
                    "archived, but cannot remove active copy {}",
                    active.display()
                )
            })?;
        }
        Ok(())
    }
}

pub fn sessions(home: &Path, isi: &str, archived: bool) -> Result<Vec<Session>> {
    let dir = home
        .join(".silicon/sessions")
        .join(if archived { "archived" } else { "active" })
        .join(isi);
    if !dir.exists() {
        return Ok(Vec::new());
    }
    let mut found = Vec::new();
    let listing = || format!("cannot list sessions in {}", dir.display());
    for entry in fs::read_dir(&dir).with_context(listing)? {
        let path = entry.with_context(listing)?.path();
        if path.extension().is_some_and(|s| s == "json") {
            let bytes =
                fs::read(&path).with_context(|| format!("cannot read {}", path.display()))?;
            found.push(
                serde_json::from_slice::<Session>(&bytes)
                    .with_context(|| format!("invalid session state {}", path.display()))?,
            );
        }
    }
    found.sort_by_key(|s| std::cmp::Reverse(s.last));
    Ok(found)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[cfg(target_os = "linux")]
    #[test]
    fn wsl_state_rejects_a_redirected_parent_before_writing() {
        // Isolate SILICON_WSL from other tests in this process.
        if std::env::var_os("SILICON_WSL_STATE_TEST").is_none() {
            let status = std::process::Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "state::tests::wsl_state_rejects_a_redirected_parent_before_writing",
                ])
                .env("SILICON_WSL", "1")
                .env("SILICON_WSL_STATE_TEST", "1")
                .status()
                .unwrap();
            assert!(status.success());
            return;
        }
        let directory = tempfile::tempdir().unwrap();
        let alias = directory.path().join(".silicon");
        std::os::unix::fs::symlink("/proc", &alias).unwrap();
        let path = alias.join(format!("silicon-state-{}/credentials.json", Uuid::new_v4()));
        let error = format!(
            "{:#}",
            write_json(&path, &serde_json::json!({"secret": "private"})).unwrap_err()
        );
        assert!(
            error.contains("copy your project to /home/silicon"),
            "{error}"
        );
        assert!(!path.exists());
    }

    #[test]
    fn state_failures_name_the_path_and_the_operating_system_reason() {
        let dir = tempfile::tempdir().unwrap();
        let blocker = dir.path().join("blocker");
        fs::write(&blocker, "a file, not a directory").unwrap();
        let path = blocker.join("state.json");
        let error = format!(
            "{:#}",
            write_json(&path, &serde_json::json!({"ok": true})).unwrap_err()
        );
        assert!(
            error.contains(&format!("saving {} failed", path.display()))
                && error.contains(&format!("cannot create directory {}", blocker.display()))
                && error.contains("(os error"),
            "{error}"
        );
        let error = format!(
            "{:#}",
            append_json(&path, &serde_json::json!({"ok": true})).unwrap_err()
        );
        assert!(error.contains(&blocker.display().to_string()), "{error}");
        // A directory in the way of the rename names both paths and the OS reason.
        let occupied = dir.path().join("occupied");
        fs::create_dir_all(occupied.join("child")).unwrap();
        let error = format!(
            "{:#}",
            write_json(&occupied, &serde_json::json!({})).unwrap_err()
        );
        assert!(
            error.contains(&format!("saving {} failed", occupied.display()))
                && error.contains("cannot rename")
                && error.contains("(os error"),
            "{error}"
        );
        // The failed save leaves no staged copy behind.
        let leftovers: Vec<_> = fs::read_dir(dir.path())
            .unwrap()
            .filter_map(|entry| entry.ok())
            .filter(|entry| entry.file_name().to_string_lossy().ends_with(".tmp"))
            .collect();
        assert!(leftovers.is_empty());
        let sessions_dir = dir.path().join(".silicon/sessions/active/worker");
        fs::create_dir_all(&sessions_dir).unwrap();
        fs::write(sessions_dir.join("broken.json"), "{\"id\":").unwrap();
        let error = format!("{:#}", sessions(dir.path(), "worker", false).unwrap_err());
        assert!(
            error.contains("broken.json") && error.contains("EOF while parsing"),
            "{error}"
        );
    }

    #[test]
    fn archive_keeps_original_time_and_safe_disk_identity() {
        let dir = tempfile::tempdir().unwrap();
        let mut session = Session::new(
            "worker",
            Some("job/with arbitrary text"),
            "Build",
            false,
            false,
        );
        let first = session.first;
        session.save(dir.path()).unwrap();
        session
            .archive(dir.path(), Some("release"), None, Some("Finished"))
            .unwrap();
        assert!(sessions(dir.path(), "worker", false).unwrap().is_empty());
        let archive = sessions(dir.path(), "worker", true).unwrap();
        assert_eq!(archive[0].first, first);
        assert_eq!(archive[0].id, "release");
    }
}
