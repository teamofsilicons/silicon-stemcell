use anyhow::Context;

// A returned error prints with Debug: the message and its whole "Caused by" chain.
fn main() -> anyhow::Result<()> {
    silicon::init_bundle_path().context("put the bundled tools on PATH")?;
    silicon::telemetry::interpreter(
        "cli",
        "invoked",
        serde_json::json!({"command":std::env::args().nth(1)}),
    );
    let result = silicon::cli::silicon();
    silicon::telemetry::flush();
    result
}
