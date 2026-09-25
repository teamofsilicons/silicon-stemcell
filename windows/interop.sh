#!/bin/sh
set -eu
case "${1-}" in
    check)
        # Keep what Windows PowerShell said, so the installer shows why WSL could not launch it.
        status=0
        output=$("$2" -NoLogo -NoProfile -NonInteractive -Command 'exit 0' 2>&1) || status=$?
        [ "$status" -ne 0 ] || exit 0
        printf 'WSL could not run Windows PowerShell: `%s -NoLogo -NoProfile -NonInteractive -Command "exit 0"` failed: exit status: %s\noutput:\n%s\n' "$2" "$status" "${output:-(empty)}" >&2
        exit "$status"
        ;;
    repair)
        directory=/proc/sys/fs/binfmt_misc
        state=$(cat "$directory/status") || { echo "Could not read $directory/status; binfmt_misc may not be mounted (cat's error is above)." >&2; exit 1; }
        [ "$state" = enabled ] || { echo "WSL binary interoperability is disabled by system policy: $directory/status says '$state'." >&2; exit 1; }
        for entry in "$directory"/WSLInterop*; do
            [ ! -e "$entry" ] || { printf 'An existing WSL interoperability entry failed; its policy was not changed. %s says:\n%s\n' "$entry" "$(cat "$entry" 2>&1)" >&2; exit 1; }
        done
        # Match WSL2's VM registration. Do not change other entries.
        printf ':WSLInterop:M::MZ::/init:FP\n' > "$directory/register" || { echo "Could not register WSLInterop through $directory/register (the error is above)." >&2; exit 1; }
        ;;
    *) printf 'usage: interop.sh check POWERSHELL | interop.sh repair (got %s)\n' "${1-nothing}" >&2; exit 2 ;;
esac
