#!/bin/sh
set -eu
case "$1" in
    check)
        "$2" -NoLogo -NoProfile -NonInteractive -Command 'exit 0' >/dev/null 2>&1
        ;;
    repair)
        directory=/proc/sys/fs/binfmt_misc
        [ "$(cat "$directory/status")" = enabled ] || { echo 'WSL binary interoperability is disabled by system policy.' >&2; exit 1; }
        for entry in "$directory"/WSLInterop*; do
            [ ! -e "$entry" ] || { echo 'An existing WSL interoperability entry failed; its policy was not changed.' >&2; exit 1; }
        done
        # Match WSL2's VM registration. Do not change other entries.
        printf ':WSLInterop:M::MZ::/init:FP\n' > "$directory/register"
        ;;
    *) exit 2 ;;
esac
