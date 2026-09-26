"""Durable single-host reference scheduler and write journal.

No network, model calls, remote auth, or human identity verification is provided.
A runtime adapter, not Codex prose, invokes this API after validating real evidence.
Unknown calls consume budget and are never automatically resubmitted.
"""
from __future__ import annotations
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from .contracts import (Blocked, Conflict, ContractError, canonical, checked_plan,
                        digest, identifier, sha, validate_receipt)

class Runtime:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS runs(
            id TEXT PRIMARY KEY, mode TEXT NOT NULL, plan TEXT NOT NULL,
            plan_hash TEXT NOT NULL, max_calls INTEGER NOT NULL, spent INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS steps(
            run_id TEXT NOT NULL REFERENCES runs(id), id TEXT NOT NULL, idx INTEGER NOT NULL,
            kind TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', output TEXT,
            PRIMARY KEY(run_id,id));
        CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, step_id TEXT NOT NULL,
            attempt INTEGER NOT NULL, state TEXT NOT NULL,
            request TEXT NOT NULL, request_hash TEXT NOT NULL,
            origin TEXT NOT NULL, receipt TEXT, outcome_evidence TEXT,
            UNIQUE(run_id,step_id,attempt),
            FOREIGN KEY(run_id,step_id) REFERENCES steps(run_id,id));
        CREATE TABLE IF NOT EXISTS writes(
            id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, state TEXT NOT NULL,
            receipt TEXT, evidence TEXT);
        CREATE TABLE IF NOT EXISTS events(
            seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            event TEXT NOT NULL, detail TEXT NOT NULL);
        ''')

    def close(self):
        self.db.close()

    @contextmanager
    def txn(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        else:
            self.db.execute('COMMIT')

    def _event(self, run: str, event: str, detail: dict):
        self.db.execute('INSERT INTO events(run_id,event,detail) VALUES(?,?,?)',
                        (run, event, canonical(detail)))

    def _run(self, run_id: str):
        r = self.db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if r is None:
            raise ContractError('Unknown run')
        return r

    def _step(self, run_id: str, step_id: str):
        s = self.db.execute('SELECT * FROM steps WHERE run_id=? AND id=?', (run_id,step_id)).fetchone()
        if s is None:
            raise ContractError('Unknown step')
        return s

    def create(self, plan: dict) -> str:
        p = checked_plan(plan)
        with self.txn():
            old = self.db.execute('SELECT * FROM runs WHERE id=?', (p['run_id'],)).fetchone()
            if old:
                if old['plan_hash'] != digest(p):
                    raise Conflict('Frozen run changed; create a new revision')
                return 'existing'
            self.db.execute('INSERT INTO runs(id,mode,plan,plan_hash,max_calls) VALUES(?,?,?,?,?)',
                            (p['run_id'],p['mode'],canonical(p),digest(p),p['max_image_calls']))
            for i, step in enumerate(p['steps']):
                self.db.execute('INSERT INTO steps(run_id,id,idx,kind) VALUES(?,?,?,?)',
                                (p['run_id'],step['id'],i,step['kind']))
            self._event(p['run_id'],'run_created',{'plan_hash':digest(p)})
        return 'created'

    def plan(self, run_id: str) -> dict:
        return json.loads(self._run(run_id)['plan'])

    def next(self, run_id: str) -> dict:
        self._run(run_id)
        row = self.db.execute('SELECT * FROM steps WHERE run_id=? AND state!=? ORDER BY idx LIMIT 1',
                              (run_id,'succeeded')).fetchone()
        if not row:
            return {'state':'completed','step':None}
        step = dict(row)
        if step['state'] == 'pending':
            earlier = self.db.execute('SELECT COUNT(*) FROM steps WHERE run_id=? AND idx<? AND state!=?',
                                      (run_id,step['idx'],'succeeded')).fetchone()[0]
            if earlier:
                return {'state':'blocked','step':step['id']}
        return {'state':step['state'],'step':step['id'],'kind':step['kind']}

    def _ready(self, run_id: str, step_id: str):
        n = self.next(run_id)
        if n['step'] != step_id or n['state'] != 'pending':
            raise Blocked('Step not ready; preserve waiting/unknown state')

    def dispatch(self, run_id: str, step_id: str, *, authorization: dict, origin: str) -> dict:
        with self.txn():
            self._ready(run_id,step_id)
            r, s = self._run(run_id), self._step(run_id,step_id)
            if s['kind'] not in ('codex_brief','image_generate'):
                raise ContractError('Not an external worker node')
            if origin not in ('native','mock') or (r['mode']=='production' and origin!='native'):
                raise Blocked('Unsupported origin or mock in production')
            if authorization.get('run_id') != run_id or authorization.get('plan_hash') != r['plan_hash']:
                raise Blocked('Authorization must bind run and frozen plan')
            if authorization.get('approved') is not True or not authorization.get('actor_id'):
                raise Blocked('Explicit, attributed authorization required')
            if s['kind']=='image_generate':
                if authorization.get('max_image_calls') != r['max_calls']:
                    raise Blocked('Authorization budget mismatch')
                if r['spent'] >= r['max_calls']:
                    raise Blocked('Image call budget exhausted')
            attempt = self.db.execute('SELECT COUNT(*) FROM jobs WHERE run_id=? AND step_id=?',
                                      (run_id,step_id)).fetchone()[0]+1
            p = json.loads(r['plan'])
            definition = next(x for x in p['steps'] if x['id']==step_id)
            upstream = {x['id']: json.loads(x['output']) for x in self.db.execute(
                'SELECT id,output FROM steps WHERE run_id=? AND state=? AND output IS NOT NULL',
                (run_id,'succeeded'))}
            req = {'run_id':run_id,'step_id':step_id,'mode':r['mode'],
                   'plan_hash':r['plan_hash'],'definition':definition,'attempt':attempt,'upstream':upstream}
            job_id = 'job_' + digest(req)[:28]
            self.db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)',
                (job_id,run_id,step_id,attempt,'waiting',canonical(req),digest(req),origin,None,None))
            if s['kind']=='image_generate':
                self.db.execute('UPDATE runs SET spent=spent+1 WHERE id=?',(run_id,))
            self.db.execute('UPDATE steps SET state=? WHERE run_id=? AND id=?',
                            ('waiting_worker',run_id,step_id))
            self._event(run_id,'dispatched',{'job_id':job_id,'step':step_id,'origin':origin})
        return {'job_id':job_id,'request_hash':digest(req),'request':req,'origin':origin}

    def job(self, job_id: str) -> dict:
        row = self.db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not row:
            raise ContractError('Unknown job')
        r = dict(row)
        r['job_id'] = r['id']
        return r

    def accept(self, job_id: str, receipt: dict, artifacts: dict[str,bytes]) -> str:
        with self.txn():
            job = self.job(job_id)
            validate_receipt(receipt,job,self._run(job['run_id'])['mode'],job['origin'])
            if job['state']=='succeeded':
                if job['receipt'] != canonical(receipt):
                    raise Conflict('Different receipt for completed job')
                self._check_bytes(receipt, artifacts)
                return 'existing'
            if job['state'] not in ('waiting','unknown'):
                raise Blocked('Job already resolved as unsuccessful')
            self._check_bytes(receipt,artifacts)
            self.db.execute('UPDATE jobs SET state=?,receipt=? WHERE id=?',
                            ('succeeded',canonical(receipt),job_id))
            self.db.execute('UPDATE steps SET state=?,output=? WHERE run_id=? AND id=?',
                ('succeeded',canonical(receipt),job['run_id'],job['step_id']))
            self._event(job['run_id'],'receipt_accepted',{'job_id':job_id,'receipt_hash':digest(receipt)})
        return 'accepted'

    @staticmethod
    def _check_bytes(receipt, artifacts):
        if set(artifacts) != {x['name'] for x in receipt['outputs']}:
            raise Conflict('Receipt and actual file sets differ')
        for o in receipt['outputs']:
            b = artifacts[o['name']]
            if not isinstance(b,bytes) or sha(b)!=o['sha256'] or len(b)!=o['bytes']:
                raise Conflict('Artifact hash/length mismatch')

    def mark_unknown(self, job_id: str, evidence: str):
        if not evidence:
            raise ContractError('Evidence reference required')
        with self.txn():
            j = self.job(job_id)
            if j['state'] not in ('waiting','unknown'):
                raise Blocked('Cannot make resolved job unknown')
            self.db.execute('UPDATE jobs SET state=?,outcome_evidence=? WHERE id=?',('unknown',evidence,job_id))
            self.db.execute('UPDATE steps SET state=? WHERE run_id=? AND id=?',('unknown',j['run_id'],j['step_id']))
            self._event(j['run_id'],'unknown',{'job_id':job_id,'evidence':evidence})

    def prove_no_effect(self, job_id: str, *, worker_terminal: bool, no_effect_verified: bool, evidence: str):
        # An empty GET is not evidence of terminal failure. Caller must establish BOTH
        # no surviving worker and a definitive no-effect reconciliation.
        if worker_terminal is not True or no_effect_verified is not True or not evidence:
            raise Blocked('Absent-now alone is insufficient to retry')
        with self.txn():
            j = self.job(job_id)
            if j['state']!='unknown':
                raise Blocked('Only unknown jobs can be resolved here')
            self.db.execute('UPDATE jobs SET state=?,outcome_evidence=? WHERE id=?',('failed_confirmed',evidence,job_id))
            self.db.execute('UPDATE steps SET state=? WHERE run_id=? AND id=?',('pending',j['run_id'],j['step_id']))
            # Budget is NOT refunded; even a failed result may have consumed usage.
            self._event(j['run_id'],'failure_confirmed',{'job_id':job_id,'evidence':evidence})

    def finish_local(self, run_id: str, step_id: str, output: dict):
        with self.txn():
            self._ready(run_id,step_id)
            s = self._step(run_id,step_id)
            if s['kind'] in ('image_generate','codex_brief','human_review'):
                raise Blocked('External/review nodes cannot use local completion')
            self.db.execute('UPDATE steps SET state=?,output=? WHERE run_id=? AND id=?',
                            ('succeeded',canonical(output),run_id,step_id))
            self._event(run_id,'local_complete',{'step':step_id,'output_hash':digest(output)})

    def wait_review(self, run_id: str, step_id: str):
        with self.txn():
            self._ready(run_id,step_id)
            if self._step(run_id,step_id)['kind']!='human_review':
                raise ContractError('Not a review step')
            self.db.execute('UPDATE steps SET state=? WHERE run_id=? AND id=?',('waiting_review',run_id,step_id))

    def finish_review(self, run_id: str, step_id: str, *, validated_snapshot: dict):
        # Trusted boundary: live adapter independently verifies reviewer identity,
        # scope, latest version and candidate set BEFORE calling this method.
        from .review import validate_review_snapshot
        validate_review_snapshot(validated_snapshot, self._run(run_id)['mode'])
        if validated_snapshot['run_id']!=run_id:
            raise Conflict('Wrong run review')
        with self.txn():
            if self._step(run_id,step_id)['state']!='waiting_review':
                raise Blocked('Run is not waiting for review')
            self.db.execute('UPDATE steps SET state=?,output=? WHERE run_id=? AND id=?',
                ('succeeded',canonical(validated_snapshot),run_id,step_id))
            self._event(run_id,'review_complete',{'snapshot_hash':digest(validated_snapshot)})

    def begin_write(self, operation_id: str, payload: dict) -> str:
        identifier(operation_id)
        with self.txn():
            old = self.db.execute('SELECT * FROM writes WHERE id=?',(operation_id,)).fetchone()
            if old:
                if old['payload_hash']!=digest(payload):
                    raise Conflict('Write id reused with different payload')
                if old['state']=='confirmed':
                    return 'existing'
                raise Blocked('Write pending/unknown; reconcile, never resend')
            self.db.execute('INSERT INTO writes VALUES(?,?,?,?,?)',
                            (operation_id,digest(payload),'pending',None,None))
        return 'reserved'

    def unknown_write(self, operation_id: str):
        with self.txn():
            cur = self.db.execute('UPDATE writes SET state=? WHERE id=? AND state=?',
                                 ('unknown',operation_id,'pending'))
            if cur.rowcount!=1:
                raise Blocked('Write not pending')

    def confirm_write(self, operation_id: str, *, expected_hash: str, observed_hash: str, receipt: dict):
        if expected_hash!=observed_hash or not receipt:
            raise Conflict('Read-back did not confirm intended effect')
        with self.txn():
            row = self.db.execute('SELECT * FROM writes WHERE id=?',(operation_id,)).fetchone()
            if not row:
                raise ContractError('Unjournaled write')
            if expected_hash != row['payload_hash']:
                raise Conflict('Proof hash does not match journaled payload')
            if row['state']=='confirmed':
                if row['receipt']!=canonical(receipt):
                    raise Conflict('Confirmed write receipt changed')
                return 'existing'
            self.db.execute('UPDATE writes SET state=?,receipt=? WHERE id=?',
                            ('confirmed',canonical(receipt),operation_id))
        return 'confirmed'

    def status(self, run_id: str):
        r = self._run(run_id)
        return {'run_id':run_id,'mode':r['mode'],'next':self.next(run_id),
                'image_calls_reserved':r['spent'],'image_calls_max':r['max_calls'],
                'unknown_jobs':[x[0] for x in self.db.execute('SELECT id FROM jobs WHERE run_id=? AND state=?',(run_id,'unknown'))]}
