#!/bin/sh
# Public installs use a complete binary bundle; no Rust toolchain is required.
# Source builds: SILICON_SOURCE_DIR=/checkout (or SILICON_GIT_REV=<40 hex SHA>).
# Offline source-build dependencies: SILICON_DEPENDENCY_BIN_DIR=/trusted/bin.
set -eu

fail() { printf 'silicon install: %s\n' "$*" >&2; exit 1; }
say() { printf 'silicon install: %s\n' "$*"; }

version=${SILICON_VERSION:-v3.5.1}
prefix=${SILICON_PREFIX:-"$HOME/.local/share/silicon"}
manage_path=false
if [ -z "${SILICON_PREFIX+x}" ] && [ "${SILICON_NO_PATH:-0}" != 1 ]; then manage_path=true; fi
repository=${SILICON_REPOSITORY:-teamofsilicons/silicon-stemcell}
source_dir=${SILICON_SOURCE_DIR:-}
git_rev=${SILICON_GIT_REV:-}
dependency_bins=${SILICON_DEPENDENCY_BIN_DIR:-}
omni_rev=d52f5416cd33b363554d2300b5603dc0b6c43545
commit_rev=3fe18128282bf65c1f62595ed01e65ec467dba28
commands='silicon si omnid silicon-omni omni so caddy iam dm briefcase waveform commit remind hook'
binaries="$commands commit-native remind-native"
notices='LICENSE LICENSES/README.md LICENSES/iam-LICENSE.txt LICENSES/dm-NOTICE.txt LICENSES/briefcase-LICENSE.txt LICENSES/waveform-LICENSE.txt LICENSES/commit-NOTICE.txt LICENSES/remind-LICENSE.txt LICENSES/hook-NOTICE.txt LICENSES/omni-LICENSE.txt LICENSES/caddy-LICENSE.txt LICENSES/caddy-AUTHORS.txt'

case "$version" in ''|*[!A-Za-z0-9._-]*) fail 'SILICON_VERSION must be a release tag, without slashes' ;; esac
case "$repository" in ''|*[!A-Za-z0-9._/-]*) fail 'invalid SILICON_REPOSITORY' ;; esac
[ -z "$source_dir" ] || [ -z "$git_rev" ] || fail 'choose SILICON_SOURCE_DIR or SILICON_GIT_REV, not both'
if [ -n "$git_rev" ]; then
    [ "${#git_rev}" -eq 40 ] || fail 'SILICON_GIT_REV must be an exact 40-character Git commit'
    case "$git_rev" in *[!0-9a-f]*) fail 'SILICON_GIT_REV must be lowercase hexadecimal' ;; esac
fi

system=$(uname -s)
case "$system/$(uname -m)" in
    Darwin/arm64) target=aarch64-apple-darwin; caddy_platform=mac_arm64 ;;
    Darwin/x86_64) target=x86_64-apple-darwin; caddy_platform=mac_amd64 ;;
    Linux/x86_64) target=x86_64-unknown-linux-gnu; caddy_platform=linux_amd64 ;;
    Linux/aarch64|Linux/arm64) target=aarch64-unknown-linux-gnu; caddy_platform=linux_arm64 ;;
    *) fail 'supported systems are macOS and Linux on x86-64 or ARM64' ;;
esac

hash_file() {
    if command -v "sha${1}sum" >/dev/null 2>&1; then
        "sha${1}sum" "$2" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a "$1" "$2" | awk '{print $1}'
    else
        fail "SHA-$1 verifier missing; install shasum or coreutils"
    fi
}

download() {
    command -v curl >/dev/null 2>&1 || fail 'curl is required'
    curl --proto '=https' --tlsv1.2 --fail --location --silent --show-error --retry 3 --output "$2" "$1"
}

verify() {
    expected=$(awk -v name="$3" '$2 == name {print $1}' "$2")
    case "$expected" in ''|*[!0-9a-fA-F]*) fail "missing or ambiguous checksum for $3" ;; esac
    [ "${#expected}" -eq "$(( $1 / 4 ))" ] || fail "invalid checksum for $3"
    actual=$(hash_file "$1" "$4")
    [ "$actual" = "$expected" ] || fail "checksum mismatch for $3; installation was not changed"
}

