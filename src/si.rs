use anyhow::Context;

// A returned error prints with Debug: the message and its whole "Caused by" chain.
fn main() -> anyhow::Result<()> {
    silicon::init_bundle_path().context("put the bundled tools on PATH")?;
    let result = silicon::cli::si();
    silicon::telemetry::flush();
    result
}
