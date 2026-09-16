#!/usr/bin/env python3
"""Deploy the verified ARM64 relay binary through private S3 and SSM; prints no credentials."""
import hashlib,json,pathlib,shlex,subprocess,tarfile,tempfile,time
root=pathlib.Path(__file__).resolve().parents[1]
def aws(*args):return subprocess.check_output(['aws','--region','us-east-1',*args],text=True)
stack=json.loads(aws('cloudformation','describe-stacks','--stack-name','silicon-realtime-production'))['Stacks'][0]
outputs={v['OutputKey']:v['OutputValue'] for v in stack['Outputs']}
files=[root/'target/aarch64-unknown-linux-gnu/release/silicon-realtime',root/'deploy/install.sh',root/'deploy/silicon-realtime.service']
revision=hashlib.sha256(b''.join(p.read_bytes() for p in files)).hexdigest()[:20]
remote='/opt/silicon-realtime/releases/'+revision
with tempfile.TemporaryDirectory() as temp:
    archive=pathlib.Path(temp)/'release.tar.gz'
    with tarfile.open(archive,'w:gz') as tar:
        for path in files:tar.add(path,arcname=path.name)
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    url=f"s3://{outputs['ArtifactBucket']}/releases/{revision}.tar.gz"
    aws('s3','cp',str(archive),url,'--only-show-errors','--sse','AES256')
    quote=shlex.quote
    commands=['set -euo pipefail',f'install -d -m 0755 {quote(remote)}',f'aws s3 cp {quote(url)} {quote(remote+".tar.gz")} --region us-east-1 --only-show-errors',f'echo {quote(digest+"  "+remote+".tar.gz")} | sha256sum -c -',f'tar -xzf {quote(remote+".tar.gz")} -C {quote(remote)}',f'bash {quote(remote+"/install.sh")} {quote(remote)} {quote(outputs["RuntimeSecretArn"])}']
    request={'DocumentName':'AWS-RunShellScript','InstanceIds':[outputs['InstanceId']],'Parameters':{'commands':['bash -c '+quote('\n'.join(commands))]},'TimeoutSeconds':300,'Comment':'Deploy Silicon realtime '+revision}
    path=pathlib.Path(temp)/'request.json';path.write_text(json.dumps(request))
    command=json.loads(aws('ssm','send-command','--cli-input-json','file://'+str(path)))['Command']['CommandId']
print(json.dumps({'release':revision,'command':command}),flush=True)
for _ in range(60):
    time.sleep(2)
    result=json.loads(aws('ssm','get-command-invocation','--command-id',command,'--instance-id',outputs['InstanceId']))
    if result['Status'] in ['Pending','InProgress','Delayed']:continue
    print(json.dumps({k:result[k] for k in ['Status','StandardOutputContent','StandardErrorContent']}))
    raise SystemExit(0 if result['Status']=='Success' else 1)
raise SystemExit('Deployment remains running; inspect the SSM command before retrying')
