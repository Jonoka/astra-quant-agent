"""Durable pre-send intent journal tests without importing tests/__init__.py."""
import importlib.util
from pathlib import Path
import sys
import unittest

spec = importlib.util.spec_from_file_location("_maintenance_protocol_fixture", Path(__file__).with_name("test_protocol.py"))
fixture = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fixture
spec.loader.exec_module(fixture)
m = fixture.m


class JournalTests(fixture.ProtocolFixture):
    def setUp(self):
        super().setUp()
        self.normal()
        self.owner = self.identities["backend"]
        self.request_hash = "e" * 64

    def prepare(self, intent="slot-1:BTC:open"):
        return self.store.prepare_order(self.owner, intent, self.request_hash)

    def test_send_is_durable_before_broker_and_stable_on_retry(self):
        order = self.prepare()
        reopened = m.MaintenanceStore(self.path, clock=lambda: self.now)
        persisted = reopened.status()["orders"][0]
        self.assertEqual(persisted["client_order_id"], order["client_order_id"])
        self.assertTrue(order["send_allowed"])
        retry = reopened.prepare_order(self.owner, order["logical_intent"], self.request_hash)
        self.assertFalse(retry["send_allowed"])
        self.assertEqual(retry["client_order_id"], order["client_order_id"])
        self.assertEqual(len(reopened.status()["activities"]), 1)

    def test_payload_drift_never_sends(self):
        order = self.prepare()
        with self.assertRaises(m.MaintenanceError):
            self.store.prepare_order(self.owner, order["logical_intent"], "f" * 64)
        self.assertEqual(len(self.store.status()["orders"]), 1)

    def test_unknown_and_partial_block_maintenance_and_finish(self):
        for outcome, broker_id in (("UNKNOWN", ""), ("PARTIAL", "broker-1")):
            intent = "slot:" + outcome
            order = self.prepare(intent)
            self.store.record_order(self.owner, intent, outcome, broker_id)
            with self.assertRaises(m.MaintenanceError):
                self.store.finish(order["activity_id"], self.owner)
            with self.assertRaises(m.MaintenanceError):
                self.request(self.next_binding())
            with self.assertRaises(m.MaintenanceError):
                self.store.record_order(self.owner, intent, "ACKNOWLEDGED", "broker-1")
            self.assertFalse(self.store.prepare_order(self.owner, intent, self.request_hash)["send_allowed"])
        self.assertEqual(len(self.store.status()["activities"]), 2)

    def test_receipt_requires_real_broker_identity(self):
        order = self.prepare()
        for outcome in ("ACKNOWLEDGED", "PARTIAL"):
            with self.assertRaises(m.MaintenanceError):
                self.store.record_order(self.owner, order["logical_intent"], outcome)
        self.assertEqual(self.store.status()["orders"][0]["status"], "PREPARED")

    def test_acknowledged_and_rejected_release_send_not_resend(self):
        for outcome, broker_id in (("ACKNOWLEDGED", "broker-1"), ("REJECTED", "")):
            intent = "slot:" + outcome
            self.prepare(intent)
            self.store.record_order(self.owner, intent, outcome, broker_id)
            self.store.record_order(self.owner, intent, outcome, broker_id)
            self.assertFalse(self.store.prepare_order(self.owner, intent, self.request_hash)["send_allowed"])
            with self.assertRaises(m.MaintenanceError):
                self.store.record_order(self.owner, intent, "UNKNOWN")
        self.assertEqual(self.store.status()["activities"], [])

    def test_timeout_never_resubmits_or_drops_unknown(self):
        order = self.prepare()
        self.store.record_order(self.owner, order["logical_intent"], "UNKNOWN")
        self.store.hold("connection lost")
        self.now += 100000
        reopened = m.MaintenanceStore(self.path, clock=lambda: self.now)
        self.assertTrue(reopened.status()["fenced"])
        self.assertEqual(len(reopened.status()["activities"]), 1)
        self.assertFalse(reopened.prepare_order(self.owner, order["logical_intent"], self.request_hash)["send_allowed"])

    def test_stable_ids_are_different_between_logical_intents(self):
        a = self.prepare("slot-1:BTC:open")
        b = self.prepare("slot-2:BTC:open")
        self.assertNotEqual(a["client_order_id"], b["client_order_id"])
        self.assertLessEqual(len(a["client_order_id"]), 32)
        self.assertTrue(a["client_order_id"].isalnum())

    def test_fenced_new_order_refused_even_with_existing_parent(self):
        parent = self.store.admit(self.owner, "queued-order-work")
        newer = self.next_binding()
        self.request(newer)
        with self.assertRaises(m.AdmissionClosed):
            self.prepare()
        with self.assertRaises(m.AdmissionClosed):
            self.store.prepare_order(self.owner, "already-admitted", self.request_hash, parent_id=parent)
        self.assertEqual(self.store.status()["orders"], [])
        self.assertEqual(len(self.store.status()["activities"]), 1)
        self.store.finish(parent, self.owner)
        self.ack_all(newer)
        self.store.pause(newer, self.proof(newer))
        self.assertEqual(self.store.status()["phase"], "PAUSED")

    def test_unknown_reconciles_only_exact_fresh_verified_broker_evidence(self):
        order = self.prepare()
        self.store.record_order(self.owner, order["logical_intent"], "UNKNOWN")
        base = dict(client_order_id=order["client_order_id"], request_hash=self.request_hash,
                    broker_order_id="broker-1", verified=True, captured_at=self.now)
        for change in ({"verified": False}, {"captured_at": None}, {"captured_at": self.now - 31},
                       {"client_order_id": "wrong"}, {"request_hash": "f" * 64}):
            with self.subTest(change=change), self.assertRaises(m.MaintenanceError):
                self.store.reconcile_order(self.binding, self.owner, order["logical_intent"], "ACKNOWLEDGED", **{**base, **change})
        self.store.reconcile_order(self.binding, self.owner, order["logical_intent"], "ACKNOWLEDGED", **base)
        self.assertEqual(self.store.status()["activities"], [])
        self.assertFalse(self.prepare()["send_allowed"])

    def test_partial_identity_cannot_be_erased(self):
        order = self.prepare()
        self.store.record_order(self.owner, order["logical_intent"], "PARTIAL", "broker-1")
        with self.assertRaises(m.MaintenanceError):
            self.store.record_order(self.owner, order["logical_intent"], "UNKNOWN")
        self.assertEqual(self.store.status()["orders"][0]["broker_order_id"], "broker-1")


if __name__ == "__main__":
    unittest.main()
