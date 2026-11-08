#!/usr/bin/env python3
"""DriveWatch local developer bootstrap. Run from repository root: python3 scripts/dev.py up."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
TEL = ROOT / 'telemetry'
WEB = ROOT / 'my-spacetime-app'
STATE = ROOT / '.dev-run'
DB = 'ssd-monitor'
REQUIRED_NODE = '22.23.4+ on Node 22, 24.22.1+ on Node 24, or 26.12.1+'


def fail(message: str):
    raise RuntimeError(message)

def exe(name: str):
    found = shutil.which(name)
    if found:
        return found
    if name == 'spacetime':
        for path in (Path.home()/'.local/bin/spacetime', Path.home()/'.spacetime/bin/spacetime'):
            if path.is_file():
                return str(path)
    return None

def run(args, cwd=ROOT, env=None):
    print('>', ' '.join(map(str, args)), flush=True)
    subprocess.run(list(map(str,args)), cwd=cwd, env=env, check=True)

def version_node():
    if not exe('node'):
        return None
    output = subprocess.check_output(['node','--version'],text=True).strip()
    match = re.fullmatch(r'v(\d+)\.(\d+)\.(\d+)',output)
    return tuple(map(int, match.groups())) if match else None

def supported_node(v):
    return v is not None and ((v[0]==22 and v>=(22,23,4)) or (v[0]==24 and v>=(24,22,1)) or v>=(26,12,1))

def python_ok():
    return sys.version_info[:2] == (3,12)

def vpython():
    return TEL/'.venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')

def is_listening(port):
    with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as s:
        s.settimeout(.3)
        return s.connect_ex(('127.0.0.1',port))==0

def health(url):
    try:
        with urllib.request.urlopen(url,timeout=1.5) as res:
            return res.status == 200
    except Exception:
        return False

def check(full=True):
    print('DriveWatch prerequisite check')
    print('Root:',ROOT)
    results = {
        'Python 3.12':python_ok(),
        f'Node ({REQUIRED_NODE})': supported_node(version_node()),
        **({'npm':bool(exe('npm')), 'SpacetimeDB CLI':bool(exe('spacetime'))} if full else {}),
        'Expected project files': (TEL/'requirements.txt').is_file() and (WEB/'package.json').is_file() and (WEB/'spacetimedb/src/index.ts').is_file(),
    }
    for name, ok in results.items():
        print(('PASS' if ok else 'FAIL'), name)
    if not python_ok():
        print('  Install Python 3.12 and rerun this using python3.12 scripts/dev.py')
    if full and not supported_node(version_node()):
        print('  Install a supported Node release (see my-spacetime-app/package.json).')
    if full and not exe('spacetime'):
        print('  Install CLI: https://spacetimedb.com/install')
        print('  macOS/Linux: curl -sSf https://install.spacetimedb.com | sh')
        print('  CLI in ~/.local/bin is automatically detected by this script.')
    return all(results.values())

def setup():
    if not check():
        fail('Prerequisites missing. Install them first, then rerun setup.')
    if not vpython().is_file():
        run([sys.executable,'-m','venv',TEL/'.venv'])
    run([vpython(),'-m','pip','install','-r','requirements.txt'], TEL)


