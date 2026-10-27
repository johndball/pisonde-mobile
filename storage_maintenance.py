#!/usr/bin/env python3
"""Online SQLite backup with validation, atomic replacement, and two retained copies."""
import os,sqlite3,time
from pathlib import Path
import shutil
DATA=Path('/var/lib/pisonde-mobile/receptions.sqlite3')
BACKUPS=Path('/var/backups/pisonde-mobile/history')
def main():
    BACKUPS.mkdir(parents=True,exist_ok=True)
    if not DATA.exists():raise SystemExit('Reception database does not exist')
    free=shutil.disk_usage(BACKUPS).free
    if free<DATA.stat().st_size*2+1024**3:
        raise SystemExit('Insufficient space for checked backup; history was not removed')
    temp=BACKUPS/'history.new.sqlite3'
    temp.unlink(missing_ok=True)
    source=sqlite3.connect(f'file:{DATA}?mode=ro',uri=True,timeout=15)
    backup=sqlite3.connect(temp)
    try:source.backup(backup,pages=1000,sleep=.02)
    finally:source.close();backup.close()
    test=sqlite3.connect(f'file:{temp}?mode=ro',uri=True)
    try:result=test.execute('PRAGMA quick_check').fetchone()[0]
    finally:test.close()
    if result!='ok':raise SystemExit('Backup validation failed: '+result)
    os.chmod(temp,0o640)
    with temp.open('rb') as stream:os.fsync(stream.fileno())
    latest=BACKUPS/'history-latest.sqlite3';previous=BACKUPS/'history-previous.sqlite3'
    if latest.exists():os.replace(latest,previous)
    os.replace(temp,latest)
    fd=os.open(BACKUPS,os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)
    print('PASS: checked history backup stored; latest and previous copies retained.')
if __name__=='__main__':main()
