#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cargo build --locked --manifest-path "$root/Cargo.toml"
exec python3 "$root/tests/e2e.py"
