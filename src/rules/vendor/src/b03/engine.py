"""Pure-Python offline discrete-event controller and flow allocation environment.

The driver owns future events. Decisions receive copies of current visible state.
ACK, configuration readback, and analytical business QoS are separate observations.
Recovery covers committed event-boundary snapshots of this offline environment.
It does not claim transactions with external devices or an official simulator.
"""
from __future__ import annotations

import copy
import hashlib
import heapq
import json
import math
import os
import time
import uuid

import b03_formulas as formula
from .solver import (EPS, QOS, active_requests, assess, loads, path_errors,
                     solve, usable)

from .identity import PROFILE, PROFILE_ID, SEMANTICS_ID, CAPABILITY_ID, CONFIG_SCHEMA, public_identity
DEFAULT_CONFIG = {'profile':PROFILE, 'schema_version':CONFIG_SCHEMA, 'semantics_id':SEMANTICS_ID, 'capability_id':CAPABILITY_ID, 'path_limit':4, 'path_expansion_budget':2000,
                  'search_budget':2000, 'search_service_limit':12, 'max_path_hops':None,
                  'protected_service_ids':[], 'observation_delay_sec':0.2,
                  'planning_delay_sec':0.5, 'action_delay_sec':0.1,
                  'readback_delay_sec':0.1, 'retry_delay_sec':0.1,
                  'stability_window_sec':10.0, 'tail_sec':30.0,
                  'max_control_cycles':200, 'max_replans_per_workorder':3,
                  'objective_version':'priority-classes-then-min-changes-v1',
                  'faults':[]}


def number(v, name, lower=0, upper=None):
    if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<lower or (upper is not None and v>upper):
        raise ValueError('INVALID_NUMBER:'+name)
    return v


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def resolved_config(config=None):
    result=copy.deepcopy(DEFAULT_CONFIG)
    supplied=copy.deepcopy(config or {})
    unknown=set(supplied)-set(DEFAULT_CONFIG)-{'scenario_key','script_id','schema_version'}
    if unknown: raise ValueError('UNKNOWN_CONFIG_KEYS:'+','.join(sorted(unknown)))
    result.update(supplied)
    if not isinstance(result['profile'],dict): raise ValueError('PROFILE_REQUIRED')
    if config is not None and not {'profile','schema_version','semantics_id','capability_id'}<=set(supplied): raise ValueError('EXPLICIT_IDENTITY_REQUIRED')
    if result['profile']!=PROFILE: raise ValueError('UNSUPPORTED_OR_UNRESOLVED_PROFILE')
    if (result['schema_version'],result['semantics_id'],result['capability_id'])!=(CONFIG_SCHEMA,SEMANTICS_ID,CAPABILITY_ID): raise ValueError('SEMANTIC_IDENTITY_MISMATCH')
    for key in ('path_limit','path_expansion_budget','search_budget','search_service_limit','max_control_cycles','max_replans_per_workorder'):
        number(result[key],key,1)
        if not isinstance(result[key],int): raise ValueError('INTEGER_REQUIRED:'+key)
    for key in ('observation_delay_sec','planning_delay_sec','action_delay_sec','readback_delay_sec','retry_delay_sec','stability_window_sec','tail_sec'):
        number(result[key],key)
    if result['tail_sec']<result['stability_window_sec']: raise ValueError('TAIL_SHORTER_THAN_STABILITY_WINDOW')
    if result['max_path_hops'] is not None:
        number(result['max_path_hops'],'max_path_hops',1)
        if not isinstance(result['max_path_hops'],int): raise ValueError('INTEGER_REQUIRED:max_path_hops')
    if not isinstance(result['protected_service_ids'],list) or not all(isinstance(x,str) for x in result['protected_service_ids']):
        raise ValueError('INVALID_PROTECTED_IDS')
    if not isinstance(result['faults'],list): raise ValueError('INVALID_FAULTS')
    allowed={'stale','readback_missing','action_failure','ack_loss'}
    for fault in result['faults']:
        if not isinstance(fault,dict) or fault.get('kind') not in allowed: raise ValueError('UNSUPPORTED_FAULT_HOOK')
        if set(fault)-{'kind','event_ordinal','workorder_index','action_index','once'}: raise ValueError('UNKNOWN_FAULT_FIELDS')
    return result


def _identifier(obj,key):
    if not isinstance(obj.get(key),str) or not obj[key]: raise ValueError('INVALID_ID:'+key)
    return obj[key]


def _qos(params, name):
    if 'bandwitdhBps' in params:
        if 'bandwidthBps' in params and params['bandwitdhBps']!=params['bandwidthBps']:
            raise ValueError('CONFLICTING_BANDWIDTH_KEYS:'+name)
        params['bandwidthBps']=params.pop('bandwitdhBps')
    for key in ('bandwidthBps',)+QOS:
        number(params.get(key),name+'.'+key,0,1 if key in ('ber','packetLossRate') else None)


def _request(req,nodes):
    _identifier(req,'serviceId')
    for key in ('srcNodeId','dstNodeId'):
        if req.get(key) not in nodes: raise ValueError('UNKNOWN_SERVICE_ENDPOINT:'+str(req.get(key)))
    _qos(req['requirement'],req['serviceId'])
    if req['requirement']['bandwidthBps']<=0: raise ValueError('POSITIVE_REQUEST_BANDWIDTH_REQUIRED')
    number(req.get('priority'), 'priority')
    number(req.get('startAtSec',0),'startAtSec')
    if req.get('endAtSec') is not None:
        number(req['endAtSec'],'endAtSec',req.get('startAtSec',0))


def _status(value):
    if value in (True,'up','online','UP','ONLINE'): return True
    if value in (False,'down','offline','DOWN','OFFLINE'): return False
    raise ValueError('UNSUPPORTED_STATUS_VALUE')


