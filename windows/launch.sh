#!/bin/bash
set -euo pipefail
export HOME=/home/silicon SILICON_WSL=1
export PATH="$HOME/.local/share/silicon/bin:$HOME/.silicon/bin:$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
command=$1
windows_cwd=$2
shift 2
case "$command" in silicon|si|omnid|silicon-omni|omni|so|caddy|iam|honeycomb|spacestation|dm|briefcase|waveform|commit|remind|hook) ;; *) echo 'Unknown Silicon command' >&2; exit 2 ;; esac
if [ -n "${SILICON_HOME:-}" ]; then
    case "$SILICON_HOME" in /*) ;; *) echo 'Windows SILICON_HOME must name an absolute path inside the Silicon WSL2 distribution.' >&2; exit 2 ;; esac
    cd -- "$SILICON_HOME"
else
    # UNC paths under \\wsl.localhost\Silicon work too. Windows working directories
    # remain available for explicit data paths; configuration homes must be Linux FS.
    cwd=$(wslpath -u "$windows_cwd")
    cd -- "$cwd"
fi
homes=("$HOME" "${SILICON_HOME:-$HOME}" "${SILICON_INTERPRETER_HOME:-$HOME/.silicon-interpreter}")
while IFS= read -r name; do
    case "$name" in SILICON_*|OMNI_*|SPACE_STATION_*|HONEYCOMB_*|BRIEFCASE_*|DM_*|WAVEFORM_*|COMMIT_*|REMIND_*|HOOK_*|IAM_*)
        case "$name" in *_HOME|*_DIR) [ -z "${!name}" ] || homes+=("${!name}") ;; esac
    esac
done < <(compgen -e)
for home in "${homes[@]}"; do
    probe=$home
    while [ ! -e "$probe" ]; do probe=$(dirname -- "$probe"); done
    if [ "$(stat -f -c %T -- "$probe")" != ext2/ext3 ]; then
        echo 'Silicon configuration and credentials must live in the WSL2 Linux filesystem. Copy the project to /home/silicon and use /mnt/c for Windows data files.' >&2
        exit 2
    fi
done
args=()
path_operand=false
for arg do
    if [ "$command" = silicon ] && [ "$path_operand" = true ] && [ "$arg" != --json ] && [ "$arg" != -- ]; then
        case "$arg" in
            [A-Za-z]:\\*|[A-Za-z]:/*|\\\\wsl.localhost\\*|\\\\wsl\$\\*) arg=$(wslpath -u "$arg") ;;
            *\\*) arg=${arg//\\//} ;;
        esac
        path_operand=false
    elif [ "$command" = silicon ] && [ "${#args[@]}" -lt 2 ]; then
        case "$arg" in compile|connect|disconnect) path_operand=true ;; esac
    fi
    args+=("$arg")
done
exec "$HOME/.local/share/silicon/bin/$command" "${args[@]}"
