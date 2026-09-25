#!/bin/bash
set -euo pipefail
export HOME=/home/silicon SILICON_WSL=1
export PATH="$HOME/.local/share/silicon/bin:$HOME/.silicon/bin:$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
command=$1
windows_cwd=$2
shift 2
case "$command" in silicon|si|omnid|silicon-omni|omni|so|caddy|iam|honeycomb|spacestation|dm|briefcase|waveform|commit|remind|hook|ting) ;; *) echo "Unknown Silicon command '$command'" >&2; exit 2 ;; esac
if [ -n "${SILICON_HOME:-}" ]; then
    case "$SILICON_HOME" in /*) ;; *) echo "Windows SILICON_HOME must name an absolute path inside the Silicon WSL2 distribution, not '$SILICON_HOME'." >&2; exit 2 ;; esac
    cd -- "$SILICON_HOME" || { status=$?; echo "Could not enter SILICON_HOME '$SILICON_HOME' inside the Silicon WSL2 distribution (cd's error is above)." >&2; exit "$status"; }
else
    # UNC paths under \\wsl.localhost\Silicon work too. Windows working directories
    # remain available for explicit data paths; configuration homes must be Linux FS.
    cwd=$(wslpath -u "$windows_cwd") || { status=$?; echo "Could not translate the Windows working directory '$windows_cwd' into WSL: wslpath exited $status (its error is above)." >&2; exit "$status"; }
    cd -- "$cwd" || { status=$?; echo "Could not enter '$cwd', the WSL path of the Windows working directory '$windows_cwd' (cd's error is above)." >&2; exit "$status"; }
fi
homes=("$HOME" "${SILICON_HOME:-$HOME}" "${SILICON_INTERPRETER_HOME:-$HOME/.silicon-interpreter}")
while IFS= read -r name; do
    case "$name" in SILICON_*|OMNI_*|SPACE_STATION_*|HONEYCOMB_*|BRIEFCASE_*|DM_*|WAVEFORM_*|COMMIT_*|REMIND_*|HOOK_*|TING_*|IAM_*)
        case "$name" in *_HOME|*_DIR) [ -z "${!name}" ] || homes+=("${!name}") ;; esac
    esac
done < <(compgen -e)
for home in "${homes[@]}"; do
    probe=$home
    while [ ! -e "$probe" ]; do probe=$(dirname -- "$probe"); done
    filesystem=$(stat -f -c %T -- "$probe") || { status=$?; echo "Could not check which filesystem holds $home: stat exited $status on $probe (its error is above)." >&2; exit 2; }
    if [ "$filesystem" != ext2/ext3 ]; then
        echo "Silicon configuration and credentials must live in the WSL2 Linux filesystem, but $home is on $filesystem. Copy the project to /home/silicon and use /mnt/c for Windows data files." >&2
        exit 2
    fi
done
args=()
path_operand=false
for arg do
    if [ "$command" = silicon ] && [ "$path_operand" = true ] && [ "$arg" != --json ] && [ "$arg" != -- ]; then
        case "$arg" in
            [A-Za-z]:\\*|[A-Za-z]:/*|\\\\wsl.localhost\\*|\\\\wsl\$\\*)
                translated=$(wslpath -u "$arg") || { status=$?; echo "Could not translate the Windows path '$arg' into WSL: wslpath exited $status (its error is above)." >&2; exit "$status"; }
                arg=$translated
                ;;
            *\\*) arg=${arg//\\//} ;;
        esac
        path_operand=false
    elif [ "$command" = silicon ] && [ "${#args[@]}" -lt 2 ]; then
        case "$arg" in compile|connect|disconnect) path_operand=true ;; esac
    fi
    args+=("$arg")
done
export PATH="${SILICON_HOME:-$HOME}/.silicon/bin:$PATH"
# ponytail: execfail keeps this shell alive after a failed exec, so the reason gets context.
shopt -s execfail
status=0
exec "$command" "${args[@]}" || status=$?
echo "Could not start $command inside the Silicon WSL2 distribution: exec exited $status (its error is above). PATH is $PATH" >&2
exit "$status"
