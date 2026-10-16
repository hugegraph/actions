"""Reject false-positive Gremlin responses and unchanged recovery resources."""

import copy
import importlib.util
from pathlib import Path
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/helm_smoke_checks.py"
SPEC = importlib.util.spec_from_file_location("helm_smoke_checks", SCRIPT)
checks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checks)


class ResponseTests(unittest.TestCase):
    def test_success_requires_the_completed_phase_marker(self):
        checks.check_response({"status": {"code": 200},
                               "result": {"data": ["helm-ci verify passed"]}}, "verify")

    def test_http_success_does_not_hide_gremlin_failure_or_wrong_phase(self):
        for response in ({"status": {"code": 597}, "result": {"data": []}},
                         {"status": {"code": 200}, "result": {"data": []}},
                         {"status": {"code": 200}, "result": {"data": ["helm-ci exercise passed"]}},
                         {}):
            with self.subTest(response=response), self.assertRaises(ValueError):
                checks.check_response(response, "verify")


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.before = {"items": [
            {"metadata": {"name": component, "uid": component + "-old", "labels": {
                "app.kubernetes.io/component": component}},
             "spec": {"volumes": ([{"persistentVolumeClaim": {"claimName": "store-data"}}]
                                  if component == "store" else [])},
             "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
            for component in ("store", "server")]}
        self.after = copy.deepcopy(self.before)
        for pod in self.after["items"]:
            pod["metadata"]["uid"] += "-replacement"
        self.claims = {"items": [
            {"metadata": {"name": name, "uid": name + "-uid"},
             "spec": {"volumeName": name + "-volume"}, "status": {"phase": "Bound"}}
            for name in ("pd-data", "store-data")]}

    def validate(self, after_claims=None):
        return checks.check_recovery(self.before, self.after, self.claims,
                                     after_claims if after_claims is not None else self.claims)

    def test_new_ready_pods_with_the_same_claims_pass(self):
        result = self.validate()
        self.assertEqual(set(result["pods"]), {"store", "server"})
        self.assertEqual(result["claims"]["store-data"]["uid"], "store-data-uid")

    def test_ready_original_pods_do_not_count_as_recreated(self):
        for i in (0, 1):
            self.after = copy.deepcopy(self.before)
            self.after["items"][1 - i]["metadata"]["uid"] += "-replacement"
            with self.subTest(component=i), self.assertRaisesRegex(ValueError, "not recreated"):
                self.validate()

    def test_missing_duplicate_or_unready_replacement_fails(self):
        for mutate in (lambda p: p["items"].pop(),
                       lambda p: p["items"].append(copy.deepcopy(p["items"][0])),
                       lambda p: p["items"][0]["status"].update(conditions=[])):
            self.setUp()
            mutate(self.after)
            with self.assertRaises(ValueError):
                self.validate()

    def test_pod_using_a_different_claim_fails(self):
        self.after["items"][0]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "new-store-data"
        with self.assertRaisesRegex(ValueError, "persistent claims"):
            self.validate()

    def test_replaced_claim_volume_or_lost_binding_fails(self):
        for field, key, value in (("metadata", "uid", "new-uid"),
                                  ("spec", "volumeName", "new-volume"),
                                  ("status", "phase", "Pending")):
            after_claims = copy.deepcopy(self.claims)
            after_claims["items"][1][field][key] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "claim or bound volume changed"):
                self.validate(after_claims)

    def test_missing_claim_fails(self):
        with self.assertRaisesRegex(ValueError, "same two persistent claims"):
            self.validate({"items": self.claims["items"][:1]})


if __name__ == "__main__":
    unittest.main()
