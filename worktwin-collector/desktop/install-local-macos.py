"""Install a verified local test app, preserving the prior app and SQLite snapshot."""
import argparse
import json
import plistlib
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from urllib.request import Request, urlopen


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app',type=Path)
    args=parser.parse_args()
    source=args.app.resolve();target=Path('/Applications/WorkTwin.app')
    with (source/'Contents/Info.plist').open('rb') as stream:version=plistlib.load(stream)['CFBundleShortVersionString']
    subprocess.run(['codesign','--verify','--deep','--strict',str(source)],check=True)
    data=Path.home()/'Library/Application Support/WorkTwin Collector'
    backups=data/'backups';backups.mkdir(parents=True,exist_ok=True)
    stamp=time.strftime('%Y%m%d-%H%M%S')
    try:
        page=urlopen('http://127.0.0.1:8765/',timeout=3).read().decode()
    except OSError:
        page=None
    if page:
        token=re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";',page).group(1)
        request=Request('http://127.0.0.1:8765/api/shutdown',data=b'{}',method='POST',headers={'X-Worktwin-Token':token,'Content-Type':'application/json'})
        urlopen(request,timeout=5).close()
        for _ in range(30):
            try:urlopen('http://127.0.0.1:8765/api/health',timeout=1).close()
            except OSError:break
            time.sleep(.5)
        else:raise RuntimeError('WorkTwin did not stop; installation not performed')
    db=data/'worktwin.sqlite';snapshot=backups/f'worktwin-{stamp}-before-{version}.sqlite'
    if db.exists():
        with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True) as current, sqlite3.connect(snapshot) as copy:current.backup(copy)
    prior=None
    if target.exists():
        with (target/'Contents/Info.plist').open('rb') as stream:old=plistlib.load(stream)['CFBundleShortVersionString']
        prior=backups/f'WorkTwin-{old}-{stamp}.app';target.rename(prior)
    subprocess.run(['ditto',str(source),str(target)],check=True)
    subprocess.run(['open',str(target)],check=True)
    for _ in range(60):
        try:
            health=json.load(urlopen('http://127.0.0.1:8765/api/health',timeout=2));break
        except OSError:time.sleep(.5)
    else:raise RuntimeError(f'New app did not start. Previous app: {prior}; database: {snapshot}')
    if health.get('version')!=version:raise RuntimeError('Running version differs from installed version')
    record={'version':version,'health':health,'database_backup':str(snapshot),'previous_app':str(prior)}
    (backups/f'install-{stamp}.json').write_text(json.dumps(record,ensure_ascii=False,indent=2))
    print(json.dumps(record,ensure_ascii=False))


if __name__=='__main__':main()
