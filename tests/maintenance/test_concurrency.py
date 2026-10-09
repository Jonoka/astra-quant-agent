"""Real cross-process SQLite tests; these are NOT Linux flock evidence."""
import concurrent.futures
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

spec = importlib.util.spec_from_file_location("_maintenance_concurrency_fixture", Path(__file__).with_name("test_protocol.py"))
fixture = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fixture
spec.loader.exec_module(fixture)
m = fixture.m

CHILD = """
import importlib.util,json,sys,os
spec=importlib.util.spec_from_file_location('core',sys.argv[1])
m=importlib.util.module_from_spec(spec);sys.modules['core']=m;spec.loader.exec_module(m)
s=m.MaintenanceStore(sys.argv[2],clock=lambda:1000)
identity=m.Identity.from_dict(json.loads(sys.argv[3]))
binding=m.Binding.from_dict(json.loads(sys.argv[4]))
print('ready',flush=True);sys.stdin.readline()
try:
 if sys.argv[5]=='request':
  p=m.RiskProof(binding.operation_id,binding.generation,binding.plan_sha256,identity.source,identity.image,1000,'DEMO',True,0,0,0,0,0,0)
  s.request(binding,1120,p);result='fenced'
 elif sys.argv[5] in ('order','order-crash'):
  result=s.prepare_order(identity,'one-logical-intent','e'*64)['send_allowed']
 else:
  result=s.admit(identity,'queued-future')
 print(json.dumps(result),flush=True)
 if sys.argv[5] in ('crash','order-crash'):os._exit(7)
except m.AdmissionClosed:print(json.dumps('closed'),flush=True)
"""


class ConcurrencyTests(fixture.ProtocolFixture):
    def start_child(self, mode, binding=None):
        process = subprocess.Popen([sys.executable, "-c", CHILD, str(fixture.CORE_PATH), str(self.path),
                                    json.dumps(self.identities["backend"].as_dict()),
                                    json.dumps((binding or self.binding).as_dict()), mode],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.wait(timeout=10) if process.poll() is None else None)
        self.assertEqual(process.stdout.readline().strip(), "ready")
        return process

    @staticmethod
    def release(process):
        output, error = process.communicate("go\n", timeout=20)
        if error:
            raise AssertionError(error)
        return json.loads(output.strip())

    def test_admission_and_request_are_serialized_across_processes(self):
        self.normal()
        binding = self.next_binding()
        actor = self.start_child("admit", binding)
        fence = self.start_child("request", binding)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            a = executor.submit(self.release, actor)
            b = executor.submit(self.release, fence)
            admitted, fenced = a.result(), b.result()
        self.assertEqual(fenced, "fenced")
        status = self.store.status()
        self.assertTrue(status["fenced"])
        self.assertEqual(len(status["activities"]), int(admitted != "closed"))
        with self.assertRaises(m.AdmissionClosed):
            self.store.admit(self.identities["backend"], "late-manual-write")
        if admitted != "closed":
            with self.assertRaises(m.MaintenanceError):
                self.store.acknowledge(binding, self.identities["backend"])

    def test_cross_process_order_reservation_allows_exactly_one_send(self):
        self.normal()
        processes = [self.start_child("order") for _ in range(2)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(self.release, processes))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(self.store.status()["orders"]), 1)
        self.assertEqual(len(self.store.status()["activities"]), 1)

    def test_crashed_process_activity_is_not_lease_pruned(self):
        self.normal()
        child = self.start_child("crash")
        activity = self.release(child)
        self.assertEqual(child.returncode, 7)
        self.request(self.next_binding())
        self.now += 100000
        reopened = m.MaintenanceStore(self.path, clock=lambda: self.now)
        status = reopened.status()
        self.assertTrue(status["fenced"])
        self.assertEqual(status["activities"][0]["activity_id"], activity)
        with self.assertRaises(m.MaintenanceError):
            reopened.acknowledge(self.next_binding(), self.identities["backend"])

    def test_concurrent_initialization_preserves_schema_and_hold(self):
        path = Path(self.temp.name) / "concurrent.sqlite"
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            stores = list(executor.map(lambda _: m.MaintenanceStore(path, clock=lambda: self.now), range(4)))
        self.assertTrue(all(store.status()["phase"] == "HOLD" for store in stores))

    def test_process_crash_after_pre_send_commit_never_retries_send(self):
        self.normal()
        child = self.start_child("order-crash")
        self.assertTrue(self.release(child))
        self.assertEqual(child.returncode, 7)
        self.store.hold("sender crashed")
        self.now += 100000
        reopened = m.MaintenanceStore(self.path, clock=lambda: self.now)
        record = reopened.prepare_order(self.identities["backend"], "one-logical-intent", "e" * 64)
        self.assertFalse(record["send_allowed"])
        self.assertEqual(record["status"], "PREPARED")
        self.assertTrue(reopened.status()["fenced"])
        self.assertEqual(len(reopened.status()["activities"]), 1)


if __name__ == "__main__":
    unittest.main()
