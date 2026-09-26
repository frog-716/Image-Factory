"""Separate demonstration-use consent from factual commercial approval.

No live identities are authenticated here. The Feishu adapter must extract actors
from trusted API fields and allowlists rather than accept agent-written labels.
"""
from .contracts import Blocked, Conflict, ContractError, identifier

def latest_reviews(events: list[dict]) -> dict[str, dict]:
    latest = {}
    seen = set()
    for e in events:
        aid = identifier(e.get('asset_id'))
        revision = e.get('revision')
        if type(revision) is not int or revision < 1:
            raise ContractError('Trusted monotonic revision required')
        if (aid,revision) in seen:
            raise Conflict('Conflicting or duplicated review revision')
        seen.add((aid,revision))
        if aid not in latest or revision > latest[aid]['revision']:
            latest[aid] = e
    return latest

def validate_review_snapshot(snapshot: dict, mode: str) -> dict:
    if snapshot.get('mode') != mode or mode not in ('demo','production'):
        raise Blocked('Review scope mismatch')
    identifier(snapshot.get('run_id'))
    candidates = snapshot.get('candidates')
    if not isinstance(candidates,list) or not candidates:
        raise ContractError('Candidate set required')
    ids = [identifier(c.get('asset_id')) for c in candidates]
    if len(ids)!=len(set(ids)):
        raise Conflict('Duplicate candidates')
    latest = latest_reviews(snapshot.get('events',[]))
    if set(latest) != set(ids):
        raise Blocked('All candidates must have review; unknown extra assets forbidden')
    approved, rejected = [], []
    for c in candidates:
        r = latest[c['asset_id']]
        if r.get('run_id')!=snapshot['run_id'] or r.get('mode')!=mode:
            raise Blocked('Review belongs to different run or mode')
        if r.get('sha256')!=c.get('sha256') or not c.get('sha256'):
            raise Conflict('Review must bind exact current asset bytes')
        if r.get('decision') not in ('approve','reject'):
            raise Blocked('Pending/revoked review needs a new decision')
        if not isinstance(r.get('actor_id'),str) or not r['actor_id'].strip() or r.get('human_confirmed') is not True:
            raise Blocked('Human actor and explicit confirmation required')
        if not isinstance(r.get('reason'),str) or not r['reason'].strip():
            raise Blocked('Review reason required')
        if r['decision']=='approve':
            fields = ('demo_visual_ok','demo_use_only') if mode=='demo' else ('product_accuracy','brand_channel_ok')
            if any(r.get(f) is not True for f in fields):
                raise Blocked('Required checks incomplete: ' + ', '.join(fields))
            approved.append(c['asset_id'])
        else:
            rejected.append(c['asset_id'])
    return {'approved':approved,'rejected':rejected,'latest':latest}
