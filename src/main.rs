use anyhow::Context;

// A returned error prints with Debug: the message and its whole "Caused by" chain.
fn main() -> anyhow::Result<()> {
    // Before anything reads HOME or starts a thread: a supervisor may have started us without it.
    silicon::server::init_home();
    silicon::init_bundle_path().context("put the bundled tools on PATH")?;
    // Supervisors and cron run the interpreter unattended (cron every five minutes); those
    // runs are not someone invoking the CLI. A supervised serve also reads the user's
    // telemetry preference from service.env only later, in serve.
    let words: Vec<String> = std::env::args()
        .skip(1)
        .filter(|word| !word.starts_with('-'))
        .take(2)
        .collect();
    let unattended = std::env::var_os("SILICON_SERVICE").is_some_and(|value| !value.is_empty())
        || matches!(
            words
                .iter()
                .map(String::as_str)
                .collect::<Vec<_>>()
                .as_slice(),
            ["service", "ensure" | "run"]
        );
    if !unattended {
        silicon::telemetry::interpreter(
            "cli",
            "invoked",
            serde_json::json!({"command":std::env::args().nth(1)}),
        );
    }
    let result = silicon::cli::silicon();
    silicon::telemetry::flush();
    result
}
