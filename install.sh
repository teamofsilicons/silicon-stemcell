#!/bin/sh
# Public installs use a complete binary bundle; no Rust toolchain is required.
# Source builds: SILICON_SOURCE_DIR=/checkout (or SILICON_GIT_REV=<40 hex SHA>).
# Offline source-build dependencies: SILICON_DEPENDENCY_BIN_DIR=/trusted/bin.
set -eu

fail() { printf 'silicon install: %s\n' "$*" >&2; exit 1; }
say() { printf 'silicon install: %s\n' "$*"; }
# Exit status 77: only a person can finish this installation (sudo for Caddy's port 80).
# The interpreter's automatic updater does not retry such a release until someone acts.
needs_person() { printf 'silicon install: %s\n' "$*" >&2; exit 77; }

# Service managers and cron start with a minimal PATH; getcap and setcap live in sbin.
case ":$PATH:" in *:/usr/sbin:*) ;; *) PATH="$PATH:/usr/sbin" ;; esac
case ":$PATH:" in *:/sbin:*) ;; *) PATH="$PATH:/sbin" ;; esac
export PATH

version=${SILICON_VERSION:-v6.1.0}
prefix=${SILICON_PREFIX:-"$HOME/.local/share/silicon"}
manage_path=false
if [ -z "${SILICON_PREFIX+x}" ] && [ "${SILICON_NO_PATH:-0}" != 1 ]; then manage_path=true; fi
repository=${SILICON_REPOSITORY:-teamofsilicons/silicon-stemcell}
source_dir=${SILICON_SOURCE_DIR:-}
git_rev=${SILICON_GIT_REV:-}
dependency_bins=${SILICON_DEPENDENCY_BIN_DIR:-}
omni_rev=62c2adc57983be37c1de2064d169073bddf71291
commands='silicon si omnid silicon-omni omni so caddy'
binaries="$commands"
notices='LICENSE LICENSES/README.md LICENSES/omni-LICENSE.txt LICENSES/caddy-LICENSE.txt LICENSES/caddy-AUTHORS.txt'

case "$version" in ''|*[!A-Za-z0-9._-]*) fail "SILICON_VERSION must be a release tag, without slashes, not '$version'" ;; esac
case "$repository" in ''|*[!A-Za-z0-9._/-]*) fail "invalid SILICON_REPOSITORY '$repository'" ;; esac
[ -z "$source_dir" ] || [ -z "$git_rev" ] || fail 'choose SILICON_SOURCE_DIR or SILICON_GIT_REV, not both'
if [ -n "$git_rev" ]; then
    [ "${#git_rev}" -eq 40 ] || fail "SILICON_GIT_REV must be an exact 40-character Git commit, not '$git_rev' (${#git_rev} characters)"
    case "$git_rev" in *[!0-9a-f]*) fail "SILICON_GIT_REV must be lowercase hexadecimal, not '$git_rev'" ;; esac
fi

system=$(uname -s)
case "$system/$(uname -m)" in
    Darwin/arm64) target=aarch64-apple-darwin; caddy_platform=mac_arm64; honeycomb_target=macos-aarch64 ;;
    Darwin/x86_64) target=x86_64-apple-darwin; caddy_platform=mac_amd64; honeycomb_target=macos-x86_64 ;;
    Linux/x86_64) target=x86_64-unknown-linux-gnu; caddy_platform=linux_amd64; honeycomb_target=linux-x86_64 ;;
    Linux/aarch64|Linux/arm64) target=aarch64-unknown-linux-gnu; caddy_platform=linux_arm64; honeycomb_target=linux-aarch64 ;;
    *) fail "supported systems are macOS and Linux on x86-64 or ARM64, not $system/$(uname -m)" ;;
esac

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

# A captured stream for an error: verbatim, or "(empty)".
stream() {
    if [ -n "$(tr -d ' \t\r\n' < "$1")" ]; then printf '\n%s' "$(cat "$1")"; else printf ' (empty)'; fi
}