mkdir -p "$prefix"
prefix=$(CDPATH= cd -- "$prefix" && pwd -P)
[ "$prefix" != / ] || fail 'SILICON_PREFIX must be a dedicated prefix, not /'
runtime="$prefix/lib/silicon"
mkdir -p "$runtime/releases" "$prefix/bin"
for binary in $commands; do
    link="$prefix/bin/$binary"
    if [ -e "$link" ] || [ -L "$link" ]; then
        [ -L "$link" ] && [ "$(readlink "$link")" = "../lib/silicon/current/bin/$binary" ] ||
            fail "$link is not managed by this installer; choose another SILICON_PREFIX"
    fi
done
[ ! -e "$runtime/current" ] || [ -L "$runtime/current" ] || fail 'runtime current path is not a managed symlink'
stage=$(mktemp -d "$runtime/.install.XXXXXX")
new_links=''
activated=false
lock_owned=false
cleanup() {
    status=$?
    trap - 0 HUP INT TERM
    if [ "$activated" = false ]; then
        for binary in $new_links; do
            [ "$(readlink "$prefix/bin/$binary" 2>/dev/null || true)" != "../lib/silicon/current/bin/$binary" ] || rm -f "$prefix/bin/$binary"
        done
    fi
    rm -rf "$stage"
    [ "$lock_owned" = false ] || rmdir "$runtime/.install-lock"
    exit "$status"
}
trap cleanup 0
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
# ponytail: one installer per prefix; after SIGKILL remove a stale lock only
# after confirming no installer is running.
mkdir "$runtime/.install-lock" 2>/dev/null || fail "another installer holds $runtime/.install-lock"
lock_owned=true
mkdir -p "$stage/payload/bin" "$stage/payload/LICENSES"

if [ -n "$git_rev" ]; then
    command -v git >/dev/null 2>&1 || fail 'Git is required for source builds'
    source_dir="$stage/source"
    git init -q "$source_dir"
    git -C "$source_dir" fetch -q --depth 1 "https://github.com/$repository.git" "$git_rev" || fail 'could not fetch the requested interpreter commit'
    git -C "$source_dir" checkout -q --detach FETCH_HEAD
    [ "$(git -C "$source_dir" rev-parse HEAD)" = "$git_rev" ] || fail 'source Git revision did not match'
fi

copy_dependency() {
    [ -n "$dependency_bins" ] && [ -f "$dependency_bins/$1" ] && [ -x "$dependency_bins/$1" ] || return 1
    cp "$dependency_bins/$1" "$stage/payload/bin/$1"
}

install_crate() {
    if ! copy_dependency "$1"; then
        say "building required dependency $1 ($2 $3)"
        cargo install --locked --force --root "$stage/payload" --version "$3" --bin "$1" "$2" ||
            fail "required dependency $1 could not be installed from verified crate $2 $3"
    fi
}

wrap_managed_apps() {
    for app in commit remind; do
        mv "$stage/payload/bin/$app" "$stage/payload/bin/$app-native"
        cat > "$stage/payload/bin/$app" <<'WRAPPER'
#!/bin/sh
set -eu
script=$0
case "$script" in */*) ;; *) script=$(command -v "$script") ;; esac
while [ -L "$script" ]; do
    directory=$(CDPATH= cd -- "$(dirname -- "$script")" && pwd -P)
    script=$(readlink "$script")
    case "$script" in /*) ;; *) script="$directory/$script" ;; esac
done
directory=$(CDPATH= cd -- "$(dirname -- "$script")" && pwd -P)
app=${script##*/}
if [ "$app" = commit ] && [ -z "${COMMIT_API_URL+x}" ]; then
    silicon_app_home=${SILICON_HOME-${HOME:-.}}
    if [ ! -e "$silicon_app_home/.commit/session.json" ] && [ ! -e "$silicon_app_home/.commit/home_dir" ]; then
        COMMIT_API_URL=https://backend.commit.teamofsilicons.com
        export COMMIT_API_URL
    fi
fi
for arg do
    [ "$arg" != --no-update ] || exec "$directory/$app-native" "$@"
done
exec "$directory/$app-native" --no-update "$@"
WRAPPER
    done
}

