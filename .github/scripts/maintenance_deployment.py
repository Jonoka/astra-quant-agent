"""Versioned maintenance controller, used only by explicitly pinned operations.

No legacy bridge, signals, forced task termination or credentials are supplied.
Protocol self-reports are checked against actual Docker/process identities.
The existing Upgrade remains responsible for filesystem/state recovery.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import sys
import ast
import hashlib
import tarfile
import time

PROTOCOL = 1
ROLES = ('backend', 'gateway', 'watchdog-backend', 'watchdog-gateway')
LABEL = 'io.jonoka.astra.maintenance-protocol'
CLI = '/app/scripts/maintenance_control.py'
DATABASE = 'data/maintenance_state.sqlite'
SHA64 = re.compile(r'[0-9a-f]{64}\Z')


def archived_protocol_hash(archive, source, require):
    """Read the exact Git archive, without extraction or a candidate oracle."""
    with tarfile.open(archive, 'r:') as bundle:
        require(bundle.pax_headers.get('comment') == source, 'maintenance_archive_source_pin')
        entries = [m for m in bundle.getmembers() if m.name == 'astra_backend/maintenance.py']
        require(len(entries) == 1 and entries[0].isfile(), 'maintenance_previous_or_target_protocol_missing')
        raw = bundle.extractfile(entries[0]).read()
    nodes = [n for n in ast.parse(raw).body if isinstance(n, ast.Assign) and
             any(isinstance(t, ast.Name) and t.id == 'PROTOCOL_VERSION' for t in n.targets)]
    require(len(nodes) == 1 and isinstance(nodes[0].value, ast.Constant) and
            type(nodes[0].value.value) is int and nodes[0].value.value == PROTOCOL,
            'maintenance_archive_protocol_version')
    return hashlib.sha256(raw).hexdigest()


def capture_scope(operation, plan, command, require, *, uid_sha256, budget, drain, mode='maintenance'):
    require(isinstance(uid_sha256, str) and SHA64.fullmatch(uid_sha256),
            'maintenance_independent_demo_identity_required')
    hashes = {label: archived_protocol_hash(operation / ('source-' + label + '.tar'),
                                          plan['previous_source'] if label == 'previous' else plan['release_source'],
                                          require) for label in ('previous', 'release')}
    # Cross-version protocol changes need their own compatibility review. Never
    # assume a matching version number implies matching persistence semantics.
    require(hashes['previous'] == hashes['release'], 'maintenance_protocol_revision_requires_review')
    result = json.loads(command('docker', 'exec', 'astraquant-backend', 'python3', '-B', CLI, 'status'))
    require(result.get('protocol') == PROTOCOL and result.get('phase') == 'NORMAL' and
            not result.get('fenced') and not result.get('activities'),
            'maintenance_protocol_not_cold_initialized_or_active')
    instances = {}
    for row in result.get('actors', []):
        if not row.get('online'):
            continue
        identity = json.loads(row['identity'])
        require(identity['role'] not in instances, 'maintenance_duplicate_actor')
        instances[identity['role']] = identity
    binding = result.get('binding')
    require(isinstance(binding, dict) and set(instances) == set(ROLES),
            'maintenance_exact_enabled_instances_missing')
    scope = {'protocol': PROTOCOL, 'mode': mode, 'budget_seconds': budget, 'drain_seconds': drain,
             'generation': binding['generation'] + 1, 'instances': instances,
             'previous_protocol_sha256': hashes['previous'], 'target_protocol_sha256': hashes['release'],
             'account_uid_sha256': uid_sha256, 'store_relative_path': DATABASE}
    validate_scope(scope, plan, require)
    return scope


def validate_scope(scope, plan, require):
    require(isinstance(scope, dict) and set(scope) == {
        'protocol', 'mode', 'budget_seconds', 'drain_seconds', 'generation', 'instances',
        'previous_protocol_sha256', 'target_protocol_sha256', 'account_uid_sha256',
        'store_relative_path'}, 'maintenance_plan_schema')
    require(type(scope['protocol']) is int and scope['protocol'] == PROTOCOL and
            scope['mode'] in ('normal', 'maintenance') and
            type(scope['budget_seconds']) is int and 1 <= scope['budget_seconds'] <= 1200 and
            type(scope['drain_seconds']) is int and
            1 <= scope['drain_seconds'] <= scope['budget_seconds'] and
            type(scope['generation']) is int and scope['generation'] > 0 and
            scope['store_relative_path'] == DATABASE, 'maintenance_budget_scope')
    for key in ('previous_protocol_sha256', 'target_protocol_sha256', 'account_uid_sha256'):
        require(isinstance(scope[key], str) and SHA64.fullmatch(scope[key]), 'maintenance_proof_hash')
    require(isinstance(scope['instances'], dict) and set(scope['instances']) == set(ROLES),
            'maintenance_exact_instances')
    seen = set()
    for role, identity in scope['instances'].items():
        require(isinstance(identity, dict) and set(identity) ==
                {'role', 'instance_id', 'source', 'image', 'protocol'} and
                identity['role'] == role and identity['source'] == plan['previous_source'] and
                identity['image'] == plan['previous_image'] and
                type(identity['protocol']) is int and identity['protocol'] == PROTOCOL and
                isinstance(identity['instance_id'], str) and
                re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}', identity['instance_id']),
                'maintenance_instance_identity')
        require(identity['instance_id'] not in seen, 'maintenance_duplicate_instance')
        seen.add(identity['instance_id'])


def load_protocol(path, expected_sha, digest, require):
    require(path.is_file() and digest(path) == expected_sha, 'maintenance_packaged_protocol')
    name = '_astra_reviewed_maintenance_' + expected_sha
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    module = sys.modules[name]
    require(module.PROTOCOL_VERSION == PROTOCOL, 'maintenance_protocol_version')
    return module


def check_protocol_versions(upgrade):
    """No control DB/ROOT required: reject unsupported recovery before rename."""
    plan = upgrade.deployment_plan()
    require = upgrade.require_maintenance
    require(plan['schema'] == 2, 'maintenance_protocol_plan_required')
    scope = plan['maintenance']
    validate_scope(scope, plan, require)
    for side, image, source in [('previous', plan['previous_image'], plan['previous_source']),
                                ('release', plan['image'], plan['release_source'])]:
        path = upgrade.op / ('source-' + side) / 'astra_backend/maintenance.py'
        expected = scope[('previous' if side == 'previous' else 'target') + '_protocol_sha256']
        require(path.is_file() and upgrade.digest_maintenance(path) == expected,
                'maintenance_legacy_or_source_protocol_refused')
        metadata = json.loads(upgrade.command_maintenance('docker', 'image', 'inspect', image))[0]
        labels = metadata['Config'].get('Labels') or {}
        require(image in metadata.get('RepoDigests', []) and
                labels.get('org.opencontainers.image.revision') == source and
                labels.get(LABEL) == str(PROTOCOL) and
                labels.get('io.jonoka.astra.maintenance-source-sha256') == expected,
                'maintenance_image_protocol_refused')


class MaintenanceCoordinator:
    def __init__(self, upgrade, module, *, clock=time.time, sleep=time.sleep):
        self.upgrade, self.module = upgrade, module
        self.clock, self.sleep = clock, sleep
        self.plan = upgrade.deployment_plan()
        self.scope = self.plan['maintenance']
        self.require = upgrade.require_maintenance
        self.root = upgrade.root_maintenance
        validate_scope(self.scope, self.plan, self.require)
        path = self.root / DATABASE
        self.require(path.is_file() and (path.parent / 'maintenance_enabled').is_file(),
                     'maintenance_not_enabled_cold_initialization_required')
        # Never create an empty database or enroll an ordinary running process.
        self.store = module.MaintenanceStore(path, clock=clock)
        self.initial = module.Binding(
            upgrade.op.name, upgrade.deployment_plan_sha256,
            self.plan['previous_source'], self.plan['previous_image'],
            self.plan['release_source'], self.plan['image'], self.scope['generation'],
            {role: module.Identity.from_dict(value) for role, value in self.scope['instances'].items()})
        self.ended = set()

    def binding(self):
        state = self.store.status()
        if state['binding'] and state['binding']['operation_id'] == self.initial.operation_id:
            binding = self.module.Binding.from_dict(state['binding'])
            self.require(binding.plan_sha256 == self.initial.plan_sha256 and
                         (binding.previous_source, binding.previous_image, binding.target_source,
                          binding.target_image) == (self.initial.previous_source, self.initial.previous_image,
                                                   self.initial.target_source, self.initial.target_image),
                         'maintenance_operation_drift')
            return binding
        self.require(state['phase'] == 'NORMAL', 'maintenance_current_operation_changed')
        return self.initial

    def check_versions(self):
        check_protocol_versions(self.upgrade)

    @staticmethod
    def container(role):
        return 'astraquant-gateway' if role in ('gateway', 'watchdog-gateway') else 'astraquant-backend'

    def check_instances(self, binding):
        """Independently inspect the actual namespace PID/start and approved image."""
        probe = r'''import json,os,sys
from pathlib import Path
pid=int(sys.argv[1]); role=sys.argv[2]; expected=sys.argv[3]
try:
 p=Path('/proc')/str(pid); st=p.joinpath('stat').read_text().rsplit(')',1)[1].split()
 identity=Path('/proc/sys/kernel/random/boot_id').read_text().strip()+':'+str(p.joinpath('ns/pid').stat().st_ino)+':'+str(pid)+':'+st[19]
 args=p.joinpath('cmdline').read_bytes().split(b'\0'); text=b' '.join(args)
 if role=='gateway': valid=b'astra_gateway.worker' in args
 elif role=='backend': valid=b'astra_backend.app:app' in args
 else: valid=any(a.endswith(b'astra_watchdog.sh') for a in args) and ((b'gateway' in args)==(role=='watchdog-gateway'))
 counts={'qq_gateway_daemon':0,'daemon_web_sync':0,'market_stream':0,'legacy_scheduler':0}
 for child in Path('/proc').iterdir():
  if not child.name.isdigit(): continue
  try:
   words=child.joinpath('cmdline').read_bytes().split(b'\0')
   names=[a.rsplit(b'/',1)[-1] for a in words]
   for name in counts:
    if (name+'.py').encode() in names or ('astra_backend.'+name).encode() in names: counts[name]+=1
   if b'astra_backend.scheduler' in names or any(a.endswith(b'/astra_backend/scheduler.py') for a in words): counts['legacy_scheduler']+=1
  except (FileNotFoundError,ProcessLookupError): pass
 print(json.dumps({'exact_start':identity==expected,'role_matches':bool(valid),'alive':st[0]!='Z','unsupported_producers':{'verified':True,'counts':counts}}))
except (OSError,IndexError): print(json.dumps({'exact_start':False,'role_matches':False,'alive':False,'unsupported_producers':{'verified':False,'counts':{}}}))
'''
        for role, identity in binding.instances.items():
            parts = identity.instance_id.split(':')
            self.require(len(parts) == 4 and all(p.isdigit() for p in parts[1:]) and
                         int(parts[2]) > 0, 'maintenance_real_linux_start_identity')
            name = self.container(role)
            c = json.loads(self.upgrade.command_maintenance('docker', 'inspect', name))[0]
            self.require(c['Config']['Image'] == identity.image and
                         (c['Config'].get('Labels') or {}).get('org.opencontainers.image.revision') == identity.source and
                         c['State']['Running'], 'maintenance_current_container_identity')
            result = json.loads(self.upgrade.command_maintenance('docker', 'exec', name, 'python3', '-B',
                                                                '-c', probe, parts[2], role, identity.instance_id))
            self.require(all(result.get(k) is True for k in ('exact_start', 'role_matches', 'alive')),
                         'maintenance_actual_process_identity')
            external = result.get('unsupported_producers')
            self.require(isinstance(external, dict) and external.get('verified') is True and
                         external.get('counts') == {'qq_gateway_daemon': 0, 'daemon_web_sync': 0, 'market_stream': 0,
                                                   'legacy_scheduler': 0},
                         'maintenance_unsupported_resident_writer')

    def proof(self, binding):
        # New operation proof is bound explicitly, never borrowed from an old lease.
        data = self.upgrade.input_command_maintenance(
            ('docker', 'exec', '-i', 'astraquant-backend', 'python3', '-B', CLI,
             'risk', '--binding-stdin', '--account-uid-sha256', self.scope['account_uid_sha256']),
            json.dumps(binding.as_dict()).encode())
        evidence = json.loads(data)
        self.require(evidence.get('read_only') is True and
                     evidence.get('account_uid_sha256') == self.scope['account_uid_sha256'],
                     'maintenance_broker_identity_unverified')
        proof = self.module.RiskProof.from_dict(evidence['proof'])
        self.module.validate_risk_proof(proof, binding, self.clock())
        return proof

    def request_pause(self):
        self.check_versions()
        self.check_instances(self.initial)
        state = self.store.status()
        journal = self.upgrade.state
        deadline = journal.get('maintenance_deadline')
        if deadline is None:
            self.require(state['phase'] == 'NORMAL', 'maintenance_initialization_or_existing_hold')
            deadline = self.clock() + self.scope['budget_seconds']
            journal['maintenance_deadline'] = deadline
            journal['maintenance_drain_deadline'] = min(deadline, self.clock() + self.scope['drain_seconds'])
            self.upgrade.phase(journal['phase'])
        self.store.request(self.initial, deadline, self.proof(self.initial))
        drain_until = journal['maintenance_drain_deadline']
        while self.clock() < drain_until:
            state = self.store.status()
            self.require(state['binding'] == self.initial.as_dict(), 'maintenance_generation_drift')
            self.check_instances(self.initial)
            if not state['activities']:
                try:
                    self.store.pause(self.initial, self.proof(self.initial))
                    return
                except self.module.MaintenanceError:
                    pass  # A missing ACK/unsafe fresh proof never becomes success.
            self.sleep(min(1, max(0, drain_until - self.clock())))
        self.store.cancel(self.initial, 'drain budget exhausted; explicit recovery/resume required')
        self.require(False, 'maintenance_drain_cancelled_no_stop_or_kill')

    def orderly_stop(self, *, recovery=False):
        binding = self.binding()
        self.upgrade.assert_maintenance_ownership()
        self.check_instances(binding)
        def boundary():
            # Re-prove the actual binding, drain and DEMO state before each
            # supervisory mutation and the actual natural shutdown request.
            try:
                if not recovery:
                    self.store.pause(binding, self.proof(binding))
                else:
                    self.module.validate_risk_proof(self.proof(binding), binding, self.clock())
                if not recovery and self.scope['mode'] == 'normal':
                    self.upgrade.idle_window()
            except BaseException:
                self.store.cancel(binding, 'pre-shutdown boundary refused; explicit resume required')
                raise
        boundary()
        if recovery:
            self.store.begin_recovery(binding)
        policies = self.upgrade.state.setdefault('maintenance_restart_policies', {})
        for name in ('astraquant-backend', 'astraquant-gateway'):
            c = json.loads(self.upgrade.command_maintenance('docker', 'inspect', name))[0]
            if name not in policies:
                policies[name] = {'container_id': c['Id'], 'policy': c['HostConfig']['RestartPolicy']}
                self.upgrade.phase(self.upgrade.state['phase'])
            # Recovery stops the separately journaled candidate, not the old
            # container ID whose original restart policy is being preserved.
            self.require(c['Config']['Image'] == binding.instances[
                'gateway' if name.endswith('gateway') else 'backend'].image,
                'maintenance_supervision_identity_drift')
            targets = self.upgrade.state.setdefault('maintenance_stop_targets', {})
            key = str(binding.generation) + ':' + name
            if key not in targets:
                targets[key] = c['Id']
                self.upgrade.phase(self.upgrade.state['phase'])
            self.require(targets[key] == c['Id'], 'maintenance_stop_target_drift')
            boundary()
            self.upgrade.command_maintenance('docker', 'update', '--restart=no', c['Id'])
        if not recovery:
            boundary()
            self.store.begin_shutdown(binding)
        deadline = self.upgrade.state['maintenance_deadline']
        while self.clock() < deadline:
            exited = True
            for name, policy in policies.items():
                c = json.loads(self.upgrade.command_maintenance('docker', 'inspect', name))[0]
                self.require(c['Id'] == self.upgrade.state['maintenance_stop_targets'][
                    str(binding.generation) + ':' + name], 'maintenance_container_replaced_while_draining')
                exited = exited and c['State']['Status'] == 'exited'
            if exited:
                # Actual whole-container exit, not an unverified caller supplied PID set.
                self.ended = {identity.instance_id for identity in binding.instances.values()}
                self.store.begin_switch(binding, self.ended)
                self.upgrade.state['maintenance_ended_instances'] = sorted(self.ended)
                self.upgrade.phase(self.upgrade.state['phase'])
                return
            self.sleep(min(1, max(0, deadline - self.clock())))
        self.store.hold('orderly shutdown deadline exceeded; no force termination')
        self.require(False, 'maintenance_shutdown_incomplete_no_kill')

    def startup_environment(self, previous=False):
        return {'ASTRA_MAINTENANCE_ENABLED': '1',
                'ASTRA_MAINTENANCE_DB': '/app/' + DATABASE,
                'ASTRA_SOURCE_COMMIT': self.plan['previous_source'] if previous else self.plan['release_source'],
                'ASTRA_IMAGE_REF': self.plan['previous_image'] if previous else self.plan['image'],
                'ASTRA_MAINTENANCE_STARTUP_BINDING': json.dumps(self.binding().as_dict(), sort_keys=True)}

    def rebind_started(self, previous=False):
        binding = self.binding()
        state = self.store.status()
        source = self.plan['previous_source'] if previous else self.plan['release_source']
        image = self.plan['previous_image'] if previous else self.plan['image']
        identities = {}
        for row in state['actors']:
            if not row['online']:
                continue
            identity = self.module.Identity.from_dict(json.loads(row['identity']))
            self.require((identity.source, identity.image) == (source, image) and identity.role not in identities,
                         'maintenance_unbound_or_duplicate_started_actor')
            identities[identity.role] = identity
        self.require(set(identities) == set(ROLES), 'maintenance_started_role_missing')
        new_binding = self.module.Binding(
            binding.operation_id, binding.plan_sha256, binding.previous_source, binding.previous_image,
            binding.target_source, binding.target_image, binding.generation + 1, identities)
        self.check_instances(new_binding)
        ended = set(self.upgrade.state['maintenance_ended_instances'])
        self.store.rebind(binding, identities, ended)
        deadline = self.upgrade.state['maintenance_deadline']
        while self.clock() < deadline:
            try:
                self.store.begin_verify(new_binding)
                return
            except self.module.MaintenanceError:
                self.sleep(min(1, max(0, deadline - self.clock())))
        self.store.hold('candidate startup ACK barrier incomplete')
        self.require(False, 'maintenance_candidate_not_verified_kept_paused')

    def explicit_resume(self):
        current = self.store.status()
        self.require(current.get('binding') is not None and
                     current['binding']['operation_id'] == self.initial.operation_id and
                     current['binding']['plan_sha256'] == self.initial.plan_sha256,
                     'maintenance_stale_resume_operation')
        binding = self.binding()
        self.upgrade.assert_maintenance_ownership()
        self.check_instances(binding)
        # Every fallible supervision action stays behind the admission fence.
        # A failed update/inspection must never turn a healthy pause into trading.
        for name in ('astraquant-backend', 'astraquant-gateway'):
            saved = self.upgrade.state.get('maintenance_restart_policies', {}).get(name)
            self.require(saved is not None, 'maintenance_original_supervision_missing')
            c = json.loads(self.upgrade.command_maintenance('docker', 'inspect', name))[0]
            self.require(c['Config']['Image'] == binding.instances[
                'gateway' if name.endswith('gateway') else 'backend'].image,
                'maintenance_resume_runtime_drift')
            policy = saved['policy']
            self.require(isinstance(policy, dict) and set(policy) == {'Name', 'MaximumRetryCount'} and
                         policy['Name'] in {'no', 'always', 'unless-stopped', 'on-failure'} and
                         type(policy['MaximumRetryCount']) is int and policy['MaximumRetryCount'] >= 0,
                         'maintenance_supervision_policy_invalid')
            value = policy['Name']
            if value == 'on-failure' and policy.get('MaximumRetryCount'):
                value += ':' + str(policy['MaximumRetryCount'])
            self.upgrade.command_maintenance('docker', 'update', '--restart=' + value, c['Id'])
        self.upgrade.assert_maintenance_ownership()
        self.check_instances(binding)
        for name in ('astraquant-backend', 'astraquant-gateway'):
            saved = self.upgrade.state['maintenance_restart_policies'][name]
            c = json.loads(self.upgrade.command_maintenance('docker', 'inspect', name))[0]
            self.require(c['HostConfig']['RestartPolicy'] == saved['policy'],
                         'maintenance_supervision_restore_not_verified')
        proof = self.proof(binding)
        # Final atomic barrier/source generation check is the ONLY opening action.
        self.store.resume(binding, proof)
