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
    fs::create_dir_all(path)?;
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    Ok(())
}

/// Check the actual filesystem before creating state, following existing symlinks.
#[cfg(target_os = "linux")]
pub(crate) fn validate_wsl_filesystem(path: &Path) -> Result<()> {
    use std::os::unix::ffi::OsStrExt;
    let absolute = std::path::absolute(path)?;
    let mut probe = absolute.as_path();
    loop {
        let name = std::ffi::CString::new(probe.as_os_str().as_bytes())?;
        let mut filesystem = std::mem::MaybeUninit::<libc::statfs>::uninit();
        if unsafe { libc::statfs(name.as_ptr(), filesystem.as_mut_ptr()) } == 0 {
            if unsafe { filesystem.assume_init() }.f_type != libc::EXT4_SUPER_MAGIC {
                bail!("Windows WSL installation requires configuration and private state inside the Silicon distribution's Linux filesystem; copy your project to /home/silicon first. Windows data files remain accessible through /mnt/c and other mounted drives");
            }
            return Ok(());
        }
        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::NotFound {
            return Err(error).context("inspect WSL configuration filesystem");
        }
        probe = probe
            .parent()
            .ok_or(error)
            .context("find WSL configuration filesystem")?;
    }
}

pub fn write_json(path: &Path, value: &impl Serialize) -> Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| anyhow!("state path has no parent"))?;
    private_dir(parent)?;
    let staged = parent.join(format!(".{}.tmp", Uuid::new_v4()));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&staged)?;
    let result = (|| {
        serde_json::to_writer_pretty(&mut file, value)?;
        file.write_all(b"\n")?;
        file.sync_all()?;
        fs::rename(&staged, path)?;
        File::open(parent)?.sync_all()?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&staged);
    }
    result
}

pub fn append_json(path: &Path, value: &impl Serialize) -> Result<()> {
    private_dir(path.parent().ok_or_else(|| anyhow!("log has no parent"))?)?;
    let mut data = serde_json::to_vec(value)?;
    data.push(b'\n');
    OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .open(path)?
        .write_all(&data)?;
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
    pub ephemeral: bool,
}

impl Session {
    pub fn new(isi: &str, id: Option<&str>, title: &str, ephemeral: bool) -> Self {
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
        if !self.ephemeral || self.archived_at.is_some() {
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
            fs::remove_file(active)?;
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
    for entry in fs::read_dir(&dir)? {
        let path = entry?.path();
        if path.extension().is_some_and(|s| s == "json") {
            found.push(
                serde_json::from_slice::<Session>(&fs::read(&path)?)
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
        let error = write_json(&path, &serde_json::json!({"secret": "private"}))
            .unwrap_err()
            .to_string();
        assert!(
            error.contains("copy your project to /home/silicon"),
            "{error}"
        );
        assert!(!path.exists());
    }

    #[test]
    fn archive_keeps_original_time_and_safe_disk_identity() {
        let dir = tempfile::tempdir().unwrap();
        let mut session = Session::new("worker", Some("job/with arbitrary text"), "Build", false);
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
