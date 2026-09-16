fn main() -> anyhow::Result<()> {
    let result = silicon::cli::si();
    silicon::telemetry::flush();
    result
}
