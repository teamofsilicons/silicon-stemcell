#!/usr/bin/env bash
set -euo pipefail
umask 077
release_directory="${1:?release directory}"
runtime_secret="${2:?runtime secret ARN}"
artifact_region="${3:-us-east-1}"
exec 9>/var/lock/silicon-realtime-deploy.lock
flock -n 9
id silicon-realtime >/dev/null 2>&1 || useradd --system --home-dir /var/lib/silicon-realtime --shell /usr/sbin/nologin silicon-realtime
install -d -o silicon-realtime -g silicon-realtime -m 0700 /var/lib/silicon-realtime
install -d -o root -g root -m 0700 /etc/silicon-realtime
aws secretsmanager get-secret-value --secret-id "$runtime_secret" --region "$artifact_region" --query SecretString --output text > /etc/silicon-realtime/runtime.json
python3 - <<'PY'
import json,os,re
values=json.load(open('/etc/silicon-realtime/runtime.json'))
with open('/etc/silicon-realtime/runtime.env.new','w') as output:
    os.chmod(output.name,0o600)
    for key,value in values.items():
        assert re.fullmatch('[A-Z][A-Z0-9_]*',key) and isinstance(value,str)
        assert not any(char in value for char in '\n\r\0')
        value=value.replace('\\','\\\\').replace('"','\\"')
        output.write(f'{key}="{value}"\n')
os.replace('/etc/silicon-realtime/runtime.env.new','/etc/silicon-realtime/runtime.env')
os.unlink('/etc/silicon-realtime/runtime.json')
PY
install -m 0644 "$release_directory/silicon-realtime.service" /etc/systemd/system/silicon-realtime.service
previous_release=$(readlink -f /opt/silicon-realtime/current || true)
ln -sfn "$release_directory" /opt/silicon-realtime/current.next
mv -Tf /opt/silicon-realtime/current.next /opt/silicon-realtime/current
systemctl daemon-reload
systemctl enable silicon-realtime.service
systemctl restart silicon-realtime.service
for attempt in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:1830/health; then exit 0; fi
    sleep 1
done
if [[ -n "$previous_release" ]]; then
    ln -sfn "$previous_release" /opt/silicon-realtime/current.next
    mv -Tf /opt/silicon-realtime/current.next /opt/silicon-realtime/current
    systemctl restart silicon-realtime.service
fi
exit 1
