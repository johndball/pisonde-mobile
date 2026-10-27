#!/usr/bin/env python3
"""Publish reviewed version notes as tags/Releases using the Actions job token."""
import importlib.util,json,os,re,sys,urllib.error,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def request(method,path,body=None):
    repo=os.environ['GITHUB_REPOSITORY']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo):raise RuntimeError('Invalid repository')
    req=urllib.request.Request('https://api.github.com/repos/'+repo+path,
        data=json.dumps(body).encode() if body is not None else None,method=method,
        headers={'Authorization':'Bearer '+os.environ['GITHUB_TOKEN'],'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28','User-Agent':'PiSonde-Release-Publisher'})
    try:
        with urllib.request.urlopen(req,timeout=30) as response:return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code==404 and method=='GET':return None
        raise RuntimeError('GitHub release API failed: '+str(exc.code)+' '+method+' '+path) from None

def publish(entry,stable):
    tag=entry['tag']
    if not re.fullmatch(r'v\d+\.\d+\.\d+',tag):raise RuntimeError('Invalid release tag')
    target=entry['commit']
    if target in ('stable','signed-stable'):
        if target=='signed-stable' and stable.get('schema')!=2:
            print('Waiting for signed-channel publication:',tag);return
        if tag!='v'+stable['version']:
            print('Waiting for stable-channel publication:',tag);return
        target=stable['commit']
    if not re.fullmatch('[0-9a-f]{40}',target):raise RuntimeError('Invalid source commit')
    notes=ROOT/'release-notes'/(tag+'.md')
    body=notes.read_text().replace('{{SOURCE_COMMIT}}',target)
    source=request('GET','/git/commits/'+target)
    if not source:raise RuntimeError('Release target is not available')
    ref=request('GET','/git/ref/tags/'+tag)
    if ref and (ref['object']['type']!='commit' or ref['object']['sha']!=target):raise RuntimeError('Existing tag points elsewhere; refusing to move '+tag)
    if not ref:request('POST','/git/refs',{'ref':'refs/tags/'+tag,'sha':target})
    desired={'tag_name':tag,'target_commitish':target,'name':'PiSonde Mobile '+tag[1:],'body':body,'draft':False,'prerelease':bool(entry.get('prerelease',False)),'make_latest':'true' if tag=='v'+stable['version'] and not entry.get('prerelease') else 'false'}
    release=request('GET','/releases/tags/'+tag)
    if release:
        if any(release.get(k)!=desired[k] for k in ('name','body','prerelease')):request('PATCH','/releases/'+str(release['id']),desired)
    else:request('POST','/releases',desired)
    print('Published:',tag)

def main():
    entries=json.loads((ROOT/'releases/versions.json').read_text())
    stable=json.loads((ROOT/'updates/stable.json').read_text())
    signed=ROOT/'updates/signed-stable.json'
    if signed.exists():
        spec=importlib.util.spec_from_file_location('verify_published_manifest',ROOT/'enroll-signed-updates.py')
        verifier=importlib.util.module_from_spec(spec);spec.loader.exec_module(verifier)
        stable=verifier.verify(verifier.strict_json(signed.read_bytes()),ROOT/'updates/signing-public.pem')
    for entry in entries:publish(entry,stable)
if __name__=='__main__':main()