configure_caddy_port() {
    [ "$system" = Linux ] || return 0
    port_start=$(cat /proc/sys/net/ipv4/ip_unprivileged_port_start 2>/dev/null) || port_start=1024
    case "$port_start" in ''|*[!0-9]*) fail 'could not determine Linux privileged port permissions' ;; esac
    [ "$port_start" -gt 80 ] || return 0
    command -v getcap >/dev/null 2>&1 || fail 'port 80 requires getcap/setcap; install the Linux libcap tools first'
    caddy="$stage/payload/bin/caddy"
    current_caddy="$runtime/current/bin/caddy"
    if [ -f "$current_caddy" ] && caddy_has_capability "$current_caddy" &&
        [ "$(hash_file 256 "$current_caddy")" = "$(hash_file 256 "$caddy")" ]; then
        # An unchanged inode retains its capability without another sudo prompt.
        if ln "$current_caddy" "$stage/payload/caddy.capable" 2>/dev/null; then
            mv -f "$stage/payload/caddy.capable" "$caddy"
            return 0
        fi
    fi
    setcap_path=$(command -v setcap) || fail 'port 80 requires setcap; install the Linux libcap tools first'
    say 'granting Caddy permission to bind local port 80'
    if [ "$(id -u)" = 0 ]; then
        "$setcap_path" cap_net_bind_service=ep "$caddy" || fail 'could not grant Caddy port 80 permission'
    elif command -v sudo >/dev/null 2>&1 && sudo -n "$setcap_path" cap_net_bind_service=ep "$caddy" 2>/dev/null; then
        :
    elif [ "${SILICON_NONINTERACTIVE:-0}" != 1 ] && command -v sudo >/dev/null 2>&1 &&
        ( : </dev/tty ) >/dev/null 2>&1; then
        sudo "$setcap_path" cap_net_bind_service=ep "$caddy" </dev/tty || fail 'could not grant Caddy port 80 permission'
    else
        fail 'Caddy needs port 80 permission; rerun this installation in a terminal with sudo access. The active release was not changed'
    fi
    caddy_has_capability "$caddy" || fail 'Caddy port 80 permission was not applied'
}

caddy_has_capability() {
    case "$(getcap "$1" 2>/dev/null)" in
        *' cap_net_bind_service=ep') return 0 ;;
        *) return 1 ;;
    esac
}

if [ -n "$source_dir" ]; then
    command -v cargo >/dev/null 2>&1 || fail 'source builds require Rust 1.98+ and a C compiler; use a released binary bundle otherwise'
    [ -f "$source_dir/Cargo.toml" ] || fail 'SILICON_SOURCE_DIR must contain Cargo.toml'
    # Reuse compilation across the required crates, then remove staged build files.
    CARGO_TARGET_DIR=${CARGO_TARGET_DIR:-"$stage/build-target"}
    export CARGO_TARGET_DIR
    source_dir=$(CDPATH= cd -- "$source_dir" && pwd -P)
    say "building interpreter from $source_dir"
    cargo install --locked --force --root "$stage/payload" --path "$source_dir" --bin silicon --bin si || fail 'interpreter source build failed'
    for binary in omnid silicon-omni omni so; do
        if ! copy_dependency "$binary"; then
            case "$binary" in omnid) package=omni-daemon ;; *) package=silicon-omni-cli ;; esac
            cargo install --locked --force --root "$stage/payload" --git https://github.com/teamofsilicons/silicon-omni --rev "$omni_rev" --bin "$binary" "$package" ||
                fail "required Omni binary $binary could not be built at $omni_rev"
        fi
    done
    install_crate iam silicon-iam-cli 1.4.1
    install_crate dm silicon-dm-cli 0.3.0
    install_crate briefcase briefcase-cli 0.2.4
    install_crate waveform waveform-cli 0.1.0
    if ! copy_dependency commit; then
        cargo install --locked --force --root "$stage/payload" --git https://github.com/teamofsilicons/silicon-commit --rev "$commit_rev" --bin commit silicon-commit-cli ||
            fail "required Commit binary could not be built at $commit_rev"
    fi
    "$stage/payload/bin/commit" --no-update logout --help >/dev/null || fail 'required Commit logout command is unavailable'
    install_crate remind silicon-remind-cli 0.1.2
    install_crate hook silicon-hook-cli 0.2.0
    wrap_managed_apps
    if ! copy_dependency caddy; then
        caddy_asset="caddy_2.11.4_${caddy_platform}.tar.gz"
        caddy_url=https://github.com/caddyserver/caddy/releases/download/v2.11.4
        download "$caddy_url/$caddy_asset" "$stage/caddy.tar.gz" || fail 'could not download required Caddy 2.11.4'
        download "$caddy_url/caddy_2.11.4_checksums.txt" "$stage/caddy-checksums" || fail 'could not download Caddy checksums'
        # Upstream Caddy's checksum file uses SHA-512.
        verify 512 "$stage/caddy-checksums" "$caddy_asset" "$stage/caddy.tar.gz"
        tar -xOzf "$stage/caddy.tar.gz" caddy > "$stage/payload/bin/caddy" || fail 'Caddy archive has no binary'
    fi
    printf '%s\n' "$version" > "$stage/payload/VERSION"
    cp "$source_dir/install.sh" "$stage/payload/installer.sh" || fail 'source checkout has no installer'
    for notice in $notices; do
        cp "$source_dir/$notice" "$stage/payload/$notice" || fail "source checkout is missing $notice"
    done
    rm -f "$stage/payload/.crates.toml" "$stage/payload/.crates2.json"
