use anyhow::Context;
use std::io::Write;

fn main() {
    if let Err(error) = run() {
        let _ = writeln!(
            std::io::stderr().lock(),
            "{}",
            silicon::error_report(&error)
        );
        std::process::exit(1);
    }
}

fn run() -> anyhow::Result<()> {
    silicon::init_bundle_path().context("put the bundled tools on PATH")?;
    silicon::cli::si()
}
