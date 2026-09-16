fn main() -> anyhow::Result<()> {
    silicon::init_bundle_path()?;
    let result = silicon::cli::si();
    silicon::telemetry::flush();
    result
}
