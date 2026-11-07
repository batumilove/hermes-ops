"""Finite non-secret configuration inspection, never effective-route attestation.

Only presence booleans for four exact keys and a public source digest cross SSH.
No settings imports, dotenv reads, provider requests or secret-derived hashes.
"""
import json
import shlex
import time

KEYS = (
    'EMBEDDING_MODEL_CONFIG__MODEL',
    'EMBEDDING_MODEL_CONFIG__OVERRIDES__BASE_URL',
    'DERIVER_MODEL_CONFIG__MODEL',
    'DERIVER_MODEL_CONFIG__OVERRIDES__BASE_URL',
)


def config_inspection_command():
    inner = '''import os,json,hashlib
from pathlib import Path
p=Path('/app/src/config.py')
blob=p.open('rb').read(1048577) if p.is_file() else b''
if len(blob)>1048576: raise SystemExit(1)
print(json.dumps({'present':[bool(os.environ.get(k)) for k in KEYS],
 'config_source_sha256':hashlib.sha256(blob).hexdigest() if blob else '',
 'toml_present':Path('/app/config.toml').exists(),
 'dotenv_present':Path('/app/.env').exists()}))
'''.replace('KEYS', repr(KEYS))
    outer = '''import json,subprocess,time
rows={}
for label,name in [('api','honcho-api-1'),('deriver','honcho-deriver-1')]:
 try:
  r=subprocess.run(['docker','exec',name,'python','-c',INNER],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=7)
  if r.returncode or len(r.stdout)>4096: raise ValueError()
  rows[label]=json.loads(r.stdout)
 except Exception: raise SystemExit(1)
print(json.dumps({'version':1,'collected_at':int(time.time()),'containers':rows}))
'''.replace('INNER', repr(inner))
    return 'python3 -c ' + shlex.quote(outer)


def collect_config_inspection(ssh_fn, *, now=None):
    """Acquire fresh input; reject partial/stale results without echoing input."""
    failed = {'status': 'unavailable', 'reason': 'inspection_collection_failed',
              'effective_status': 'unverified'}
    try:
        raw = ssh_fn(config_inspection_command(), timeout=20)
        observed = time.time() if now is None else now
        if not isinstance(raw, str) or len(raw.encode()) > 8192:
            return failed
        def unique(pairs):
            d = {}
            for k, v in pairs:
                if k in d:
                    raise ValueError()
                d[k] = v
            return d
        data = json.loads(raw, object_pairs_hook=unique)
        if set(data) != {'version', 'collected_at', 'containers'}:
            return failed
        if type(data['version']) is not int or data['version'] != 1:
            return failed
        stamp = data['collected_at']
        if type(stamp) is not int or not 0 <= observed-stamp <= 60:
            return failed
        rows = data['containers']
        if set(rows) != {'api', 'deriver'}:
            return failed
        complete = True
        for row in rows.values():
            if set(row) != {'present', 'config_source_sha256', 'toml_present', 'dotenv_present'}:
                return failed
            flags = row['present']
            if not isinstance(flags, list) or len(flags) != 4 or any(type(x) is not bool for x in flags):
                return failed
            if any(type(row[k]) is not bool for k in ('toml_present', 'dotenv_present')):
                return failed
            digest = row['config_source_sha256']
            if not isinstance(digest, str) or (digest and (len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest))):
                return failed
            complete &= all(flags) and bool(digest)
        return {'status': 'inspected' if complete else 'incomplete',
                'collected_at': stamp, 'containers': rows,
                'effective_status': 'unverified',
                'reason': 'finite_key_presence_only_precedence_not_attested'}
    except Exception:
        return failed
