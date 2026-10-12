use anyhow::Context;

// A returned error prints with Debug: the message and its whole "Caused by" chain.
fn main() -> anyhow::Result<()> {
    // Before anything reads HOME or starts a thread: a supervisor may have started us without it.
    silicon::server::init_home();
    silicon::init_bundle_path().context("put the bundled tools on PATH")?;
    silicon::cli::silicon()
}
