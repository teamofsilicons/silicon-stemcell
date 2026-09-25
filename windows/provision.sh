#!/bin/sh
set -eu
payload=$1
expected=$2
version=$3
windows_powershell=$4
windows_opener=$5
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
if [ ! -f "$release/VERSION" ]; then
    stage=$(mktemp -d "$runtime/releases/.install.XXXXXX")
    trap 'rm -rf "$stage"' EXIT HUP INT TERM
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
    trap - EXIT HUP INT TERM
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
trap 'rm -f "$next"' EXIT HUP INT TERM
chown -h silicon:silicon "$next"
run "could not activate Silicon $version; the active release was not changed" mv -fT "$next" "$runtime/current"
trap - EXIT HUP INT TERM
# Install latest Honeycomb independently; leave its app-update settings and services alone.
bootstrap=$(mktemp -d)
trap 'rm -rf "$bootstrap"' EXIT HUP INT TERM
machine=$(uname -m)
case "$machine" in x86_64) honeycomb_arch=x86_64 ;; aarch64|arm64) honeycomb_arch=aarch64 ;; *) fail "Honeycomb has no Linux build for architecture $machine" ;; esac
honeycomb_asset="honeycomb-linux-$honeycomb_arch.tar.gz"
honeycomb_url=https://github.com/teamofsilicons/silicon-honeycomb/releases/latest/download
run 'could not download the latest Honeycomb' curl --proto '=https' --tlsv1.2 -fsSL "$honeycomb_url/$honeycomb_asset" -o "$bootstrap/$honeycomb_asset"
run 'could not download the latest Honeycomb checksum' curl --proto '=https' --tlsv1.2 -fsSL "$honeycomb_url/$honeycomb_asset.sha256" -o "$bootstrap/checksum"
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
printf '%s\n' "$version" > /opt/silicon/windows-version
printf 'Silicon %s installed. Project home: /home/silicon; Windows files: /mnt/c.\n' "$version"