class Runtime:
    SNAPSHOT_FIELDS = (
        'config','method','run_id','nodes','links','requests','assignments',
        'initial_assignments','initial_audit','initial_metrics','sequence','now',
        'state_version','config_version','objective_revision','objective_version',
        'trace','intervals','workorders','ledger','pending_verifications','event_count',
        'unsupported','control_cycles','unknown_environment','stable_since','failure_rows',
        'solver_wall_seconds','input_event_count','endpoint','done','step_count',
        'commit_sequence','clock_sessions','downtime_seconds','completed_wall_seconds')

    def __init__(self,topology,requests,events,config=None,method='priority_greedy',run_id=None):
        if method not in ('keep_initial','priority_greedy','bounded_search'): raise ValueError('UNKNOWN_METHOD')
        if any(e.get('eventType')=='node_move' for e in events): raise ValueError('UNSUPPORTED_NODE_MOVE_PREFLIGHT')
        self.config=resolved_config(config); self.method=method
        self.run_id=run_id or 'b03-'+uuid.uuid4().hex[:12]
        from .persistence import code_fingerprints
        self.source_fingerprints=code_fingerprints()
        self.input_hash=digest({'topology':topology,'requests':requests,'events':events})
        self.config_hash=digest(self.config)
        self.done=False; self.step_count=0; self.commit_sequence=0; self._stepping=False
        self.wall_elapsed_base=0.0; self.downtime_seconds=0.0; self.completed_wall_seconds=None
        self.clock_sessions=[{'sessionId':uuid.uuid4().hex,'pid':os.getpid(),
                              'startedUnixSec':time.time(),'kind':'new_run'}]
        self.nodes={}; self.links={}; self.requests={}
        for raw in topology['nodes']:
            node=copy.deepcopy(raw); nid=_identifier(node,'nodeId')
            if nid in self.nodes: raise ValueError('DUPLICATE_NODE_ID')
            node['_online']=_status(node.get('status',True)); self.nodes[nid]=node
        for raw in topology['links']:
            link=copy.deepcopy(raw); lid=_identifier(link,'linkId')
            if lid in self.links: raise ValueError('DUPLICATE_LINK_ID')
            if any(link.get(k) not in self.nodes for k in ('srcNodeId','dstNodeId')): raise ValueError('UNKNOWN_LINK_ENDPOINT')
            _qos(link['params'],lid); link['_online']=_status(link.get('status',True)); self.links[lid]=link
        for raw in requests:
            req=copy.deepcopy(raw); _request(req,self.nodes); sid=req['serviceId']
            if sid in self.requests: raise ValueError('DUPLICATE_SERVICE_ID')
            self.requests[sid]=req
        if set(self.config['protected_service_ids'])-set(self.requests): raise ValueError('UNKNOWN_PROTECTED_SERVICE')
        # Only driver queue holds the event suffix; never put it in observations.
        self.queue=[]; self.sequence=0; self.now=0.0; self.state_version=0; self.config_version=1
        self.objective_revision=0; self.objective_version=self.config['objective_version']+'/r0'
        self.trace=[]; self.intervals=[]; self.workorders=[]; self.ledger={}; self.current=None
        self.pending_verifications=[]
        self.event_count=0; self.unsupported=[]; self.used_faults=set(); self.control_cycles=0
        self.unknown_environment=False
        self.stable_since={}; self.stable_confirmed=set(); self.failure_rows=[]
        self.wall_start=time.perf_counter(); self.solver_wall_seconds=0.0
        for i,raw in enumerate(events,1):
            event=copy.deepcopy(raw); number(event.get('time'),'event.time')
            if not isinstance(event.get('eventType'),str) or not isinstance(event.get('attrs'),dict): raise ValueError('INVALID_EVENT_SHAPE')
            self.push(event['time'],0,'external',{'event':event,'sourceOrdinal':i})
        self.input_event_count=len(events)
        self.endpoint=max((e['time'] for e in events),default=0)+self.config['tail_sec']
        for sid,r in self.requests.items():
            if 0<r.get('startAtSec',0)<=self.endpoint: self.push(r['startAtSec'],0,'lifecycle',{'serviceId':sid,'phase':'start','expectedTimeSec':r['startAtSec']})
            if r.get('endAtSec') is not None and 0<r['endAtSec']<=self.endpoint: self.push(r['endAtSec'],0,'lifecycle',{'serviceId':sid,'phase':'end','expectedTimeSec':r['endAtSec']})
        self.assignments,self.initial_audit=solve(self.nodes,self.links,self.requests,{sid:None for sid in self.requests},0,self.config,initialize=True)
        self.initial_assignments=copy.deepcopy(self.assignments)
        self.initial_metrics=self.metrics()
        if self.initial_metrics['overloadedLinkIds']: raise RuntimeError('INITIALIZATION_NOT_CAPACITY_FEASIBLE')
        self.emit('initial_deployment',initialAssignments=self.assignments,assessment=self.initial_metrics,
                  initialization='shared_priority_greedy_for_all_methods',solverAudit=self.initial_audit)
        self.update_stability()

    def push(self,when,priority,kind,payload):
        self.sequence+=1; heapq.heappush(self.queue,(float(when),priority,self.sequence,kind,payload))

    def emit(self,kind,**payload):
        row={**public_identity(),'runId':self.run_id,'traceId':self.current['traceId'] if self.current else self.run_id+'-environment',
             'scenarioKey':self.config.get('scenario_key'),'scriptId':self.config.get('script_id'),
             'workOrderId':self.current['workOrderId'] if self.current else None,
             'timeSimSec':self.now,'wallElapsedSec':self.wall_elapsed(),
             'stateVersion':self.state_version,'configVersion':self.config_version,
             'objectiveVersion':self.objective_version,'policyVersion':self.config['objective_version'],'kind':kind}
        row.update(copy.deepcopy(payload)); self.trace.append(row); return row

    def wall_elapsed(self):
        if self.completed_wall_seconds is not None: return self.completed_wall_seconds
        return self.wall_elapsed_base+time.perf_counter()-self.wall_start

    def export_checkpoint(self,*,commit_sequence=None):
        """Environment-private snapshot; safe only between complete step calls."""
        from .persistence import code_fingerprints, CheckpointError
        if self._stepping or getattr(self,'_step_failed',False): raise CheckpointError('NOT_AT_VALID_EVENT_BOUNDARY')
        if code_fingerprints()!=self.source_fingerprints: raise CheckpointError('SOURCE_CHANGED_DURING_RUN')
        if digest(self.config)!=self.config_hash: raise CheckpointError('CONFIG_CHANGED_DURING_RUN')
        state={name:copy.deepcopy(getattr(self,name)) for name in self.SNAPSHOT_FIELDS}
        if commit_sequence is not None: state['commit_sequence']=commit_sequence
        state['queue']=copy.deepcopy(self.queue)
        state['used_faults']=sorted(self.used_faults)
        state['stable_confirmed']=sorted(self.stable_confirmed)
        state['currentWorkOrderId']=self.current['workOrderId'] if self.current else None
        return {'schemaVersion':'b03.environment-private-checkpoint.rss.v1','visibility':'environment_private',
                'runId':self.run_id,'sourceFingerprints':copy.deepcopy(self.source_fingerprints),
                'inputHash':self.input_hash,'configHash':self.config_hash,'semanticIdentity':public_identity(),
                'savedAtUnixSec':time.time(),'activeWallSeconds':self.wall_elapsed(),'state':state}

    @classmethod
    def restore_checkpoint(cls,payload,*,expected_config_hash=None,expected_input_hash=None):
        """Restore committed state without initialization, admission, or event replay."""
        from .persistence import code_fingerprints, CheckpointError
        if payload.get('schemaVersion')!='b03.environment-private-checkpoint.rss.v1' or payload.get('visibility')!='environment_private':
            raise CheckpointError('PRIVATE_CHECKPOINT_SCHEMA_MISMATCH')
        if payload.get('sourceFingerprints')!=code_fingerprints(): raise CheckpointError('SOURCE_FINGERPRINT_MISMATCH')
        if payload.get('semanticIdentity')!=public_identity(): raise CheckpointError('SEMANTIC_IDENTITY_MISMATCH')
        state=payload['state']
        expected_keys=set(cls.SNAPSHOT_FIELDS)|{'queue','used_faults','stable_confirmed','currentWorkOrderId'}
        if set(state)!=expected_keys: raise CheckpointError('CHECKPOINT_STATE_FIELDS_MISMATCH')
        if digest(resolved_config(state['config']))!=payload['configHash']: raise CheckpointError('CONFIG_HASH_MISMATCH')
        if expected_config_hash is not None and expected_config_hash!=payload['configHash']: raise CheckpointError('EXPECTED_CONFIG_MISMATCH')
        if expected_input_hash is not None and expected_input_hash!=payload['inputHash']: raise CheckpointError('EXPECTED_INPUT_MISMATCH')
        if state['run_id']!=payload['runId'] or not 0<=state['now']<=state['endpoint']: raise CheckpointError('INVALID_RUN_OR_CLOCK')
        obj=cls.__new__(cls)
        for name in cls.SNAPSHOT_FIELDS: setattr(obj,name,copy.deepcopy(state[name]))
        obj.source_fingerprints=copy.deepcopy(payload['sourceFingerprints'])
        obj.input_hash=payload['inputHash']; obj.config_hash=payload['configHash']
        obj.used_faults=set(state['used_faults']); obj.stable_confirmed=set(state['stable_confirmed'])
        orders={w['workOrderId']:w for w in obj.workorders}
        if len(orders)!=len(obj.workorders): raise CheckpointError('DUPLICATE_WORKORDER_ID')
        for order in obj.workorders:
            attempts=order['attemptTimings']
            if len(attempts)!=order['attempts']: raise CheckpointError('ATTEMPT_TIMING_COUNT_MISMATCH')
            order['timings']=attempts[-1] if attempts else {}
        current_id=state['currentWorkOrderId']
        if current_id is not None and current_id not in orders: raise CheckpointError('MISSING_CURRENT_WORKORDER')
        obj.current=orders.get(current_id)
        pending={p['workOrderId']:p for p in obj.pending_verifications}
        if len(pending)!=len(obj.pending_verifications): raise CheckpointError('DUPLICATE_PENDING_VERIFICATION')
        obj.queue=[]
        for raw in state['queue']:
            when,priority,sequence,kind,item=copy.deepcopy(raw)
            if when<obj.now or sequence>obj.sequence: raise CheckpointError('INVALID_QUEUE_CLOCK_OR_SEQUENCE')
            if kind=='verify_stable':
                authoritative=pending.get(item['workOrderId'])
                if authoritative!=item: raise CheckpointError('PENDING_VERIFICATION_QUEUE_MISMATCH')
                item=authoritative
            obj.queue.append((when,priority,sequence,kind,item))
        if len({x[2] for x in obj.queue})!=len(obj.queue): raise CheckpointError('DUPLICATE_QUEUE_SEQUENCE')
        heapq.heapify(obj.queue)
        applied=[x for x in obj.trace if x['kind']=='configuration_applied']
        ids=[x['actionId'] for x in applied]
        actual={aid for aid,record in obj.ledger.items() if record['actualApplied']}
        if len(ids)!=len(set(ids)) or set(ids)!=actual or obj.config_version!=1+len(actual):
            raise CheckpointError('ENVIRONMENT_LEDGER_VERSION_MISMATCH')
        for aid,record in obj.ledger.items():
            if record['actionId']!=aid or digest(record['action'])!=record['payloadHash']:
                raise CheckpointError('LEDGER_PAYLOAD_MISMATCH')
        if obj.intervals and obj.intervals[-1]['endSec']!=obj.now: raise CheckpointError('EVALUATION_CURSOR_MISMATCH')
        obj.wall_elapsed_base=payload['activeWallSeconds']; obj.wall_start=time.perf_counter()
        downtime=time.time()-payload['savedAtUnixSec']
        obj.downtime_seconds+=max(0.0,downtime)
        obj.clock_sessions.append({'sessionId':uuid.uuid4().hex,'pid':os.getpid(),'startedUnixSec':time.time(),
            'kind':'resume','fromCommitSequence':obj.commit_sequence,'savedAtUnixSec':payload['savedAtUnixSec'],
            'downtimeSeconds':max(0.0,downtime),'wallClockAdjustmentDetected':downtime<0})
        obj._stepping=False; obj._step_failed=False
        return obj

    def metrics(self):
        result=assess(self.nodes,self.links,self.requests,self.assignments,self.now)
        result['unknownCount']=0
        if self.unknown_environment:
            result['unknownCount']=result['requestCount']; result['satisfiedCount']=0
            result['satisfiedFraction']=0.0 if result['requestCount'] else None
            result['satisfiedPriorityWeight']=0
            result['onlineNodePairConnectedFraction']=None
            result['originalActiveEndpointReachableCount']=None
            result['originalActiveEndpointReachableFraction']=None
            for row in result['services'].values():
                row['satisfied']=False; row['reasons'].append('UNKNOWN_ENVIRONMENT_AFTER_UNSUPPORTED_EVENT')
        return result

    def integrate_to(self,when):
        if when>self.now:
            metrics=self.metrics(); dt=when-self.now
            self.intervals.append({'startSec':self.now,'endSec':when,'durationSec':dt,
                'stateVersion':self.state_version,'configVersion':self.config_version,
                'requestSeconds':dt*metrics['requestCount'],
                'satisfiedBusinessSeconds':dt*metrics['satisfiedCount'],
                'unservedBusinessSeconds':dt*(metrics['requestCount']-metrics['satisfiedCount']),
                'unknownBusinessSeconds':dt*metrics['unknownCount'],
                'priorityRequestSeconds':dt*metrics['priorityWeightTotal'],
                'prioritySatisfiedSeconds':dt*metrics['satisfiedPriorityWeight'], 'assessment':metrics})
            self.now=when

    def fault(self,kind,action_index=None):
        for i,f in enumerate(self.config['faults']):
            if f['kind']!=kind or (f.get('once',True) and i in self.used_faults): continue
            if f.get('event_ordinal',self.event_count)!=self.event_count: continue
            if f.get('workorder_index',len(self.workorders))!=len(self.workorders): continue
            if 'action_index' in f and f['action_index']!=action_index: continue
            self.used_faults.add(i); self.emit('fault_injected',fault=f); return True
        return False

    def update_stability(self):
        measured=self.metrics()['services']
        for sid in list(self.stable_since):
            if sid not in measured or not measured[sid]['satisfied']:
                self.stable_since.pop(sid,None); self.stable_confirmed.discard(sid)
        for sid,d in measured.items():
            if d['satisfied'] and sid not in self.stable_since:
                self.stable_since[sid]=self.now
                self.push(self.now+self.config['stability_window_sec'],5,'stable',{'serviceId':sid,'since':self.now})

    def start_work(self,trigger):
        if self.current:
            self.current['dirty']=True
            self.current.setdefault('waitingTriggers',[]).append(copy.deepcopy(trigger))
            self.emit('control_request_waiting',trigger=trigger,owner='coordinator',
                      reason='single_serial_writer_arbitration',
                      resumeCondition='current workorder terminates or replans on current state',
                      blockedByWorkOrderId=self.current['workOrderId'])
            return
        if self.control_cycles>=self.config['max_control_cycles']:
            self.emit('control_budget_exhausted',trigger=trigger); return
        self.control_cycles+=1
        wid=self.run_id+'-work-%04d'%self.control_cycles
        self.current={'workOrderId':wid,'traceId':wid+'-trace','trigger':copy.deepcopy(trigger),
            'eventTimeSec':trigger.get('eventTimeSec',self.now),'createdAtSec':self.now,
            'status':'observing','attempts':0,'dirty':False,'actionIds':[],
            'attemptTimings':[],'timings':{},'outcomes':[]}
        self.workorders.append(self.current); self.emit('workorder_created',trigger=trigger)
        self.push(self.now+self.config['observation_delay_sec'],1,'observe',{'workOrderId':wid})

    def finish_work(self,status):
        wo=self.current; wo['status']=status; wo['controlFinishedAtSec']=self.now
        wo['recoveryOwner']='coordinator' if status.startswith('waiting') else 'agent_om' if status=='verification_pending_stability' else 'coordinator'
        wo['resumeCondition']=wo.get('waitingCondition',
            'next supported external observation' if status.startswith('waiting') else
            'complete original stability window' if status=='verification_pending_stability' else
            'new external state or new explicitly authorized workorder')
        if status!='verification_pending_stability': wo['finishedAtSec']=self.now
        wo['finalAssessment']=self.metrics(); self.emit('workorder_finished',status=status,assessment=wo['finalAssessment'])
        dirty=wo['dirty']; self.current=None
        if dirty and self.now<self.endpoint: self.start_work({'kind':'new_external_state_after_previous_work','eventTimeSec':self.now})

    def replan(self,reason):
        self.current['outcomes'].append({'timeSec':self.now,'status':reason})
        self.current['recoveryOwner']='agent_rc'; self.current['resumeCondition']='fresh observation and version-checked plan within remaining attempt budget'
        self.emit('replan_requested',reason=reason)
        if self.current['attempts']>=self.config['max_replans_per_workorder']:
            self.finish_work('waiting_external_event_replan_budget_exhausted'); return
        self.current['status']='observing'; self.current['dirty']=False
        self.push(self.now+self.config['retry_delay_sec'],1,'observe',{'workOrderId':self.current['workOrderId']})

    def external(self,item):
        e=item['event']; kind=e['eventType']; attrs=e['attrs']; supported=True; reason=None
        operation=attrs.get('op')
        effective_kind=kind
        effective_attrs=copy.deepcopy(attrs)
        if kind=='service_adjust' and operation is not None:
            effective_kind={'add':'service_add','update':'service_update','modify':'service_update',
                            'delete':'service_delete','remove':'service_remove'}.get(operation,'unsupported_service_op')
            effective_attrs.pop('op',None)
        attrs=effective_attrs; kind=effective_kind
        nodes=copy.deepcopy(self.nodes); links=copy.deepcopy(self.links); requests=copy.deepcopy(self.requests)
        try:
            if kind=='link_param_change':
                lid=attrs['linkId']; params=links[lid]['params']
                changes={k:v for k,v in attrs.items() if k!='linkId'}
                if set(changes)-set(('bandwidthBps','bandwitdhBps')+QOS): raise ValueError('UNKNOWN_LINK_PARAMETER')
                params.update(changes); _qos(params,lid)
            elif kind in ('node_offline','node_online'):
                if set(attrs)-{'nodeId','state'}: raise ValueError('UNKNOWN_NODE_STATUS_SCHEMA')
                if 'state' in attrs and _status(attrs['state'])!=(kind=='node_online'): raise ValueError('NODE_STATUS_CONFLICT')
                nodes[attrs['nodeId']]['_online']=kind=='node_online'
            elif kind in ('link_state_change','link_state'):
                keys=[k for k in ('status','state','up','online','isUp') if k in attrs]
                if len(keys)!=1 or set(attrs)-{'linkId',keys[0]}: raise ValueError('AMBIGUOUS_LINK_STATE_SCHEMA')
                links[attrs['linkId']]['_online']=_status(attrs[keys[0]])
            elif kind=='node_move':
                raise ValueError('UNSUPPORTED_NODE_MOVE_FAIL_CLOSED')
            elif kind in ('service_remove','service_delete'):
                if set(attrs)!={'serviceId'}: raise ValueError('UNKNOWN_SERVICE_DELETE_SCHEMA')
                requests[attrs['serviceId']]['_deleted']=True
            elif kind=='service_add':
                if 'service' in attrs and set(attrs)!={'service'}: raise ValueError('UNKNOWN_SERVICE_ADD_SCHEMA')
                req=copy.deepcopy(attrs.get('service',attrs)); req.setdefault('startAtSec',self.now); _request(req,nodes)
                if req['serviceId'] in requests: raise ValueError('DUPLICATE_SERVICE_ID')
                requests[req['serviceId']]=req
            elif kind in ('service_adjust','service_update'):
                sid=attrs['serviceId']; changes={k:v for k,v in attrs.items() if k!='serviceId'}
                if set(changes)-{'requirement','priority','startAtSec','endAtSec'}: raise ValueError('UNKNOWN_SERVICE_ADJUST_SCHEMA')
                if 'requirement' in changes:
                    requests[sid]['requirement'].update(changes.pop('requirement'))
                requests[sid].update(changes); _request(requests[sid],nodes)
            else: raise ValueError('UNSUPPORTED_EVENT_TYPE')
        except (KeyError,TypeError,ValueError) as exc:
            supported=False; reason=str(exc)
        self.event_count+=1
        if supported:
            previous_requests=self.requests
            self.nodes,self.links,self.requests=nodes,links,requests
            for sid in requests: self.assignments.setdefault(sid,None)
            self.state_version+=1
            if kind in ('service_add','service_adjust','service_update','service_delete','service_remove'):
                self.objective_revision+=1
                self.objective_version=self.config['objective_version']+'/r'+str(self.objective_revision)
            if kind in ('service_add','service_adjust','service_update'):
                sid=attrs.get('serviceId',attrs.get('service',{}).get('serviceId'))
                if sid in requests:
                    for field,phase in (('startAtSec','start'),('endAtSec','end')):
                        when=requests[sid].get(field)
                        if when is not None and self.now<when<=self.endpoint and previous_requests.get(sid,{}).get(field)!=when:
                            self.push(when,0,'lifecycle',{'serviceId':sid,'phase':phase,'expectedTimeSec':when})
        else:
            self.unsupported.append({'sourceOrdinal':item['sourceOrdinal'],'event':e,'reason':reason})
            self.unknown_environment=True; self.state_version+=1
        self.emit('external_event',sourceOrdinal=item['sourceOrdinal'],deliveredOrdinal=self.event_count,event=e,supported=supported,reason=reason)
        self.update_stability(); self.start_work({'kind':kind,'sourceOrdinal':item['sourceOrdinal'],'eventTimeSec':self.now})

    def observation(self):
        wo=self.current; wo['attempts']+=1; wo['dirty']=False; wo['status']='planning'
        # Each attempt owns its timestamps; retries must not subtract an earlier
        # attempt's first ACK/apply from a later attempt's submission time.
        timing={'attemptId':wo['workOrderId']+'-attempt-%02d'%wo['attempts'],
                'attemptNumber':wo['attempts'],'eventTimeSec':wo['eventTimeSec'],
                'observationAvailableSec':self.now,'planGeneratedSec':None,
                'submittedSec':None,'firstAckSec':None,'firstAppliedSec':None,
                'lastAppliedSec':None,'readbackSec':None,'businessMeasuredSec':None,
                'perceptionSec':self.now-wo['eventTimeSec'],'strategyGenerationSec':None,
                'dispatchToAckSec':None,'dispatchToApplySec':None,'dispatchToReadbackSec':None}
        wo['attemptTimings'].append(timing); wo['timings']=timing
        snapshot={'nodes':copy.deepcopy(self.nodes),'links':copy.deepcopy(self.links),'requests':copy.deepcopy(self.requests),
                  'assignments':copy.deepcopy(self.assignments),'stateVersion':self.state_version,'configVersion':self.config_version,
                  'objectiveVersion':self.objective_version,'observedAtSec':self.now}
        wo['_snapshot']=snapshot; wo['timings']['observationAvailableSec']=self.now
        wo['timings']['perceptionSec']=self.now-wo['eventTimeSec']
        assessment=self.metrics()
        self.emit('Observation',snapshot=snapshot,assessment=assessment)
        self.emit('DiagnosisReport',role='agent_om',transport='in_process_dispatch',
                  affectedServiceIds=[sid for sid,d in assessment['services'].items() if not d['satisfied']],
                  facts={'unservedCount':assessment['requestCount']-assessment['satisfiedCount']},
                  rootCauseScope='deterministic_link_node_capacity_and_declared_qos_checks')
        if self.unknown_environment:
            self.finish_work('waiting_supported_environment_semantics'); return
        if self.method=='keep_initial':
            self.emit('baseline_hold',note='initial allocation retained; no corrective configuration writes')
            self.finish_work('observed_keep_initial'); return
        began=time.perf_counter()
        target,audit=solve(snapshot['nodes'],snapshot['links'],snapshot['requests'],snapshot['assignments'],self.now,self.config,self.method)
        elapsed=time.perf_counter()-began; self.solver_wall_seconds+=elapsed
        changed=sorted(sid for sid in set(self.assignments)|set(target) if self.assignments.get(sid)!=target.get(sid))
        actions=[{'action':'release','serviceId':sid,'assignment':None} for sid in changed if self.assignments.get(sid) is not None]
        actions += [{'action':'install','serviceId':sid,'assignment':copy.deepcopy(target[sid])} for sid in changed if target.get(sid) is not None]
        pid=wo['workOrderId']+'-plan-%02d'%wo['attempts']
        for i,action in enumerate(actions): action['actionId']=pid+'-action-%03d'%i
        wo['_plan']={'planId':pid,'requestId':pid,'baseStateVersion':snapshot['stateVersion'],
            'baseConfigVersion':snapshot['configVersion'],'objectiveVersion':self.objective_version,
            'target':target,'actions':actions,'solverAudit':audit,'solverWallSeconds':elapsed}
        self.push(self.now+self.config['planning_delay_sec'],2,'plan_ready',{'workOrderId':wo['workOrderId']})

    def validate_plan(self,plan):
        errors=[]
        active=active_requests(self.requests,self.now)
        if plan['baseStateVersion']!=self.state_version: errors.append('STALE_STATE_VERSION')
        if plan['baseConfigVersion']!=self.config_version: errors.append('STALE_CONFIG_VERSION')
        if plan['objectiveVersion']!=self.objective_version: errors.append('STALE_OBJECTIVE_VERSION')
        for sid in self.config['protected_service_ids']:
            if sid in active and self.assignments.get(sid)!=plan['target'].get(sid): errors.append('PROTECTED_ASSIGNMENT_CHANGE:'+sid)
        before=loads(self.links,self.assignments); after=loads(self.links,plan['target'])
        for lid,l in self.links.items():
            cap=l['params']['bandwidthBps'] if usable(self.nodes,l) else 0
            if not formula.within_transition(after[lid], cap, before[lid]): errors.append('NEW_OR_WORSENED_CAPACITY_VIOLATION:'+lid)
        for sid,a in plan['target'].items():
            if a is not None and a!=self.assignments.get(sid):
                if sid not in active: errors.append('INACTIVE_SERVICE:'+sid); continue
                errors += [sid+':'+x for x in path_errors(self.nodes,self.links,active[sid],a)]
                if a['allocatedBps']!=active[sid]['requirement']['bandwidthBps']: errors.append('ORIGINAL_BANDWIDTH_CHANGED:'+sid)
        return errors

    def plan_ready(self):
        wo=self.current; plan=wo['_plan']
        if self.fault('stale'):
            self.state_version+=1; self.emit('external_version_injection',note='synthetic fault hook; no physical event claimed')
        wo['timings']['planGeneratedSec']=self.now
        wo['timings']['strategyGenerationSec']=self.now-wo['timings']['observationAvailableSec']
        self.emit('ControlPlan',role='agent_rc',transport='in_process_dispatch',plan=plan)
        errors=self.validate_plan(plan); self.emit('ValidationReport',planId=plan['planId'],valid=not errors,errors=errors)
        if errors:
            self.replan('plan_validation_rejected:'+','.join(errors)); return
        wo['status']='executing'; wo['_action_index']=0; wo['_expected_config_version']=self.config_version
        wo['timings']['submittedSec']=self.now
        if not plan['actions']:
            self.push(self.now+self.config['readback_delay_sec'],4,'readback',{'workOrderId':wo['workOrderId']})
        else:
            self.push(self.now+self.config['action_delay_sec'],3,'action',{'workOrderId':wo['workOrderId']})

    def lookup_action(self,action_id):
        """Read-only evidence query; does not retry or infer business success."""
        return copy.deepcopy(self.ledger.get(action_id))

    def submit_action(self,action,expected_versions=None,*,inject_failure=False,ack_lost=False):
        """Same ID/payload returns the original result, including failures/unknowns.

        A new attempt requires a new actionId. The environment and this ledger
        become durable together only when CheckpointStore.save succeeds.
        """
        aid=action.get('actionId'); payload_hash=digest(action)
        if aid in self.ledger:
            record=self.ledger[aid]
            if record['payloadHash']==payload_hash: return copy.deepcopy(record['receipt'])
            return {'actionId':aid,'status':'rejected_payload_conflict','reason':'IDEMPOTENCY_PAYLOAD_CONFLICT',
                    'ackReceived':True,'appliedKnownToExecutor':False}
        if self.done: raise RuntimeError('COMPLETED_RUN_IS_READ_ONLY')
        if not isinstance(aid,str) or not aid: raise ValueError('ACTION_ID_REQUIRED')
        if expected_versions is None: raise ValueError('EXPECTED_VERSIONS_REQUIRED_FOR_NEW_ACTION')
        reasons=[]; sid=action.get('serviceId'); active=active_requests(self.requests,self.now)
        if set(action)!={'actionId','action','serviceId','assignment'} or sid not in self.requests:
            reasons.append('INVALID_ACTION_FIELDS_OR_SERVICE')
        if (expected_versions.get('stateVersion')!=self.state_version or
            expected_versions.get('configVersion')!=self.config_version or
            expected_versions.get('objectiveVersion')!=self.objective_version): reasons.append('VERSION_CHANGED_BEFORE_ACTION')
        if self.unknown_environment: reasons.append('UNKNOWN_ENVIRONMENT')
        before=loads(self.links,self.assignments); proposed=copy.deepcopy(self.assignments)
        if not reasons:
            if action['action']=='release':
                if action['assignment'] is not None: reasons.append('RELEASE_MUST_HAVE_NULL_ASSIGNMENT')
            elif action['action']=='install':
                assignment=action['assignment']
                if not isinstance(assignment,dict) or set(assignment)!={'nodes','linkIds','allocatedBps'}:
                    reasons.append('INVALID_ASSIGNMENT_FIELDS')
                elif sid not in active: reasons.append('INACTIVE_SERVICE')
                else:
                    try:
                        number(assignment['allocatedBps'],'allocatedBps')
                        if assignment['allocatedBps']!=active[sid]['requirement']['bandwidthBps']: reasons.append('ORIGINAL_BANDWIDTH_CHANGED')
                        reasons+=path_errors(self.nodes,self.links,active[sid],assignment)
                    except (TypeError,KeyError,ValueError): reasons.append('INVALID_ASSIGNMENT_VALUE')
            else: reasons.append('UNSUPPORTED_ACTION')
        if not reasons:
            proposed[sid]=copy.deepcopy(action['assignment']); after=loads(self.links,proposed)
            if sid in active and sid in self.config['protected_service_ids'] and proposed[sid]!=self.assignments.get(sid): reasons.append('PROTECTED_ASSIGNMENT_CHANGE')
            for lid,l in self.links.items():
                cap=l['params']['bandwidthBps'] if usable(self.nodes,l) else 0
                if not formula.within_transition(after[lid], cap, before[lid]): reasons.append('PREFIX_NEW_OR_WORSE_OVERLOAD:'+lid)
        applied=not reasons and not inject_failure
        if applied:
            self.assignments=proposed; self.config_version+=1
            self.emit('configuration_applied',actionId=aid,action=action,loadBefore=before,loadAfter=after)
            self.update_stability()
        receipt={'actionId':aid,'status':('rejected' if reasons else 'failed' if inject_failure else 'unknown' if ack_lost else 'accepted_and_applied_in_memory'),
                 'ackReceived':not (applied and ack_lost),'appliedKnownToExecutor':applied and not ack_lost,
                 'reason':reasons or ('INJECTED_ACTION_FAILURE' if inject_failure else None),
                 'stateVersion':self.state_version,'configVersion':self.config_version,'receivedAtSec':self.now,
                 'note':'receipt and actual configuration do not assert business QoS'}
        self.ledger[aid]={'actionId':aid,'payloadHash':payload_hash,'actualApplied':applied,
                          'appliedAtSec':self.now if applied else None,'configVersion':self.config_version,
                          'action':copy.deepcopy(action),'receipt':copy.deepcopy(receipt)}
        return receipt

    def execute_action(self):
        wo=self.current; plan=wo['_plan']; i=wo['_action_index']; action=plan['actions'][i]
        aid=action['actionId']
        if self.state_version!=plan['baseStateVersion'] or self.config_version!=wo['_expected_config_version'] or self.objective_version!=plan['objectiveVersion']:
            self.emit('ExecutionReceipt',actionId=aid,status='rejected',reason='VERSION_CHANGED_BEFORE_ACTION',applied=False)
            self.replan('external_or_unexpected_config_change'); return
        failure=self.fault('action_failure',i) if aid not in self.ledger else False
        lost=self.fault('ack_loss',i) if not failure and aid not in self.ledger else False
        receipt=self.submit_action(action,{'stateVersion':plan['baseStateVersion'],
            'configVersion':wo['_expected_config_version'],'objectiveVersion':plan['objectiveVersion']},
            inject_failure=failure,ack_lost=lost)
        self.emit('ExecutionReceipt',**receipt)
        record=self.lookup_action(aid)
        if not record or not record['actualApplied']:
            wo['status']='partial_failure_readback_pending'
            wo['recoveryOwner']='executor'; wo['resumeCondition']='read current configuration and replan with fresh action IDs'
            self.push(self.now+self.config['readback_delay_sec'],4,'readback',{'workOrderId':wo['workOrderId']}); return
        wo['_expected_config_version']=self.config_version
        wo['actionIds'].append(aid)
        if wo['timings']['firstAppliedSec'] is None:
            wo['timings']['firstAppliedSec']=self.now
            wo['timings']['dispatchToApplySec']=self.now-wo['timings']['submittedSec']
        wo['timings']['lastAppliedSec']=self.now
        lost=receipt['status']=='unknown'
        if not lost and wo['timings']['firstAckSec'] is None:
            wo['timings']['firstAckSec']=self.now
            wo['timings']['dispatchToAckSec']=self.now-wo['timings']['submittedSec']
        wo['_action_index']+=1
        if lost:
            wo['status']='unknown_ack_readback_pending'
            wo['_uncertain_action_id']=aid
            wo['recoveryOwner']='executor'; wo['resumeCondition']='query original actionId and read current configuration without resubmission'
            self.push(self.now+self.config['readback_delay_sec'],4,'readback',{'workOrderId':wo['workOrderId']})
        elif wo['_action_index']<len(plan['actions']):
            self.push(self.now+self.config['action_delay_sec'],3,'action',{'workOrderId':wo['workOrderId']})
        else:
            self.push(self.now+self.config['readback_delay_sec'],4,'readback',{'workOrderId':wo['workOrderId']})

    def readback(self):
        wo=self.current; plan=wo['_plan']
        if wo.get('_uncertain_action_id'):
            self.emit('ActionStatusQuery',actionId=wo['_uncertain_action_id'],
                      result=self.lookup_action(wo['_uncertain_action_id']),
                      semantics='read-only original action ID; no repeat configuration write')
        if self.fault('readback_missing'):
            wo['status']='waiting_evidence'; self.emit('Readback',status='missing',configurationMatchesPlan=None,businessQoSSatisfied=None)
            wo['recoveryOwner']='agent_om'; wo['resumeCondition']='bounded readback retry; then next external observation'
            if wo.get('_readback_retries',0)>=self.config['max_replans_per_workorder']:
                self.finish_work('waiting_evidence_readback_budget_exhausted'); return
            wo['_readback_retries']=wo.get('_readback_retries',0)+1
            self.push(self.now+self.config['retry_delay_sec'],4,'readback',{'workOrderId':wo['workOrderId']}); return
        actual=copy.deepcopy(self.assignments); matches=actual==plan['target']; assessment=self.metrics()
        wo.pop('_uncertain_action_id',None)
        wo['timings']['readbackSec']=self.now; wo['timings']['businessMeasuredSec']=self.now
        wo['timings']['dispatchToReadbackSec']=self.now-wo['timings']['submittedSec']
        wo['timings']['dispatchToAckSec']=wo['timings']['firstAckSec']-wo['timings']['submittedSec'] if wo['timings']['firstAckSec'] is not None else None
        wo['timings']['dispatchToApplySec']=wo['timings']['firstAppliedSec']-wo['timings']['submittedSec'] if wo['timings']['firstAppliedSec'] is not None else None
        self.emit('Readback',status='available',assignments=actual,configurationMatchesPlan=matches,
                  configurationVersion=self.config_version)
        self.emit('VerificationReport',role='agent_om',transport='in_process_dispatch',
                  businessAssessment=assessment,allOriginalActiveRequirementsSatisfied=assessment['satisfiedCount']==assessment['requestCount'],
                  stableWindowSec=self.config['stability_window_sec'],
                  stableServiceIds=sorted(self.stable_confirmed))
        if self.state_version!=plan['baseStateVersion'] or not matches:
            self.replan('readback_current_state_requires_replan'); return
        if assessment['satisfiedCount']<assessment['requestCount']:
            wo['waitingCondition']='new external state, authorized scope change, or explicitly increased solver budget'
            self.finish_work('closed_with_unserved_in_scope')
        elif not assessment['requestCount']:
            self.finish_work('closed_no_active_requests')
        else:
            pending={'workOrderId':wo['workOrderId'],'traceId':wo['traceId'],'sinceSec':self.now,
                     'deadlineSec':self.now+self.config['stability_window_sec'],
                     'serviceIds':sorted(assessment['services']),
                     'requestDigest':digest(active_requests(self.requests,self.now)),
                     'status':'pending_stability'}
            self.pending_verifications.append(pending)
            self.push(pending['deadlineSec'],5,'verify_stable',pending)
            self.finish_work('verification_pending_stability')

    def _process_event(self,kind,item):
        if kind=='external': self.external(item)
        elif kind=='lifecycle':
            req=self.requests.get(item['serviceId'],{})
            field='startAtSec' if item['phase']=='start' else 'endAtSec'
            if req.get('_deleted') or req.get(field)!=item['expectedTimeSec']: return
            self.state_version+=1; self.emit('service_lifecycle_boundary',**item)
            self.objective_revision+=1
            self.objective_version=self.config['objective_version']+'/r'+str(self.objective_revision)
            self.update_stability(); self.start_work({'kind':'service_'+item['phase'],'eventTimeSec':self.now})
        elif kind=='verify_stable':
            same_requests=digest(active_requests(self.requests,self.now))==item['requestDigest']
            stable=same_requests and all(sid in self.stable_since and self.stable_since[sid]<=item['sinceSec'] for sid in item['serviceIds'])
            item['status']='archived_recovered' if stable else 'verification_interrupted_or_objective_changed'
            item['resolvedAtSec']=self.now
            target=next(w for w in self.workorders if w['workOrderId']==item['workOrderId'])
            target['status']=item['status']; target['finishedAtSec']=self.now
            self.emit('workorder_stability_resolution',workOrderId=item['workOrderId'],traceId=item['traceId'],
                      status=item['status'],requiredServiceIds=item['serviceIds'],windowSec=self.config['stability_window_sec'])
        elif kind=='stable':
            sid=item['serviceId']
            if self.stable_since.get(sid)==item['since'] and self.metrics()['services'].get(sid,{}).get('satisfied'):
                self.stable_confirmed.add(sid); self.emit('business_stability_evidence',serviceId=sid,sinceSec=item['since'],windowSec=self.config['stability_window_sec'])
        elif self.current and item['workOrderId']==self.current['workOrderId']:
            if kind=='observe': self.observation()
            elif kind=='plan_ready': self.plan_ready()
            elif kind=='action': self.execute_action()
            elif kind=='readback': self.readback()

    def step(self):
        """Advance one full event boundary. Only the store can make it durable."""
        start=len(self.trace)
        if self.done: return {'done':True,'eventKind':None,'timeSimSec':self.now,'traceStart':start,'traceEnd':start}
        if self._stepping or getattr(self,'_step_failed',False): raise RuntimeError('INVALID_STEP_STATE')
        self._stepping=True
        try:
            if self.queue and self.queue[0][0]<=self.endpoint:
                when,_,_,kind,item=heapq.heappop(self.queue)
                self.integrate_to(when); self._process_event(kind,item)
            else:
                kind='evaluation_endpoint'; self.integrate_to(self.endpoint)
                if self.current: self.finish_work('evaluation_horizon_reached')
                self.emit('evaluation_endpoint',assessment=self.metrics())
                self.completed_wall_seconds=self.wall_elapsed(); self.done=True
            self.step_count+=1
        except BaseException:
            self._step_failed=True; raise
        finally:
            self._stepping=False
        return {'done':self.done,'eventKind':kind,'timeSimSec':self.now,'traceStart':start,'traceEnd':len(self.trace)}

    def run(self):
        while not self.done: self.step()
        return self.finalize()

    def finalize(self):
        """Read-only result projection of a completed runtime; never replays work."""
        if not self.done: raise RuntimeError('RUN_NOT_COMPLETE_USE_STEP_OR_RUN')
        final=self.metrics()
        den=sum(r['requestSeconds'] for r in self.intervals); num=sum(r['satisfiedBusinessSeconds'] for r in self.intervals)
        wden=sum(r['priorityRequestSeconds'] for r in self.intervals); wnum=sum(r['prioritySatisfiedSeconds'] for r in self.intervals)
        durations={}
        for row in self.intervals:
            for sid,d in row['assessment']['services'].items():
                slot=durations.setdefault(sid,{'activeSeconds':0,'satisfiedSeconds':0,'unservedSeconds':0,'category':d['category'],'serviceType':d['serviceType']})
                slot['activeSeconds']+=row['durationSec']; slot['satisfiedSeconds']+=row['durationSec']*d['satisfied']; slot['unservedSeconds']+=row['durationSec']*(not d['satisfied'])
        public_workorders=copy.deepcopy(self.workorders)
        for wo in public_workorders:
            for key in list(wo):
                if key.startswith('_'): wo.pop(key)
        coverage={'eventsExpected':self.input_event_count,'eventsConsumed':self.event_count,
            'unsupportedEvents':self.unsupported,'allInputEventsConsumed':self.event_count==self.input_event_count,
            'allInputEventsSupported':not self.unsupported,'evaluationEndpointReached':self.now==self.endpoint,
            'implementedEventSemanticsNotAllIndependentlyValidated':['link_param_change','node_offline','node_online','link_state_explicit_boolean_or_named_status',
                'service_adjust_op_add','service_adjust_explicit_requirements','service_remove'],
            'notCovered':['official_simulator','packet_queue_interference_or_physical_link_rebuild','real_models','A2A_network',
                'external_device_transaction_recovery','make_before_break_healthy_rerouting','global_optimality'],
            'implementedRecoveryScope':'offline event-boundary environment and ledger snapshot; independent process tests required',
            'faultHooksConsumed':len(self.used_faults),'faultHooksConfigured':len(self.config['faults'])}
        summary={**public_identity(),'runId':self.run_id,'method':self.method,'evidenceKind':'offline_flow_level_discrete_event_runtime',
            'scenarioKey':self.config.get('scenario_key'),'scriptId':self.config.get('script_id'),
            'initialAssessment':self.initial_metrics,'finalAssessment':final,
            'evaluationStartSec':0,'evaluationEndSec':self.endpoint,'fixedHorizonSec':self.endpoint,
            'terminationReason':'time_limit','terminated':False,'truncated':True,
            'requestSeconds':den,'satisfiedBusinessSeconds':num,'unservedBusinessSeconds':den-num,
            'unknownBusinessSeconds':sum(r['unknownBusinessSeconds'] for r in self.intervals),
            'timeWeightedQoSSatisfaction':num/den if den else None,
            'priorityRequestSeconds':wden,'prioritySatisfiedSeconds':wnum,'priorityTimeWeightedQoSSatisfaction':wnum/wden if wden else None,
            'perService':durations,'appliedActions':sum(r['actualApplied'] for r in self.ledger.values()),'workorderCount':len(self.workorders),
            'stableAtEndpointServiceIds':sorted(self.stable_confirmed),
            'solverWallSeconds':self.solver_wall_seconds,'wallElapsedSeconds':self.wall_elapsed(),
            'downtimeSeconds':self.downtime_seconds,'clockSessions':copy.deepcopy(self.clock_sessions),
            'fiveMetricLocalDefinitions':{'networkConnectivity':'online node-pair connected fraction; original active business endpoints also retained',
               'businessGuarantee':'interval-weighted original-QoS satisfied business-seconds / active requested business-seconds',
               'perception':'event-to-current-observation simulation seconds',
               'strategyGeneration':'current-observation-to-plan-ready simulation seconds',
               'dispatch':'submission-to-first-ACK and submission-to-first-apply simulation seconds separately; submission-to-readback is an additional metric'},
            'timingMode':'configured deterministic simulated delays; actual solver wall time logged separately, not mapped to official latency',
            'officialScore':None,'modelCalls':0,'A2ANetworkCalls':0,'actualSimulatorCalls':0,
            'status':'completed_with_explicit_unsupported_events' if self.unsupported else 'offline_runtime_completed',
            'completeBGatePassed':False}
        return copy.deepcopy({'summary':summary,'trace':self.trace,'intervals':self.intervals,'workorders':public_workorders,
            'coverage':coverage,'resolved_config':self.config,
            'pending_verifications':self.pending_verifications,
            'final_state':{'nodes':self.nodes,'links':self.links,'requests':self.requests,'assignments':self.assignments,
                'stateVersion':self.state_version,'configVersion':self.config_version,'objectiveVersion':self.objective_version},
            'checkpoint':{'kind':'public_recovery_audit_without_future_queue','restartRecoveryImplemented':True,
                'scope':'offline_committed_event_boundary_only','commitSequence':self.commit_sequence,
                'runId':self.run_id,'atSimSec':self.now,'actionLedger':self.ledger}})


def run_scenario(topology, requests, events, config=None, *, method='priority_greedy', run_id=None):
    """Run one scenario in memory. The caller owns immutable result persistence."""
    return Runtime(topology,requests,events,config,method,run_id).run()
