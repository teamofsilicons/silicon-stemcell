#!/bin/sh
set -eu
payload=$1
expected=$2
version=$3
windows_powershell=$4
windows_opener=$5
case "$version" in v[0-9]*.[0-9]*.[0-9]*) ;; *) echo 'Invalid Silicon version' >&2; exit 2 ;; esac
case "$version" in *[!a-zA-Z0-9._-]*) exit 2 ;; esac
actual=$(sha256sum "$payload/runtime.tar.gz" | cut -d' ' -f1)
[ "$actual" = "$expected" ] || { echo 'Linux bundle checksum mismatch' >&2; exit 2; }
if ! id silicon >/dev/null 2>&1; then useradd --create-home --shell /bin/bash silicon; fi
chmod 700 /home/silicon
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# The verified rootfs is an immutable base image; apply current Ubuntu security
# updates before running applications, while preserving existing local config.
apt-get upgrade -y -qq --with-new-pkgs -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold
apt-get install -y -qq ca-certificates curl libcap2-bin python3 git
prefix=/home/silicon/.local/share/silicon
runtime="$prefix/lib/silicon"
mkdir -p "$runtime/releases" "$prefix/bin" /opt/silicon
release="$runtime/releases/$version-$expected"
if [ ! -f "$release/VERSION" ]; then
    stage=$(mktemp -d "$runtime/releases/.install.XXXXXX")
    trap 'rm -rf "$stage"' EXIT HUP INT TERM
    # Validate every entry before extraction, including the fixed inventory from
    # install.sh. Never let an archive symlink redirect a privileged extraction.
    python3 - "$payload/runtime.tar.gz" "$stage" <<'PY'
import pathlib, sys, tarfile
commands = 'silicon si omnid silicon-omni omni so caddy'.split()
notices = 'README.md omni-LICENSE.txt caddy-LICENSE.txt caddy-AUTHORS.txt'.split()
expected = {'VERSION', 'installer.sh', 'LICENSE'} | {'bin/' + name for name in commands} | {'LICENSES/' + name for name in notices}
with tarfile.open(sys.argv[1]) as archive:
    files = [member for member in archive.getmembers() if not member.isdir()]
    assert len(files) == len(expected) and {m.name for m in files} == expected, 'Linux bundle inventory mismatch'
    assert all(m.isfile() for m in files), 'Linux bundle contains a link or special file'
    for member in files:
        path = pathlib.Path(sys.argv[2]) / member.name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(archive.extractfile(member).read())
        path.chmod(0o755 if member.name.startswith('bin/') else 0o644)
PY
    [ "$(cat "$stage/VERSION")" = "$version" ]
    printf '%s\n' "$prefix" > "$stage/PREFIX"
    chown -R silicon:silicon "$stage"
    runuser -u silicon -- env HOME=/home/silicon SILICON_TELEMETRY=0 SILICON_AUTO_UPDATE=0 "$stage/bin/silicon" --version
    /usr/sbin/setcap cap_net_bind_service=ep "$stage/bin/caddy"
    chmod -R go-w "$stage"
    mv "$stage" "$release"
    trap - EXIT HUP INT TERM
fi
for binary in silicon si omnid silicon-omni omni so caddy; do
    path="$prefix/bin/$binary"
    { [ ! -e "$path" ] && [ ! -L "$path" ]; } || [ "$(readlink "$path")" = "../lib/silicon/current/bin/$binary" ] || { echo "Unmanaged executable at $path" >&2; exit 2; }
    [ -L "$path" ] || ln -s "../lib/silicon/current/bin/$binary" "$path"
done
chown silicon:silicon /home/silicon/.local /home/silicon/.local/share "$prefix" "$prefix/bin" "$prefix/lib" "$runtime" "$runtime/releases"
next="$runtime/current.new.$$"
ln -s "releases/$version-$expected" "$next"
trap 'rm -f "$next"' EXIT HUP INT TERM
chown -h silicon:silicon "$next"
mv -fT "$next" "$runtime/current"
trap - EXIT HUP INT TERM
# Install Honeycomb separately at its latest release, retaining its own update policy.
bootstrap=$(mktemp)
trap 'rm -f "$bootstrap"' EXIT HUP INT TERM
curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/teamofsilicons/silicon-honeycomb/main/install.sh -o "$bootstrap"
chmod 644 "$bootstrap"
runuser -u silicon -- env HOME=/home/silicon SILICON_HOME="$prefix" HONEYCOMB_NO_MODIFY_PATH=1 bash "$bootstrap"
rm "$bootstrap"
trap - EXIT HUP INT TERM
honeycomb="$prefix/.honeycomb/dir/system/bin/honeycomb"
link="$prefix/bin/honeycomb"
if [ -e "$link" ] || [ -L "$link" ]; then
    [ -L "$link" ] && { [ "$(readlink "$link")" = '../lib/silicon/current/bin/honeycomb' ] || [ "$(readlink "$link")" = "$honeycomb" ]; } || { echo "Unmanaged executable at $link" >&2; exit 2; }
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
[ "$#" = 1 ] || exit 2
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
