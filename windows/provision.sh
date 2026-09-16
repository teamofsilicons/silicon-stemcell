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
commands = 'silicon si omnid silicon-omni omni so caddy iam honeycomb spacestation dm briefcase waveform commit remind hook commit-native remind-native'.split()
notices = 'README.md iam-LICENSE.txt dm-NOTICE.txt briefcase-LICENSE.txt waveform-LICENSE.txt commit-NOTICE.txt remind-LICENSE.txt hook-NOTICE.txt omni-LICENSE.txt caddy-LICENSE.txt caddy-AUTHORS.txt honeycomb-LICENSE.txt spacestation-LICENSE.txt'.split()
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
for binary in silicon si omnid silicon-omni omni so caddy iam honeycomb spacestation dm briefcase waveform commit remind hook; do
    path="$prefix/bin/$binary"
    { [ ! -e "$path" ] && [ ! -L "$path" ]; } || [ "$(readlink "$path")" = "../lib/silicon/current/bin/$binary" ] || { echo "Unmanaged executable at $path" >&2; exit 2; }
    [ -L "$path" ] || ln -s "../lib/silicon/current/bin/$binary" "$path"
done
chown silicon:silicon /home/silicon/.local /home/silicon/.local/share "$prefix" "$prefix/bin" "$prefix/lib" "$runtime" "$runtime/releases"
ln -s "releases/$version-$expected" "$runtime/current.new"
chown -h silicon:silicon "$runtime/current.new"
mv -fT "$runtime/current.new" "$runtime/current"
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
printf '[user]\ndefault=silicon\n[interop]\nappendWindowsPath=false\n' > /etc/wsl.conf
printf 'export PATH="$HOME/.local/share/silicon/bin:$HOME/.silicon/bin:$HOME/.local/bin:$PATH"\nexport SILICON_WSL=1\n' > /etc/profile.d/silicon.sh
printf '%s\n' "$version" > /opt/silicon/windows-version
printf 'Silicon %s installed. Project home: /home/silicon; Windows files: /mnt/c.\n' "$version"
