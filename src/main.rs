fn main() -> anyhow::Result<()> {
    silicon::telemetry::interpreter(
        "cli",
        "invoked",
        serde_json::json!({"command":std::env::args().nth(1)}),
    );
    let result = silicon::cli::silicon();
    silicon::telemetry::flush();
    result
}
