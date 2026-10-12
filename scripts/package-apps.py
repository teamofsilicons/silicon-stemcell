#!/usr/bin/env python3
"""Package a complete Silicon bundle for the Silicon Apps registry."""
import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('bundle', type=Path)
parser.add_argument('target', choices=['linux-x86_64', 'linux-aarch64', 'macos-x86_64', 'macos-aarch64'])
parser.add_argument('output', type=Path)
parser.add_argument('--apps', default='silicon-apps')
args = parser.parse_args()
source = args.bundle.resolve()
version = (source / 'VERSION').read_text().strip().removeprefix('v')
if len(version.split('.')) != 3 or not all(part.isdigit() for part in version.split('.')):
    parser.error('bundle VERSION must be x.y.z')
args.output.parent.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='silicon-package-') as temp:
    package = Path(temp)
    for name in ['bin', 'VERSION', 'LICENSE', 'LICENSES']:
        path = source / name
        if path.is_dir():
            shutil.copytree(path, package / name, symlinks=False)
        else:
            shutil.copy2(path, package / name)
    # Apps exports the main command. Supporting tools are found next to the native binary.
    (package / 'apps.yaml').write_text(
        f'schema_version: 1\napp_id: silicon\nversion: {version}\ncommand: silicon\n'
        f'targets:\n  {args.target}:\n    binary: bin/silicon\n'
        '    install_script: install-app.sh\n'
    )
    (package / 'install-app.sh').write_text('''#!/bin/sh
set -eu
root=$(CDPATH= cd -- "${APPS_INSTALL_DIR:-$(dirname -- "$0")}" && pwd -P)
apps_bin=$(CDPATH= cd -- "$root/../../bin" && pwd -P)
link="$apps_bin/si"
if [ -e "$link" ] || [ -L "$link" ]; then
    [ -L "$link" ] && { [ "$(readlink "$link")" = "$root/bin/si" ] || [ "$(readlink "$link")" = '../installed/silicon/bin/si' ]; } || {
        echo "Cannot expose si: an unmanaged command already exists at $link" >&2
        exit 1
    }
fi
if [ "$(uname -s)" = Linux ]; then
    PATH="$PATH:/usr/sbin:/sbin"
    export PATH
    port_start=$(cat /proc/sys/net/ipv4/ip_unprivileged_port_start 2>/dev/null) || port_start=1024
    case "$port_start" in ''|*[!0-9]*) echo 'Cannot determine Linux port 80 permissions' >&2; exit 1 ;; esac
    if [ "$port_start" -gt 80 ]; then
        caddy="$root/bin/caddy"
        capable() { command -v getcap >/dev/null 2>&1 && getcap "$1" | grep -q ' cap_net_bind_service=ep$'; }
        if ! capable "$caddy"; then
            # Keep an unchanged capable inode across Apps' transactional replacement.
            for previous in "$root"/../.silicon-backup-*/bin/caddy; do
                [ -f "$previous" ] && capable "$previous" || continue
                previous_digest=$(sha256sum "$previous")
                current_digest=$(sha256sum "$caddy")
                if [ "${previous_digest%% *}" = "${current_digest%% *}" ]; then
                    if ln "$previous" "$root/bin/.caddy-capable"; then
                        mv -f "$root/bin/.caddy-capable" "$caddy"
                        break
                    fi
                fi
            done
        fi
        if ! capable "$caddy"; then
            if command -v setcap >/dev/null 2>&1; then
                if [ "$(id -u)" = 0 ]; then
                    setcap cap_net_bind_service=ep "$caddy" || true
                elif command -v sudo >/dev/null 2>&1; then
                    sudo -n setcap cap_net_bind_service=ep "$caddy" || true
                fi
            fi
            if ! capable "$caddy"; then
                echo "Caddy needs permission to bind local port 80. Install libcap tools, run sudo -v, then retry silicon-apps install silicon. The installer needs: sudo setcap cap_net_bind_service=ep '$caddy'. The previous package is restored on failure." >&2
                exit 1
            fi
        fi
    fi
fi
ln -s "$root/bin/si" "$link.new.$$"
mv -f "$link.new.$$" "$link"
''')
    (package / 'install-app.sh').chmod(0o755)
    env = dict(os.environ, APPS_TELEMETRY='0', ACCOUNTS_TELEMETRY='off', DO_NOT_TRACK='1')
    subprocess.run([args.apps, 'validate', str(package)], check=True, env=env)
    subprocess.run([args.apps, 'pack', str(package), '--output', str(args.output.resolve())], check=True, env=env)