# Run a command with both streams kept. Success replays them unchanged; failure
# leaves $report with the exact command, its exit status and both streams
# verbatim, the shape src/failure.rs gives every Silicon error.
attempt() {
    report=
    attempt_status=0
    "$@" >"$stage/stdout" 2>"$stage/stderr" || attempt_status=$?
    if [ "$attempt_status" -eq 0 ]; then
        [ ! -s "$stage/stderr" ] || cat "$stage/stderr" >&2
        [ ! -s "$stage/stdout" ] || cat "$stage/stdout"
        return 0
    fi
    report="\`$(command_line "$@")\` failed: exit status: $attempt_status
stderr:$(stream "$stage/stderr")
stdout:$(stream "$stage/stdout")"
    return "$attempt_status"
}

# attempt, stopping the install with what it was doing and the command's own words.
run() {
    purpose=$1
    shift
    attempt "$@" || fail "$purpose
$report"
}

# For long commands whose progress streams live: their own words are already on
# the terminal, so a failure adds the exact command and its exit status.
live() {
    purpose=$1
    shift
    live_status=0
    "$@" || live_status=$?
    [ "$live_status" -eq 0 ] || fail "$purpose
\`$(command_line "$@")\` failed: exit status: $live_status (its output is above)"
}

# Extract one archive member into a file. tar's stdout is the member itself, so a
# failure shows tar's own words and how much of the member arrived.
extract() {
    extract_status=0
    tar -xOzf "$2" "$3" >"$4" 2>"$stage/stderr" || extract_status=$?
    if [ "$extract_status" -eq 0 ]; then
        [ ! -s "$stage/stderr" ] || cat "$stage/stderr" >&2
        return 0
    fi
    fail "$1
\`$(command_line tar -xOzf "$2" "$3")\` failed: exit status: $extract_status
stderr:$(stream "$stage/stderr")
stdout: $(wc -c <"$4" | tr -d ' ') bytes written to $4"
}

hash_file() {
    if command -v "sha${1}sum" >/dev/null 2>&1; then
        digest=$(run "could not hash $2" "sha${1}sum" "$2") || exit 1
    elif command -v shasum >/dev/null 2>&1; then
        digest=$(run "could not hash $2" shasum -a "$1" "$2") || exit 1
    else
        fail "SHA-$1 verifier missing; install shasum or coreutils"
    fi
    printf '%s\n' "${digest%% *}"
}

# A stalled or trickling transfer fails instead of holding the install lock forever.
download() {
    command -v curl >/dev/null 2>&1 || fail 'curl is required'
    run "$3" curl --proto '=https' --tlsv1.2 --fail --location --silent --show-error \
        --connect-timeout 30 --speed-limit 1024 --speed-time 120 --max-time 3600 \
        --retry 3 --retry-delay 5 --output "$2" "$1"
}

verify() {
    expected=$(awk -v name="$3" '$2 == name {print $1}' "$2")
    case "$expected" in ''|*[!0-9a-fA-F]*) fail "missing or ambiguous checksum for $3; $2 says:$(stream "$2")" ;; esac
    [ "${#expected}" -eq "$(( $1 / 4 ))" ] || fail "invalid SHA-$1 checksum for $3: $expected has ${#expected} hex digits, not $(( $1 / 4 ))"
    actual=$(hash_file "$1" "$4")
    [ "$actual" = "$expected" ] || fail "checksum mismatch for $3: expected $expected, downloaded file has $actual; installation was not changed"
}

mkdir -p "$prefix" || fail "could not create SILICON_PREFIX $prefix (mkdir's error is above)"
resolved=$(CDPATH= cd -- "$prefix" && pwd -P) || fail "could not enter SILICON_PREFIX $prefix (the error is above)"
prefix=$resolved
[ "$prefix" != / ] || fail 'SILICON_PREFIX must be a dedicated prefix, not /'
runtime="$prefix/lib/silicon"
mkdir -p "$runtime/releases" "$prefix/bin" || fail "could not create $runtime/releases and $prefix/bin (mkdir's error is above)"
for binary in $commands; do
    link="$prefix/bin/$binary"
    if [ -e "$link" ] || [ -L "$link" ]; then
        [ -L "$link" ] && [ "$(readlink "$link")" = "../lib/silicon/current/bin/$binary" ] ||
            fail "$link is not managed by this installer; choose another SILICON_PREFIX
$(ls -ld "$link" 2>&1)"
    fi
done
[ ! -e "$runtime/current" ] || [ -L "$runtime/current" ] || fail "runtime current path is not a managed symlink
$(ls -ld "$runtime/current" 2>&1)"
stage=$(mktemp -d "$runtime/.install.XXXXXX") || fail "could not create a staging directory in $runtime (mktemp's error is above)"
lock="$runtime/.install-lock"
reclaim="$lock.reclaim"
new_links=''
activated=false
lock_owned=false
reclaiming=false
cleanup() {
    status=$?
    trap - 0 HUP INT TERM
    if [ "$activated" = false ]; then
        for binary in $new_links; do
            [ "$(readlink "$prefix/bin/$binary" 2>/dev/null || true)" != "../lib/silicon/current/bin/$binary" ] || rm -f "$prefix/bin/$binary"
        done
    fi
    rm -rf "$stage"
    [ "$lock_owned" = false ] || rm -rf "$lock"
    [ "$reclaiming" = false ] || rmdir "$reclaim" 2>/dev/null || true
    exit "$status"
}
trap cleanup 0
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# This boot, where the kernel names it: a lock from before a reboot is stale whatever
# process now has its number.
boot_id() {
    cat /proc/sys/kernel/random/boot_id 2>/dev/null || true
}

# Whether a process exists; another user's cannot be signalled but is listed by ps.
process_running() {
    kill -0 "$1" 2>/dev/null || ps -p "$1" >/dev/null 2>&1
}

# When a process started, which tells it apart from a later one given the same number.
# Linux counts clock ticks after boot, which setting the wall clock does not change;
# elsewhere ps prints the start in a fixed language and zone, so installers run by launchd
# and from a person's terminal agree. Fails when it cannot tell.
process_start() {
    if [ -r "/proc/$1/stat" ]; then
        process_stat=$(cat "/proc/$1/stat" 2>/dev/null) || return 1
        # Field 22. The command name (field 2) may hold spaces and parentheses, so the
        # fields are counted from after its closing one.
        # shellcheck disable=SC2086
        set -- ${process_stat##*") "}
        [ "$#" -ge 20 ] || return 1
        shift 19
        started=$1
    else
        started=$(LC_ALL=C TZ=UTC0 ps -o lstart= -p "$1" 2>/dev/null) || return 1
        # ps pads its column; single spaces compare the same whatever the padding.
        # shellcheck disable=SC2086
        set -- $started
        started=$*
    fi
    [ -n "$started" ] || return 1
    printf '%s\n' "$started"
}

# Whether $lock was left by an installer that can no longer be running: power loss,
# SIGKILL or a VM shutdown skip the trap that removes it. Sets $lock_reason either way.
# The owner is known by its process number and start, so a number that now belongs to
# another process is recognized whatever that process is called.
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
        # Older installers record nothing.
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
    # An owner that cannot be checked: no installer runs for two hours (unattended runs are
    # stopped after 30 minutes).
    if [ -n "$(find "$lock" -maxdepth 0 -mmin +120 2>/dev/null)" ]; then
        lock_reason="$unverified, and it is over two hours old"
        return 0
    fi
    lock_reason="$unverified, and it is under two hours old"
    return 1
}

# One installer per prefix. The lock records its owner, so one left behind by a killed
# installer is reclaimed instead of blocking every later update.
take_lock() {
    attempt mkdir "$lock" && return 0
    [ -d "$lock" ] || fail "could not create the installer lock $lock
$report"
    held=$report
    # Reclaimers take turns and judge the lock once alone: a lock another reclaimer has
    # just replaced is under a minute old, so two installers never both reclaim it.
    if ! mkdir "$reclaim" 2>/dev/null; then
        # A turn lasts moments; an older one was left by a reclaimer that was killed.
        if [ -n "$(find "$reclaim" -maxdepth 0 -mmin +1 2>/dev/null)" ] &&
            rmdir "$reclaim" 2>/dev/null && mkdir "$reclaim" 2>/dev/null; then
            say "removed $reclaim, left by an installer stopped while it reclaimed $lock"
        else
            fail "another installer is reclaiming $lock right now; if none is running, remove $reclaim and retry
$held"
        fi
    fi
    reclaiming=true
    if lock_is_stale; then
        say "removing a stale installer lock $lock: $lock_reason"
        run "could not remove the stale installer lock $lock" rm -rf "$lock"
        run "another installer took $lock while its stale copy was removed" mkdir "$lock"
        lock_owned=true
    fi
    rmdir "$reclaim" 2>/dev/null || true
    reclaiming=false
    [ "$lock_owned" = true ] || fail "another installer may hold $lock ($lock_reason); if none is running, remove that directory and retry
$held"
}
take_lock
lock_owned=true
# The owner: its start and this boot, then its number. A lock whose record is incomplete
# is reclaimed only after two hours.
own_start=$(process_start "$$") || own_start=
[ -z "$own_start" ] || printf '%s\n' "$own_start" > "$lock/start" || true
boot=$(boot_id)
[ -z "$boot" ] || printf '%s\n' "$boot" > "$lock/boot" || true
if ! printf '%s\n' "$$" > "$lock/pid"; then
    say "could not record this installer's process in $lock/pid (the error is above); a lock left by a crash is reclaimed after two hours"
fi
mkdir -p "$stage/payload/bin" "$stage/payload/LICENSES"

if [ -n "$git_rev" ]; then
    command -v git >/dev/null 2>&1 || fail 'Git is required for source builds'
    source_dir="$stage/source"
    run 'could not create the source checkout' git init -q "$source_dir"
    run 'could not fetch the requested interpreter commit' git -C "$source_dir" fetch -q --depth 1 "https://github.com/$repository.git" "$git_rev"
    run 'could not check out the requested interpreter commit' git -C "$source_dir" checkout -q --detach FETCH_HEAD
    head=$(run 'could not read the checked-out source revision' git -C "$source_dir" rev-parse HEAD)
    [ "$head" = "$git_rev" ] || fail "source Git revision did not match: checked out $head, requested $git_rev"
fi

copy_dependency() {
    [ -n "$dependency_bins" ] && [ -f "$dependency_bins/$1" ] && [ -x "$dependency_bins/$1" ] || return 1
    run "could not copy trusted dependency $1" cp "$dependency_bins/$1" "$stage/payload/bin/$1"
}

configure_caddy_port() {
    [ "$system" = Linux ] || return 0
    port_start=$(cat /proc/sys/net/ipv4/ip_unprivileged_port_start 2>/dev/null) || port_start=1024
    case "$port_start" in ''|*[!0-9]*) fail "could not determine Linux privileged port permissions: /proc/sys/net/ipv4/ip_unprivileged_port_start says '$port_start'" ;; esac
    [ "$port_start" -gt 80 ] || return 0
    command -v getcap >/dev/null 2>&1 || needs_person "port 80 requires getcap/setcap; install the Linux libcap tools first (searched PATH=$PATH)"
    caddy="$stage/payload/bin/caddy"
    current_caddy="$runtime/current/bin/caddy"
    if [ -f "$current_caddy" ] && caddy_has_capability "$current_caddy"; then
        # Hashed outside any condition, so a failed hash stops the install instead
        # of comparing two empty digests.
        current_hash=$(hash_file 256 "$current_caddy")
        staged_hash=$(hash_file 256 "$caddy")
        # An unchanged inode retains its capability without another sudo prompt.
        if [ "$current_hash" = "$staged_hash" ]; then
            if attempt ln "$current_caddy" "$stage/payload/caddy.capable"; then
                run 'could not reuse the permitted Caddy' mv -f "$stage/payload/caddy.capable" "$caddy"
                return 0
            fi
            say "could not reuse the permitted Caddy, so its permission is granted again
$report"
        fi
    fi
    setcap_path=$(command -v setcap) || needs_person "port 80 requires setcap; install the Linux libcap tools first (searched PATH=$PATH)"
    say 'granting Caddy permission to bind local port 80'
    report=
    if [ "$(id -u)" = 0 ]; then
        run 'could not grant Caddy port 80 permission' "$setcap_path" cap_net_bind_service=ep "$caddy"
    elif command -v sudo >/dev/null 2>&1 && attempt sudo -n "$setcap_path" cap_net_bind_service=ep "$caddy"; then
        :
    elif [ "${SILICON_NONINTERACTIVE:-0}" != 1 ] && command -v sudo >/dev/null 2>&1 &&
        ( : </dev/tty ) >/dev/null 2>&1; then
        live 'could not grant Caddy port 80 permission' sudo "$setcap_path" cap_net_bind_service=ep "$caddy" </dev/tty
    else
        # ponytail: the non-interactive sudo attempt's own words, when there was one.
        needs_person "Caddy needs port 80 permission; rerun this installation in a terminal with sudo access. The active release was not changed${report:+
$report}"
    fi
    caddy_has_capability "$caddy" || fail "Caddy port 80 permission was not applied
$report"
}

# getcap's answer stays in $report so a failed grant can show it.
caddy_has_capability() {
    attempt getcap "$1" >"$stage/capability" || return 1
    case "$(cat "$stage/capability")" in *' cap_net_bind_service=ep') return 0 ;; esac
    report="\`$(command_line getcap "$1")\` does not list cap_net_bind_service=ep; it printed:$(stream "$stage/capability")"
    return 1
}

if [ -n "$source_dir" ]; then
    command -v cargo >/dev/null 2>&1 || fail 'source builds require Rust 1.98+ and a C compiler; use a released binary bundle otherwise'
    [ -f "$source_dir/Cargo.toml" ] || fail 'SILICON_SOURCE_DIR must contain Cargo.toml'
    # Reuse compilation across the required crates, then remove staged build files.
    CARGO_TARGET_DIR=${CARGO_TARGET_DIR:-"$stage/build-target"}
    export CARGO_TARGET_DIR
    source_dir=$(CDPATH= cd -- "$source_dir" && pwd -P)
    say "building interpreter from $source_dir"
    live 'interpreter source build failed' cargo install --locked --force --root "$stage/payload" --path "$source_dir" --bin silicon --bin si
    for binary in omnid silicon-omni omni so; do
        if ! copy_dependency "$binary"; then
            case "$binary" in omnid) package=omni-daemon ;; *) package=silicon-omni-cli ;; esac
            live "required Omni binary $binary could not be built at $omni_rev" \
                cargo install --locked --force --root "$stage/payload" --git https://github.com/teamofsilicons/silicon-omni --rev "$omni_rev" --bin "$binary" "$package"
        fi
    done
    if ! copy_dependency caddy; then
        caddy_asset="caddy_2.11.4_${caddy_platform}.tar.gz"
        caddy_url=https://github.com/caddyserver/caddy/releases/download/v2.11.4
        download "$caddy_url/$caddy_asset" "$stage/caddy.tar.gz" 'could not download required Caddy 2.11.4'
        download "$caddy_url/caddy_2.11.4_checksums.txt" "$stage/caddy-checksums" 'could not download Caddy checksums'
        # Upstream Caddy's checksum file uses SHA-512.
        verify 512 "$stage/caddy-checksums" "$caddy_asset" "$stage/caddy.tar.gz"
        extract "could not extract caddy from $caddy_asset" "$stage/caddy.tar.gz" caddy "$stage/payload/bin/caddy"
    fi
    printf '%s\n' "$version" > "$stage/payload/VERSION"
    run 'source checkout has no installer' cp "$source_dir/install.sh" "$stage/payload/installer.sh"
    for notice in $notices; do
        run "source checkout is missing $notice" cp "$source_dir/$notice" "$stage/payload/$notice"
    done
    rm -f "$stage/payload/.crates.toml" "$stage/payload/.crates2.json"
else
    asset="silicon-$target.tar.gz"
    release_url=${SILICON_RELEASE_BASE_URL:-"https://github.com/$repository/releases/download/$version"}
    say "downloading $version for $target"
    download "$release_url/$asset" "$stage/bundle.tar.gz" "complete bundle $version/$asset is unavailable; legacy Stemcell releases cannot install the Rust interpreter"
    download "$release_url/SHA256SUMS" "$stage/checksums" 'release checksum file is unavailable'
    verify 256 "$stage/checksums" "$asset" "$stage/bundle.tar.gz"
    # Stream only known members into fixed paths; archive symlinks and ../ names
    # can never redirect extraction into a Silicon's configuration or other files.
    for binary in $binaries; do
        extract "could not extract required binary bin/$binary from $asset" "$stage/bundle.tar.gz" "bin/$binary" "$stage/payload/bin/$binary"
    done
    extract "could not extract the version marker VERSION from $asset" "$stage/bundle.tar.gz" VERSION "$stage/payload/VERSION"
    extract "could not extract installer.sh from $asset" "$stage/bundle.tar.gz" installer.sh "$stage/payload/installer.sh"
    for notice in $notices; do
        extract "could not extract required notice $notice from $asset" "$stage/bundle.tar.gz" "$notice" "$stage/payload/$notice"
    done
    release_version=$(cat "$stage/payload/VERSION")
    [ "$release_version" = "$version" ] || fail "release version marker says '$release_version', not the requested $version"
fi

for binary in $binaries; do
    [ -s "$stage/payload/bin/$binary" ] && [ ! -L "$stage/payload/bin/$binary" ] || fail "required binary $binary is missing, empty or a link
$(ls -ld "$stage/payload/bin/$binary" 2>&1)"
    chmod 755 "$stage/payload/bin/$binary"
done
[ -s "$stage/payload/installer.sh" ] || fail 'release installer is empty'
for notice in $notices; do
    [ -s "$stage/payload/$notice" ] || fail "release notice $notice is empty"
done
run 'interpreter binary cannot run on this system' "$stage/payload/bin/silicon" --version >/dev/null
run 'Omni daemon binary cannot run on this system' "$stage/payload/bin/omnid" --version >/dev/null
printf '%s\n' "$prefix" > "$stage/payload/PREFIX"
configure_caddy_port

# Install the latest standalone Honeycomb without changing its settings or services.
# Its ordinary CLI update checks remain enabled; app binaries stay outside the bundle.
honeycomb_asset="honeycomb-$honeycomb_target.tar.gz"
honeycomb_url=https://github.com/teamofsilicons/silicon-honeycomb/releases/latest/download
download "$honeycomb_url/$honeycomb_asset" "$stage/honeycomb.tar.gz" 'could not download the latest Honeycomb'
download "$honeycomb_url/$honeycomb_asset.sha256" "$stage/honeycomb.sha256" 'could not download the latest Honeycomb checksum'
verify 256 "$stage/honeycomb.sha256" "$honeycomb_asset" "$stage/honeycomb.tar.gz"
extract "could not extract honeycomb from $honeycomb_asset" "$stage/honeycomb.tar.gz" honeycomb "$stage/honeycomb"
[ -s "$stage/honeycomb" ] || fail 'Honeycomb archive holds an empty honeycomb executable'
chmod 755 "$stage/honeycomb"
run 'Honeycomb binary cannot run on this system' "$stage/honeycomb" --version >/dev/null
honeycomb="$prefix/.honeycomb/dir/system/bin/honeycomb"
run "could not install Honeycomb at $honeycomb" mkdir -p "$(dirname "$honeycomb")"
run "could not install Honeycomb at $honeycomb" cp "$stage/honeycomb" "$honeycomb.new.$$"
run "could not install Honeycomb at $honeycomb" mv -f "$honeycomb.new.$$" "$honeycomb"
link="$prefix/bin/honeycomb"
if [ -e "$link" ] || [ -L "$link" ]; then
    [ -L "$link" ] && { [ "$(readlink "$link")" = '../lib/silicon/current/bin/honeycomb' ] || [ "$(readlink "$link")" = "$honeycomb" ]; } || fail "unmanaged executable at $link
$(ls -ld "$link" 2>&1)"
    run "could not replace the Honeycomb link $link" rm "$link"
fi
run "could not link $link to Honeycomb" ln -s "$honeycomb" "$link"

release_name="$version-${stage##*.}"
run "could not store release $release_name; the active release was not changed" mv "$stage/payload" "$runtime/releases/$release_name"
for binary in $commands; do
    if [ ! -L "$prefix/bin/$binary" ]; then
        run "could not link $prefix/bin/$binary; the active release was not changed" ln -s "../lib/silicon/current/bin/$binary" "$prefix/bin/$binary"
        new_links="$new_links $binary"
    fi
done
previous=$(readlink "$runtime/current" 2>/dev/null) || previous=
run "could not prepare the switch to $release_name; the active release was not changed" ln -s "releases/$release_name" "$stage/current"
case "$system" in
    Darwin) run "could not activate $release_name; the active release was not changed" mv -fh "$stage/current" "$runtime/current" ;;
    Linux) run "could not activate $release_name; the active release was not changed" mv -fT "$stage/current" "$runtime/current" ;;
esac
activated=true
# Remove only links owned by older bundles. Honeycomb/app installations are untouched.
for app in iam spacestation dm briefcase waveform commit remind hook; do
    link="$prefix/bin/$app"
    if [ -L "$link" ] && [ "$(readlink "$link")" = "../lib/silicon/current/bin/$app" ]; then
        rm "$link"
    fi
done

# Every update stores a full release; nothing needs the old ones. Kept: the active
# release, the one it replaced (to roll back to), the one the updating interpreter runs
# ($SILICON_KEEP_RELEASE) and any a running interpreter recorded in $runtime/running.
# A removal that fails is a warning: the new release is already active.
prune_releases() {
    active=$(readlink "$runtime/current" 2>/dev/null) || active="releases/$release_name"
    keep=" ${active##*/} ${previous##*/} "
    if [ -n "${SILICON_KEEP_RELEASE:-}" ]; then
        kept_release=${SILICON_KEEP_RELEASE%/}
        keep="$keep${kept_release##*/} "
    fi
    for record in "$runtime/running"/*; do
        [ -f "$record" ] || continue
        record_pid=${record##*/}
        case "$record_pid" in ''|*[!0-9]*) continue ;; esac
        if process_running "$record_pid"; then
            in_use=$(cat "$record" 2>/dev/null) || in_use=
            keep="$keep${in_use##*/} "
        else
            rm -f "$record" 2>/dev/null || true
        fi
    done
    for release in "$runtime/releases"/*; do
        [ -e "$release" ] || [ -L "$release" ] || continue
        case "$keep" in *" ${release##*/} "*) continue ;; esac
        if attempt rm -rf "$release"; then
            say "removed old release ${release##*/}"
        else
            say "warning: could not remove old release $release; $release_name is active regardless
$report"
        fi
    done
    # Staging directories of installers that were killed; this one holds the lock.
    for old_stage in "$runtime"/.install.*; do
        [ -d "$old_stage" ] && [ "$old_stage" != "$stage" ] || continue
        [ -n "$(find "$old_stage" -maxdepth 0 -mmin +60 2>/dev/null)" ] || continue
        if attempt rm -rf "$old_stage"; then
            say "removed the staging directory $old_stage of an installer that did not finish"
        else
            say "warning: could not remove the staging directory $old_stage
$report"
        fi
    done
}
prune_releases
say "installed $version runtime in $prefix/bin"
quoted_bin=$(printf '%s' "$prefix/bin" | sed "s/'/'\\\\''/g")
path_line="export PATH='$quoted_bin':\"\$PATH\""
if [ "$manage_path" = true ]; then
    user_shell=${SHELL:-}
    case "${user_shell##*/}" in
        zsh) profile="$HOME/.zshrc" ;;
        bash) profile="$HOME/.bashrc" ;;
        *) profile="$HOME/.profile" ;;
    esac
    if ! grep -Fqx "$path_line" "$profile" 2>/dev/null; then
        if printf '\n# Silicon CLI\n%s\n' "$path_line" >> "$profile"; then
            say "added Silicon to $profile; new shells will find the commands"
        else
            say "could not update $profile (the shell's error is above); use the PATH command below"
        fi
    fi
fi
case ":$PATH:" in
    *":$prefix/bin:"*) ;;
    *) printf 'For this terminal, run: %s\n' "$path_line" ;;
esac
