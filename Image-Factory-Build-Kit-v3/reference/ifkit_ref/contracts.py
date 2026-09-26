"""Validated, immutable task definitions. Standard library only."""
from __future__ import annotations
import hashlib
import json
import re
from typing import Any

class ContractError(ValueError):
    pass

class Conflict(ContractError):
    pass

class Blocked(ContractError):
    pass

def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)

def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()

def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}', value):
        raise ContractError('Unsafe or empty identifier')
    return value

def integer(value: Any, low: int, high: int, label: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ContractError(f'{label}: integer in [{low}, {high}] required')
    return value

def checked_plan(plan: dict) -> dict:
    # Round-trip severs references to caller-owned mutable objects.
    if not isinstance(plan, dict):
        raise ContractError('Plan must be an object')
    p = json.loads(canonical(plan))
    if p.get('schema_version') != 1 or p.get('category') != 'kids_shoes':
        raise ContractError('Only schema=1, category=kids_shoes supported')
    identifier(p.get('run_id'))
    if p.get('mode') not in ('demo', 'production'):
        raise ContractError('Explicit demo or production mode required')
    if not isinstance(p.get('workflow_version'), str) or not p['workflow_version']:
        raise ContractError('Workflow version required')
    integer(p.get('max_image_calls'), 0, 100, 'max_image_calls')
    steps = p.get('steps')
    if not isinstance(steps, list) or not steps:
        raise ContractError('Nonempty ordered steps required')
    seen = set()
    allowed = {'codex_brief', 'image_generate', 'compose', 'human_review', 'export', 'metrics'}
    for s in steps:
        if not isinstance(s, dict):
            raise ContractError('Step must be an object')
        sid = identifier(s.get('id'))
        if sid in seen or s.get('kind') not in allowed:
            raise ContractError('Duplicate step or unsupported node type')
        if not isinstance(s.get('depends_on', []), list):
            raise ContractError('depends_on must be a list')
        if any(x not in seen for x in s.get('depends_on', [])):
            raise ContractError('Dependencies must precede step; no cycles or forward references')
        seen.add(sid)
    return p

def validate_receipt(receipt: dict, job: dict, run_mode: str, required_origin: str) -> None:
    if receipt.get('job_id') != job['job_id'] or receipt.get('request_hash') != job['request_hash']:
        raise Conflict('Receipt does not belong to frozen request')
    if receipt.get('mode') != run_mode or receipt.get('status') != 'succeeded':
        raise Blocked('Mode mismatch or unsuccessful receipt')
    if receipt.get('origin') != required_origin:
        raise Blocked('Receipt origin mismatch; mock is not native generation')
    for key in ('tool', 'model_reported', 'evidence_ref'):
        if not isinstance(receipt.get(key), str) or not receipt[key].strip():
            raise ContractError(f'Missing receipt {key}; use not_reported for unreported model')
    outputs = receipt.get('outputs')
    if not isinstance(outputs, list) or not outputs:
        raise ContractError('Receipt needs at least one output')
    req = job.get('request', {})
    req = json.loads(req) if isinstance(req, str) else req
    definition = req.get('definition', {})
    if definition.get('kind') == 'image_generate':
        expected = definition.get('independent_image_count', 1)
        integer(expected, 1, 10, 'independent_image_count')
        if len(outputs) != expected:
            raise Conflict('Unexpected image output count')
    names = set()
    for out in outputs:
        name = identifier(out.get('name'))
        if name in names:
            raise Conflict('Duplicate output name')
        names.add(name)
        if not isinstance(out.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', out['sha256']):
            raise ContractError('Output hash required')
        integer(out.get('bytes'), 1, 100_000_000, 'output bytes')
    # Evidence authenticity and actual bytes are checked by trusted runtime adapters,
    # not established just because this JSON validates.
