"""Evidence-bound metric normalization; no universal CVR and no zero-fill for unknown."""
import datetime as dt
import re
from decimal import Decimal, InvalidOperation
from .contracts import Blocked, Conflict, ContractError, canonical, digest

COUNTS = ('impressions','clicks','orders')

def count(value, key):
    if value is None:
        return None
    if type(value) is bool:
        raise ContractError(key + ': booleans are not counts')
    try:
        n = Decimal(str(value))
    except InvalidOperation as exc:
        raise ContractError(key + ': invalid numeric value') from exc
    if not n.is_finite() or n<0 or n!=n.to_integral_value() or n>10**15:
        raise ContractError(key + ': finite nonnegative integer required')
    return int(n)

def normalize(row: dict, usage: dict) -> dict:
    p = dict(row)
    if usage.get('mode') not in ('demo','production') or p.get('mode')!=usage['mode']:
        raise Blocked('Metric mode must match usage')
    if p.get('usage_id')!=usage.get('usage_id') or p.get('asset_id')!=usage.get('asset_id'):
        raise Conflict('Metric does not belong to usage/asset')
    expected = 'simulated' if p['mode']=='demo' else 'observed'
    if p.get('data_nature') != expected:
        raise Blocked('Simulated/real data mismatch')
    required = ('source_sha256','channel','account_id','placement','date_from','date_to',
                'timezone','attribution_window','definition_version')
    if any(not isinstance(p.get(k),str) or not p[k].strip() for k in required):
        raise ContractError('Incomplete metric dimensions or source evidence')
    if not re.fullmatch('[0-9a-f]{64}',p['source_sha256']):
        raise ContractError('Source hash must be SHA256')
    try:
        lo,hi = dt.date.fromisoformat(p['date_from']),dt.date.fromisoformat(p['date_to'])
    except ValueError as exc:
        raise ContractError('Use ISO dates') from exc
    if lo>hi:
        raise ContractError('Reversed date interval')
    for k in ('channel','account_id','placement'):
        if p[k]!=usage.get(k):
            raise Conflict('Usage dimension changed: '+k)
    for k in COUNTS:
        p[k] = count(p.get(k),k)
    p['ctr'] = None if p['impressions'] in (None,0) or p['clicks'] is None else p['clicks']/p['impressions']
    p['orders_per_click'] = None if p['clicks'] in (None,0) or p['orders'] is None else p['orders']/p['clicks']
    return p

class MetricBook:
    """Offline idempotency reference. Live integration must persist the same keys."""
    def __init__(self):
        self.rows = {}
    def import_one(self, row: dict, usage: dict) -> str:
        p = normalize(row,usage)
        dims = ('mode','usage_id','asset_id','channel','account_id','placement','date_from','date_to',
                'timezone','attribution_window','definition_version','source_sha256')
        key = digest({k:p[k] for k in dims})
        old = self.rows.get(key)
        if old:
            if canonical(old)!=canonical(p):
                raise Conflict('Same source key with different data')
            return 'existing'
        # A revised source is a separate version, not a silent overwrite.
        self.rows[key]=p
        return 'created'
    def query(self, mode: str):
        if mode not in ('demo','production'):
            raise ContractError('Explicit mode required')
        return [x for x in self.rows.values() if x['mode']==mode]
