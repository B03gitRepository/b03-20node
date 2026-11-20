"""Single-writer, atomic event-boundary checkpoints for the offline runtime.

The stable lock file is never replaced or unlinked. The checkpoint is private
environment state and contains the future event queue; never give it to roles.
Guarantee: process-exit recovery after a successful complete-file replacement.
No external-device exactly-once or cross-platform power-loss guarantee is made.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import uuid

FORMAT='b03-private-checkpoint-envelope-rss-v1'


def canonical_bytes(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('utf-8')


def code_fingerprints():
    root=Path(__file__).resolve().parent
    return {name:hashlib.sha256((root/name).read_bytes()).hexdigest()
            for name in sorted(p.name for p in root.glob('*.py'))}


class CheckpointError(ValueError):
    pass


class WriterLockedError(RuntimeError):
    pass


class CheckpointStore:
    """Hold this context through load, all steps, save, and result persistence."""
    def __init__(self,run_dir):
        self.run_dir=Path(run_dir).resolve()
        self.path=self.run_dir/'checkpoint.json'
        self._handle=None; self._pid=None

    @property
    def exists(self):
        self._require_owner()
        return self.path.is_file()

    def __enter__(self):
        if self._handle is not None: raise WriterLockedError('STORE_ALREADY_OPEN')
        self.run_dir.mkdir(parents=True,exist_ok=True)
        handle=open(self.run_dir/'.writer.lock','a+b')
        try:
            if os.name=='nt':
                import msvcrt
                handle.seek(0); msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            handle.close(); raise WriterLockedError('RUN_ALREADY_HAS_WRITER') from exc
        self._handle=handle; self._pid=os.getpid()
        return self

    def _require_owner(self):
        if self._handle is None or self._pid!=os.getpid(): raise WriterLockedError('WRITER_LOCK_REQUIRED')

    def __exit__(self,typ,value,tb):
        if self._handle is not None:
            if self._pid==os.getpid():
                if os.name=='nt':
                    import msvcrt
                    self._handle.seek(0); msvcrt.locking(self._handle.fileno(),msvcrt.LK_UNLCK,1)
                else:
                    import fcntl
                    fcntl.flock(self._handle.fileno(),fcntl.LOCK_UN)
            self._handle.close(); self._handle=None; self._pid=None

    def _read_payload(self):
        self._require_owner()
        try:
            envelope=json.loads(self.path.read_text(encoding='utf-8'))
            if envelope.get('format')!=FORMAT: raise CheckpointError('CHECKPOINT_FORMAT_MISMATCH')
            payload=envelope['payload']
            actual=hashlib.sha256(canonical_bytes(payload)).hexdigest()
            if actual!=envelope['checksumSha256']: raise CheckpointError('CHECKPOINT_CHECKSUM_MISMATCH')
            return payload
        except (OSError,KeyError,TypeError,json.JSONDecodeError) as exc:
            raise CheckpointError('CHECKPOINT_UNREADABLE_OR_DAMAGED') from exc

    def load(self,*,expected_config_hash=None,expected_input_hash=None):
        from .engine import Runtime
        payload=self._read_payload()
        return Runtime.restore_checkpoint(payload,expected_config_hash=expected_config_hash,
                                          expected_input_hash=expected_input_hash)

    def save(self,runtime,*,fail_before_replace=False):
        self._require_owner()
        if self.path.exists():
            prior=self._read_payload()
            if prior['runId']!=runtime.run_id: raise CheckpointError('RUN_ID_MISMATCH')
            if prior['state']['commit_sequence']!=runtime.commit_sequence: raise CheckpointError('STALE_COMMIT_SEQUENCE')
            if prior['inputHash']!=runtime.input_hash or prior['configHash']!=runtime.config_hash:
                raise CheckpointError('CHECKPOINT_INPUT_OR_CONFIG_MISMATCH')
        elif runtime.commit_sequence:
            raise CheckpointError('MISSING_PREVIOUS_COMMIT')
        payload=runtime.export_checkpoint(commit_sequence=runtime.commit_sequence+1)
        envelope={'format':FORMAT,'checksumSha256':hashlib.sha256(canonical_bytes(payload)).hexdigest(),'payload':payload}
        raw=canonical_bytes(envelope)+b'\n'
        temporary=self.run_dir/('.checkpoint-'+uuid.uuid4().hex+'.tmp')
        try:
            with open(temporary,'xb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            if fail_before_replace: raise OSError('INJECTED_SAVE_FAILURE_BEFORE_REPLACE')
            os.replace(temporary,self.path)
            # Replacement is the process-recovery commit point. A subsequent
            # directory-sync failure must not leave the in-memory sequence stale.
            runtime.commit_sequence=payload['state']['commit_sequence']
            directory_sync={'attempted':os.name!='nt','succeeded':None}
            # Directory sync is attempted only on POSIX; no Windows power-loss claim.
            if os.name!='nt':
                try:
                    fd=os.open(self.run_dir,os.O_RDONLY)
                    try: os.fsync(fd)
                    finally: os.close(fd)
                    directory_sync['succeeded']=True
                except OSError as exc:
                    directory_sync.update(succeeded=False,error=str(exc))
            return {'runId':runtime.run_id,'commitSequence':runtime.commit_sequence,
                    'atSimSec':runtime.now,'checkpointSha256':hashlib.sha256(raw).hexdigest(),
                    'payloadSha256':envelope['checksumSha256'],'bytes':len(raw),
                    'path':str(self.path),'commitPoint':'atomic_replace_completed',
                    'directorySync':directory_sync,'powerLossDurabilityClaimed':False}
        finally:
            if temporary.exists(): temporary.unlink()
