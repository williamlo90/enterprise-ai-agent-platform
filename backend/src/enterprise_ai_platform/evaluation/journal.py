"""Owned local evaluation ledger: reserve before egress, retain unknown spend.

Typed observations may contain fictional source text. This private artifact is
not an operational log or an authorization mechanism for real customer data.
"""
import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from enterprise_ai_platform.evaluation.rag import Attempt,EvaluationError,Observation,configuration_hash

class BudgetExceeded(EvaluationError):pass

class RunJournal:
    APPLICATION_ID=1163280726
    def __init__(self,path:Path,manifest):
        self.path=path;self.fingerprint=configuration_hash(manifest);self.limit=Decimal(str(manifest.budget_limit_usd)) if manifest.budget_limit_usd is not None else None
        self.max_attempts=manifest.max_inference_attempts
        path.parent.mkdir(parents=True,exist_ok=True)
        existed=path.exists()
        with self.connect() as db:
            marker=db.execute('PRAGMA application_id').fetchone()[0]
            if existed and marker!=self.APPLICATION_ID:raise EvaluationError('journal_not_owned')
            db.execute(f'PRAGMA application_id={self.APPLICATION_ID}')
            db.execute('CREATE TABLE IF NOT EXISTS meta(fingerprint TEXT PRIMARY KEY, budget TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY, case_id TEXT NOT NULL, run INTEGER NOT NULL, kind TEXT NOT NULL, reserved TEXT NOT NULL, state TEXT NOT NULL, actual TEXT, input_tokens INTEGER, output_tokens INTEGER, outcome TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS observations(case_id TEXT NOT NULL, run INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(case_id,run))')
            db.execute('CREATE TABLE IF NOT EXISTS case_claims(case_id TEXT NOT NULL, run INTEGER NOT NULL, request_sha256 TEXT NOT NULL, PRIMARY KEY(case_id,run))')
            db.execute('CREATE TABLE IF NOT EXISTS partial_outputs(case_id TEXT NOT NULL, run INTEGER NOT NULL, sequence INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(case_id,run,sequence))')
            db.execute('CREATE TABLE IF NOT EXISTS phase_stop(id INTEGER PRIMARY KEY CHECK(id=1),reason TEXT NOT NULL)')
            rows=db.execute('SELECT fingerprint,budget FROM meta').fetchall()
            expected=(self.fingerprint,str(self.limit) if self.limit is not None else None)
            if rows and rows!=[expected]:raise EvaluationError('journal_configuration_mismatch')
            db.execute('INSERT OR IGNORE INTO meta VALUES(?,?)',expected)
            for attempt in manifest.preprocessing_attempts:
                reserved=attempt.cost_usd if attempt.cost_usd is not None else attempt.unknown_reserved_estimate_usd
                if reserved is None:raise EvaluationError('preprocessing_cost_unknown_without_reservation')
                existing=db.execute('SELECT id FROM attempts WHERE id=?',(attempt.attempt_id,)).fetchone()
                if not existing:db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?)',(attempt.attempt_id,'__preprocessing__',0,attempt.kind,str(reserved),'known' if attempt.cost_usd is not None else 'unknown',str(attempt.cost_usd) if attempt.cost_usd is not None else None,attempt.input_tokens,attempt.output_tokens,attempt.outcome))
    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=10);db.execute('PRAGMA synchronous=FULL')
        try:
            with db:yield db
        finally:db.close()
    @staticmethod
    def exposure_in(db):return sum((Decimal(actual if state=='known' else reserved) for state,reserved,actual in db.execute('SELECT state,reserved,actual FROM attempts')),Decimal(0))
    def exhausted(self):
        with self.connect() as db:return self.limit is not None and self.exposure_in(db)>=self.limit
    def request_stop(self,reason):
        if reason not in {'budget_reservation_rejected','phase_attempt_limit','reservation_overrun_review_required','ledger_failure','corpus_transferred','provider_scope_rejected'}:raise EvaluationError('invalid_phase_stop')
        with self.connect() as db:db.execute('INSERT OR IGNORE INTO phase_stop VALUES(1,?)',(reason,))
    def stop_requested(self):
        with self.connect() as db:
            row=db.execute('SELECT reason FROM phase_stop WHERE id=1').fetchone()
            if row:return row[0]
            if any(state=='known' and Decimal(actual)>Decimal(reserved) for state,reserved,actual in db.execute('SELECT state,reserved,actual FROM attempts')):return 'reservation_overrun_review_required'
            if self.max_attempts is not None and db.execute('SELECT count(*) FROM attempts').fetchone()[0]>=self.max_attempts:return 'phase_attempt_limit'
            if self.limit is not None and self.exposure_in(db)>=self.limit:return 'budget_reservation_rejected'
            return None
    def reserve(self,case_id,run,kind,reserved_usd):
        amount=Decimal(str(reserved_usd))
        if not amount.is_finite() or amount<0 or kind not in ['chat','query_embedding','document_embedding']:raise EvaluationError('invalid_reservation')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.max_attempts is not None and db.execute('SELECT count(*) FROM attempts').fetchone()[0]>=self.max_attempts:
                raise BudgetExceeded('phase_attempt_limit')
            if any(state=='known' and Decimal(actual)>Decimal(reserved) for state,reserved,actual in db.execute('SELECT state,reserved,actual FROM attempts')):
                raise BudgetExceeded('reservation_overrun_review_required')
            if self.limit is not None and self.exposure_in(db)+amount>self.limit:raise BudgetExceeded('budget_reservation_rejected')
            attempt_id=str(uuid4());db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?)',(attempt_id,case_id,run,kind,str(amount),'unknown',None,None,None,'unknown'));return attempt_id
    def settle(self,attempt_id,*,cost_usd,input_tokens=None,output_tokens=None,outcome='ok'):
        Attempt(attempt_id=attempt_id,kind='chat',outcome=outcome,input_tokens=input_tokens,output_tokens=output_tokens,cost_usd=cost_usd)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');row=db.execute('SELECT state,actual,input_tokens,output_tokens,outcome FROM attempts WHERE id=?',(attempt_id,)).fetchone()
            if row is None:raise EvaluationError('unreserved_attempt')
            actual=str(cost_usd) if cost_usd is not None else None
            if row[0]=='known':
                if (row[1],row[2],row[3],row[4])!=(actual,input_tokens,output_tokens,outcome):raise EvaluationError('settlement_conflict')
                return
            db.execute('UPDATE attempts SET state=?,actual=?,input_tokens=?,output_tokens=?,outcome=? WHERE id=?',('known' if actual is not None else 'unknown',actual,input_tokens,output_tokens,outcome,attempt_id))
    def attempts(self,case_id,run):
        with self.connect() as db:rows=db.execute('SELECT id,kind,reserved,state,actual,input_tokens,output_tokens,outcome FROM attempts WHERE case_id=? AND run=? ORDER BY rowid',(case_id,run)).fetchall()
        return [Attempt(attempt_id=ident,kind=kind,input_tokens=inp,output_tokens=out,cost_usd=float(actual) if state=='known' else None,unknown_reserved_estimate_usd=float(reserved) if state!='known' else None,outcome=outcome) for ident,kind,reserved,state,actual,inp,out,outcome in rows]
    def append(self,observation):
        if observation.configuration_sha256!=self.fingerprint:raise EvaluationError('journal_observation_configuration')
        with self.connect() as db:db.execute('INSERT INTO observations VALUES(?,?,?)',(observation.case_id,observation.run,observation.model_dump_json()))
    def claim_case(self,case_id,run,request_sha256):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous=db.execute('SELECT request_sha256 FROM case_claims WHERE case_id=? AND run=?',(case_id,run)).fetchone()
            if previous is not None:
                if previous[0]!=request_sha256:raise EvaluationError('case_request_changed')
                return False
            db.execute('INSERT INTO case_claims VALUES(?,?,?)',(case_id,run,request_sha256));return True
    def save_partial(self,case_id,run,output):
        from enterprise_ai_platform.evaluation.rag import Output
        if type(run) is not int or not 1<=run<=3 or not isinstance(output,Output):raise EvaluationError('partial_output_scope')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM case_claims WHERE case_id=? AND run=?',(case_id,run)).fetchone():raise EvaluationError('partial_without_claim')
            n=db.execute('SELECT count(*) FROM partial_outputs WHERE case_id=? AND run=?',(case_id,run)).fetchone()[0]
            if n>=3:raise EvaluationError('partial_output_limit')
            db.execute('INSERT INTO partial_outputs VALUES(?,?,?,?)',(case_id,run,n,output.model_dump_json()))
    def partials(self,case_id,run):
        from enterprise_ai_platform.evaluation.rag import Output
        with self.connect() as db:rows=db.execute('SELECT body FROM partial_outputs WHERE case_id=? AND run=? ORDER BY sequence',(case_id,run)).fetchall()
        return [Output.model_validate_json(r[0]) for r in rows]

    def get(self,case_id,run):
        with self.connect() as db:row=db.execute('SELECT body FROM observations WHERE case_id=? AND run=?',(case_id,run)).fetchone()
        return Observation.model_validate_json(row[0]) if row else None

class CallGuard:
    """Trusted provider adapters must reserve through this guard before each SDK call.

    Token bounds come from the fixed model adapter, not model-generated input.
    Provider RunPort installation remains explicit and subject to source review.
    """
    def __init__(self,journal,case_id,run,rates):self.journal,self.case_id,self.run,self.rates=journal,case_id,run,rates
    def reserve(self,kind,input_tokens,output_tokens):
        if type(input_tokens) is not int or type(output_tokens) is not int or input_tokens<0 or output_tokens<0:raise EvaluationError('token_bound')
        prefix='chat' if kind=='chat' else 'embedding'
        amount=(Decimal(str(self.rates[prefix+'_input']))*input_tokens+Decimal(str(self.rates[prefix+'_output']))*output_tokens)/1000000
        return self.journal.reserve(self.case_id,self.run,kind,amount)
    def settle(self,*args,**kwargs):return self.journal.settle(*args,**kwargs)