else
    asset="silicon-$target.tar.gz"
    release_url=${SILICON_RELEASE_BASE_URL:-"https://github.com/$repository/releases/download/$version"}
    say "downloading $version for $target"
    download "$release_url/$asset" "$stage/bundle.tar.gz" || fail "complete bundle $version/$asset is unavailable; legacy Stemcell releases cannot install the Rust interpreter"
    download "$release_url/SHA256SUMS" "$stage/checksums" || fail 'release checksum file is unavailable'
    verify 256 "$stage/checksums" "$asset" "$stage/bundle.tar.gz"
    # Stream only known members into fixed paths; archive symlinks and ../ names
    # can never redirect extraction into a Silicon's configuration or other files.
    for binary in $binaries; do
        tar -xOzf "$stage/bundle.tar.gz" "bin/$binary" > "$stage/payload/bin/$binary" || fail "release is missing required binary $binary"
    done
    tar -xOzf "$stage/bundle.tar.gz" VERSION > "$stage/payload/VERSION" || fail 'release has no version marker'
    tar -xOzf "$stage/bundle.tar.gz" installer.sh > "$stage/payload/installer.sh" || fail 'release has no installer'
    for notice in $notices; do
        tar -xOzf "$stage/bundle.tar.gz" "$notice" > "$stage/payload/$notice" || fail "release is missing $notice"
    done
    [ "$(cat "$stage/payload/VERSION")" = "$version" ] || fail 'release version marker does not match the requested release'
fi

for binary in $binaries; do
    [ -s "$stage/payload/bin/$binary" ] && [ ! -L "$stage/payload/bin/$binary" ] || fail "required binary $binary is missing or empty"
    chmod 755 "$stage/payload/bin/$binary"
done
[ -s "$stage/payload/installer.sh" ] || fail 'release installer is empty'
for notice in $notices; do
    [ -s "$stage/payload/$notice" ] || fail "release notice $notice is empty"
done
"$stage/payload/bin/silicon" --version >/dev/null || fail 'interpreter binary cannot run on this system'
"$stage/payload/bin/omnid" --version >/dev/null || fail 'Omni daemon binary cannot run on this system'
printf '%s\n' "$prefix" > "$stage/payload/PREFIX"
configure_caddy_port

release_name="$version-${stage##*.}"
mv "$stage/payload" "$runtime/releases/$release_name"
for binary in $commands; do
    if [ ! -L "$prefix/bin/$binary" ]; then
        ln -s "../lib/silicon/current/bin/$binary" "$prefix/bin/$binary"
        new_links="$new_links $binary"
    fi
done
ln -s "releases/$release_name" "$stage/current"
case "$system" in
    Darwin) mv -fh "$stage/current" "$runtime/current" ;;
    Linux) mv -fT "$stage/current" "$runtime/current" ;;
esac
activated=true
say "installed $version and every required dependency in $prefix/bin"
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
            say "could not update $profile; use the PATH command below"
        fi
    fi
fi
case ":$PATH:" in
    *":$prefix/bin:"*) ;;
    *) printf 'For this terminal, run: %s\n' "$path_line" ;;
esac
