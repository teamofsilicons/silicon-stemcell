#!/bin/sh
set -eu
payload=$1
expected=$2
version=$3
windows_powershell=$4
windows_opener=$5
# task: the Windows logon task supervises the interpreter; none: install.ps1 -NoService.
service=${6:-task}
# The task's name, one per Windows user: `Silicon Interpreter <user SID>` in the root folder.
task_name=${7:-}
fail() { printf 'Silicon WSL provisioning: %s\n' "$*" >&2; exit 2; }
# Shell-quoted argv, so an error names exactly what ran.
command_line() {
    line=
    for word do
        case "$word" in
            ''|*[!A-Za-z0-9_./:=@%+,-]*) word="'$(printf '%s' "$word" | sed "s/'/'\\\\''/g")'" ;;
        esac
        line="${line:+$line }$word"
    done
    printf '%s' "$line"
}
# Run a step whose own output streams live; a failure adds what provisioning was
# doing, the exact command and its exit status, and keeps that status.
run() {
    purpose=$1
    shift
    status=0
    "$@" || status=$?
    [ "$status" -ne 0 ] || return 0
    printf 'Silicon WSL provisioning: %s\n`%s` failed: exit status: %s (its output is above)\n' "$purpose" "$(command_line "$@")" "$status" >&2
    exit "$status"
}
case "$version" in v[0-9]*.[0-9]*.[0-9]*) ;; *) fail "invalid Silicon version '$version'" ;; esac
case "$version" in *[!a-zA-Z0-9._-]*) fail "Silicon version '$version' may use only letters, digits, '.', '_' and '-'" ;; esac
case "$service" in task|none) ;; *) fail "invalid service mode '$service'; expected task or none" ;; esac
# Written into JSON below, so only the shape install.ps1 builds: the prefix and a SID.
case "$task_name" in "Silicon Interpreter S-"*) ;; *) fail "invalid logon task name '$task_name'; expected 'Silicon Interpreter <user SID>'" ;; esac
case "${task_name#Silicon Interpreter }" in *[!S0-9-]*) fail "invalid logon task name '$task_name'; the user SID may hold only S, digits and '-'" ;; esac
actual=$(run 'could not hash the Linux bundle' sha256sum "$payload/runtime.tar.gz")
actual=${actual%% *}
[ "$actual" = "$expected" ] || fail "Linux bundle checksum mismatch: expected $expected, $payload/runtime.tar.gz has $actual"
if ! id silicon >/dev/null 2>&1; then run 'could not create the silicon user' useradd --create-home --shell /bin/bash silicon; fi
chmod 700 /home/silicon
export DEBIAN_FRONTEND=noninteractive
run 'could not refresh Ubuntu package lists' apt-get update -qq
# The verified rootfs is an immutable base image; apply current Ubuntu security
# updates before running applications, while preserving existing local config.
run 'could not apply Ubuntu updates' apt-get upgrade -y -qq --with-new-pkgs -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold
run 'could not install Silicon dependencies' apt-get install -y -qq ca-certificates curl libcap2-bin python3 git
prefix=/home/silicon/.local/share/silicon
runtime="$prefix/lib/silicon"
mkdir -p "$runtime/releases" "$prefix/bin" /opt/silicon
release="$runtime/releases/$version-$expected"
# One installer per prefix, taken, recorded and reclaimed the way install.sh does it, so
# each reclaims a lock the other left behind: `wsl --shutdown`, a Windows restart or
# SIGKILL skip the trap that removes it. The interpreter's release pruning and install.sh's
# own reclaim both leave this staging and activation alone while it is held.
lock="$runtime/.install-lock"
reclaim="$lock.reclaim"
lock_owned=false
reclaiming=false
stage=
next=
release_lock() {
    [ "$reclaiming" = false ] || rmdir "$reclaim" 2>/dev/null || true
    [ "$lock_owned" = false ] || rm -rf "$lock"
    [ -z "$stage" ] || rm -rf "$stage"
    [ -z "$next" ] || rm -f "$next"
}
trap release_lock EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
say() { printf 'Silicon WSL provisioning: %s\n' "$*" >&2; }
# This boot: a lock recorded in another one is stale whatever process now has its number.
boot_id() {
    cat /proc/sys/kernel/random/boot_id 2>/dev/null || true
}
process_running() {
    kill -0 "$1" 2>/dev/null || ps -p "$1" >/dev/null 2>&1
}
# Field 22 of /proc/PID/stat (clock ticks after boot), counted after the command name,
# which may hold spaces and parentheses. Fails when it cannot tell.
process_start() {
    process_stat=$(cat "/proc/$1/stat" 2>/dev/null) || return 1
    # shellcheck disable=SC2086
    set -- ${process_stat##*") "}
    [ "$#" -ge 20 ] || return 1
    shift 19
    [ -n "$1" ] || return 1
    printf '%s\n' "$1"
}
# install.sh's lock_is_stale: whether $lock was left by an installer that can no longer be
# running. Sets $lock_reason either way.
lock_is_stale() {
    lock_pid=$(cat "$lock/pid" 2>/dev/null) || lock_pid=
    lock_start=$(cat "$lock/start" 2>/dev/null) || lock_start=
    lock_boot=$(cat "$lock/boot" 2>/dev/null) || lock_boot=
    # Its owner records itself just after taking it; a new lock is never judged.
    if [ -z "$(find "$lock" -maxdepth 0 -mmin +1 2>/dev/null)" ]; then
        lock_reason="it was taken less than a minute ago"
        return 1
    fi
    case "$lock_pid" in ''|*[!0-9]*) lock_pid= ;; esac
    if [ -z "$lock_pid" ]; then
        unverified="it names no installer process"
    elif [ -n "$lock_boot" ] && [ "$lock_boot" != "$(boot_id)" ]; then
        lock_reason="installer process $lock_pid ran before this machine last started"
        return 0
    elif ! process_running "$lock_pid"; then
        lock_reason="installer process $lock_pid is not running"
        return 0
    elif [ -z "$lock_start" ]; then
        unverified="it does not record when installer process $lock_pid started"
    elif running_start=$(process_start "$lock_pid"); then
        if [ "$running_start" = "$lock_start" ]; then
            lock_reason="installer process $lock_pid is running"
            return 1
        fi
        lock_reason="process $lock_pid is not the installer that took it: that one started at $lock_start, this one at $running_start"
        return 0
    else
        unverified="the start of process $lock_pid cannot be read"
    fi
    if [ -n "$(find "$lock" -maxdepth 0 -mmin +120 2>/dev/null)" ]; then
        lock_reason="$unverified, and it is over two hours old"
        return 0
    fi
    lock_reason="$unverified, and it is under two hours old"
    return 1
}
# Take $lock; a stale one is removed (as root, whoever left it) and taken once more.
# Reclaimers take turns through $reclaim, as install.sh's do, so two never both reclaim it.
take_lock() {
    if made=$(mkdir "$lock" 2>&1); then
        lock_owned=true
        return 0
    fi
    [ -d "$lock" ] || fail "could not create the installer lock $lock: $made"
    held=$(ls -ld "$lock" "$lock"/* 2>&1) || true
    if ! mkdir "$reclaim" 2>/dev/null; then
        # A turn lasts moments; an older one was left by a reclaimer that was stopped.
        if [ -n "$(find "$reclaim" -maxdepth 0 -mmin +1 2>/dev/null)" ] &&
            rmdir "$reclaim" 2>/dev/null && mkdir "$reclaim" 2>/dev/null; then
            say "removed $reclaim, left by an installer stopped while it reclaimed $lock"
        else
            fail "another installer is reclaiming $lock right now; if none is running, remove $reclaim and rerun the Windows installer
$held"
        fi
    fi
    reclaiming=true
    if lock_is_stale; then
        say "removing a stale installer lock $lock, by the rules install.sh uses: $lock_reason"
        run "could not remove the stale installer lock $lock" rm -rf "$lock"
        run "another installer took $lock while its stale copy was removed" mkdir "$lock"
        lock_owned=true
    fi
    rmdir "$reclaim" 2>/dev/null || true
    reclaiming=false
    [ "$lock_owned" = true ] || fail "another installer holds $lock ($lock_reason); if none is running, remove that directory and rerun the Windows installer
$held"
}
# Its start and this boot, then its number, as install.sh records them.
record_owner() {
    own_start=$(process_start "$$") || own_start=
    [ -z "$own_start" ] || printf '%s\n' "$own_start" > "$lock/start"
    boot=$(boot_id)
    [ -z "$boot" ] || printf '%s\n' "$boot" > "$lock/boot"
    printf '%s\n' "$$" > "$lock/pid"
}
take_lock
# The in-WSL updater runs install.sh as silicon, which cannot remove a directory only root
# may write: handed over right after it is made, so a lock left here is reclaimable there.
run "could not hand the installer lock $lock to the silicon user" chown silicon:silicon "$lock"
record_owner
run "could not hand the installer lock $lock to the silicon user" chown -R silicon:silicon "$lock"
if [ ! -f "$release/VERSION" ]; then
    stage=$(mktemp -d "$runtime/releases/.install.XXXXXX")
    # Validate every entry before extraction, including the fixed inventory from
    # install.sh. Never let an archive symlink redirect a privileged extraction.
    run 'could not unpack the Linux bundle' python3 - "$payload/runtime.tar.gz" "$stage" <<'PY'
import pathlib, sys, tarfile
commands = 'silicon si omnid silicon-omni omni so caddy'.split()
notices = 'README.md omni-LICENSE.txt caddy-LICENSE.txt caddy-AUTHORS.txt'.split()
expected = {'VERSION', 'installer.sh', 'LICENSE'} | {'bin/' + name for name in commands} | {'LICENSES/' + name for name in notices}
with tarfile.open(sys.argv[1]) as archive:
    files = [member for member in archive.getmembers() if not member.isdir()]
    names = [m.name for m in files]
    missing, unexpected = sorted(expected - set(names)), sorted(set(names) - expected)
    duplicated = sorted({name for name in names if names.count(name) > 1})
    # SystemExit, not assert: python3 -O would drop these checks.
    if missing or unexpected or duplicated:
        raise SystemExit(f'Linux bundle inventory mismatch: missing {missing}, unexpected {unexpected}, duplicated {duplicated}')
    special = [f'{m.name} (tar type {m.type!r})' for m in files if not m.isfile()]
    if special:
        raise SystemExit(f'Linux bundle contains a link or special file: {special}')
    for member in files:
        path = pathlib.Path(sys.argv[2]) / member.name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(archive.extractfile(member).read())
        path.chmod(0o755 if member.name.startswith('bin/') else 0o644)
PY
    staged_version=$(cat "$stage/VERSION")
    [ "$staged_version" = "$version" ] || fail "Linux bundle VERSION says '$staged_version', not $version"
    printf '%s\n' "$prefix" > "$stage/PREFIX"
    chown -R silicon:silicon "$stage"
    run 'the Linux interpreter cannot run in WSL' runuser -u silicon -- env HOME=/home/silicon SILICON_TELEMETRY=0 SILICON_AUTO_UPDATE=0 "$stage/bin/silicon" --version
    run 'could not grant Caddy port 80 permission' /usr/sbin/setcap cap_net_bind_service=ep "$stage/bin/caddy"
    chmod -R go-w "$stage"
    mv "$stage" "$release"
    stage=
fi
for binary in silicon si omnid silicon-omni omni so caddy; do
    path="$prefix/bin/$binary"
    { [ ! -e "$path" ] && [ ! -L "$path" ]; } || [ "$(readlink "$path")" = "../lib/silicon/current/bin/$binary" ] || fail "unmanaged executable at $path
$(ls -ld "$path" 2>&1)"
    [ -L "$path" ] || ln -s "../lib/silicon/current/bin/$binary" "$path"
done
chown silicon:silicon /home/silicon/.local /home/silicon/.local/share "$prefix" "$prefix/bin" "$prefix/lib" "$runtime" "$runtime/releases"
next="$runtime/current.new.$$"
ln -s "releases/$version-$expected" "$next"
chown -h silicon:silicon "$next"
run "could not activate Silicon $version; the active release was not changed" mv -fT "$next" "$runtime/current"
next=
release_lock
trap - EXIT HUP INT TERM
# Install latest Honeycomb independently; leave its app-update settings and services alone.
bootstrap=$(mktemp -d)
trap 'rm -rf "$bootstrap"' EXIT HUP INT TERM
machine=$(uname -m)
case "$machine" in x86_64) honeycomb_arch=x86_64 ;; aarch64|arm64) honeycomb_arch=aarch64 ;; *) fail "Honeycomb has no Linux build for architecture $machine" ;; esac
honeycomb_asset="honeycomb-linux-$honeycomb_arch.tar.gz"
honeycomb_url=https://github.com/teamofsilicons/silicon-honeycomb/releases/latest/download
# A stalled transfer fails instead of holding provisioning forever.
curl_limits='--connect-timeout 30 --speed-limit 1024 --speed-time 120 --max-time 3600 --retry 3 --retry-delay 5'
# shellcheck disable=SC2086
run 'could not download the latest Honeycomb' curl --proto '=https' --tlsv1.2 -fsSL $curl_limits "$honeycomb_url/$honeycomb_asset" -o "$bootstrap/$honeycomb_asset"
# shellcheck disable=SC2086
run 'could not download the latest Honeycomb checksum' curl --proto '=https' --tlsv1.2 -fsSL $curl_limits "$honeycomb_url/$honeycomb_asset.sha256" -o "$bootstrap/checksum"
expected_honeycomb=$(awk -v name="$honeycomb_asset" '$2 == name {print $1}' "$bootstrap/checksum")
actual_honeycomb=$(run 'could not hash the Honeycomb download' sha256sum "$bootstrap/$honeycomb_asset")
actual_honeycomb=${actual_honeycomb%% *}
[ "$actual_honeycomb" = "$expected_honeycomb" ] || fail "Honeycomb checksum mismatch: $honeycomb_asset.sha256 lists '$expected_honeycomb', the download has $actual_honeycomb. The checksum file says:
$(cat "$bootstrap/checksum")"
run 'Honeycomb archive has no executable' tar -xOzf "$bootstrap/$honeycomb_asset" honeycomb > "$bootstrap/honeycomb"
[ -s "$bootstrap/honeycomb" ] || fail 'Honeycomb archive holds an empty honeycomb executable'
honeycomb="$prefix/.honeycomb/dir/system/bin/honeycomb"
mkdir -p "$(dirname "$honeycomb")"
chown silicon:silicon "$prefix/.honeycomb" "$prefix/.honeycomb/dir" "$prefix/.honeycomb/dir/system" "$prefix/.honeycomb/dir/system/bin"
install -o silicon -g silicon -m 755 "$bootstrap/honeycomb" "$honeycomb.new.$$"
run 'the latest Honeycomb cannot run in WSL' runuser -u silicon -- "$honeycomb.new.$$" --version
mv -f "$honeycomb.new.$$" "$honeycomb"
rm -rf "$bootstrap"
trap - EXIT HUP INT TERM
link="$prefix/bin/honeycomb"
if [ -e "$link" ] || [ -L "$link" ]; then
    [ -L "$link" ] && { [ "$(readlink "$link")" = '../lib/silicon/current/bin/honeycomb' ] || [ "$(readlink "$link")" = "$honeycomb" ]; } || fail "unmanaged executable at $link
$(ls -ld "$link" 2>&1)"
    rm "$link"
fi
ln -s "$honeycomb" "$link"
for app in iam spacestation dm briefcase waveform commit remind hook; do
    path="$prefix/bin/$app"
    if [ -L "$path" ] && [ "$(readlink "$path")" = "../lib/silicon/current/bin/$app" ]; then rm "$path"; fi
done
install -m 755 "$payload/launch.sh" /opt/silicon/launch
printf '%s\n' "$windows_powershell" > /opt/silicon/windows-powershell
printf '%s\n' "$windows_opener" > /opt/silicon/windows-opener
cat > /usr/local/bin/xdg-open <<'BROWSER'
#!/bin/sh
set -eu
[ "$#" = 1 ] || { printf 'xdg-open: expected exactly one URL, got %s arguments: %s\n' "$#" "$*" >&2; exit 2; }
exec "$(cat /opt/silicon/windows-powershell)" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$(cat /opt/silicon/windows-opener)" -Url "$1"
BROWSER
chmod 755 /usr/local/bin/xdg-open
# Keep Ubuntu's binfmt service aware of WSL's native PE interpreter. Without
# this registration, a package upgrade/service stop can remove VM-wide interop.
mkdir -p /etc/binfmt.d
if [ ! -e /etc/binfmt.d/WSLInterop.conf ]; then
    printf ':WSLInterop:M::MZ::/init:FP\n' > /etc/binfmt.d/WSLInterop.conf
fi
printf '[user]\ndefault=silicon\n[interop]\nenabled=true\nappendWindowsPath=false\n' > /etc/wsl.conf
printf 'export PATH="$HOME/.local/share/silicon/bin:$HOME/.silicon/bin:$HOME/.local/bin:$PATH"\nexport SILICON_WSL=1\n' > /etc/profile.d/silicon.sh
# Tell `silicon` inside the distribution that the Windows logon task supervises the
# interpreter, so connect waits for it and never installs a second supervisor here.
# `none` records that autostart was declined (install.ps1 -NoService).
interpreter=/home/silicon/.silicon-interpreter
run 'could not create the interpreter directory' install -d -o silicon -g silicon -m 700 "$interpreter"
marker="$interpreter/service.json"
# The label is the task's full path, `\Silicon Interpreter <SID>` (a JSON-escaped backslash).
case "$service" in
    task) printf '{"mechanism":"windows-task","label":"\\\\%s"}\n' "$task_name" ;;
    none) printf '{"mechanism":"windows-task","label":"\\\\%s","declined":true}\n' "$task_name" ;;
esac > "$marker.new.$$"
chown silicon:silicon "$marker.new.$$"
chmod 600 "$marker.new.$$"
run 'could not record the Windows logon task for the interpreter' mv -f "$marker.new.$$" "$marker"
printf '%s\n' "$version" > /opt/silicon/windows-version
printf 'Silicon %s installed. Project home: /home/silicon; Windows files: /mnt/c.\n' "$version"
