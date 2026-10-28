# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements. See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License. You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Execute the real publication-report step and verify Vermeer release gates."""

import json
import os
import re
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/_publish_image_reusable.yml"
DIGEST = "sha256:" + "a" * 64


class VermeerPublisherTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.safe_load(WORKFLOW.read_text())
        cls.jobs = cls.workflow["jobs"]
        cls.steps = cls.jobs["publish"]["steps"]
        cls.smoke_steps = cls.jobs["smoke"]["steps"]
        cls.report = next(step["run"] for step in cls.steps
                          if step["name"].startswith("Record image provenance"))

    def execute_report(self, published, digest):
        with tempfile.TemporaryDirectory(prefix="vermeer-publisher-") as directory:
            path = Path(directory)
            environment = dict(os.environ, IMAGE_URL="registry.example:5000/hugegraph/vermeer:helm-tested",
                               IMAGE_DIGEST=digest, IMAGE_PLATFORMS="linux/amd64,linux/arm64",
                               SOURCE_REPOSITORY="hugegraph/hugegraph-computer", SOURCE_SHA="b" * 40,
                               PUBLISH_IMAGES=str(published).lower(), GITHUB_STEP_SUMMARY=str(path / "summary"))
            result = subprocess.run(["bash", "-euo", "pipefail", "-c", self.report],
                                    cwd=path, env=environment, capture_output=True, text=True, timeout=15)
            report = path / "image-publication.json"
            summary = path / "summary"
            return (result, json.loads(report.read_text()) if report.exists() else None,
                    summary.read_text() if summary.exists() else "")

    def test_published_image_records_the_registry_digest_and_reviewed_source(self):
        result, report, summary = self.execute_report(True, DIGEST)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(DIGEST, report["digest"])
        self.assertEqual("b" * 40, report["source_sha"])
        self.assertEqual(["linux/amd64", "linux/arm64"], report["platforms"])
        self.assertTrue(report["published"])
        self.assertIn("registry.example:5000/hugegraph/vermeer@" + DIGEST, summary)

    def test_build_only_run_does_not_claim_to_have_published_an_image(self):
        result, report, summary = self.execute_report(False, "")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(report["published"])
        self.assertNotIn("Immutable reference:", summary)
        self.assertIn("no image or cache was published", summary)

    def test_missing_or_invalid_published_digest_fails_before_recording_success(self):
        for digest in ("", "sha256:short", "sha256:" + "G" * 64):
            with self.subTest(digest=digest):
                result, report, _ = self.execute_report(True, digest)
                self.assertNotEqual(0, result.returncode)
                self.assertIsNone(report)

    def execute_step(self, script, environment, directory):
        return subprocess.run(["bash", "-euo", "pipefail", "-c", script],
                              cwd=directory, env=dict(os.environ, **environment),
                              capture_output=True, text=True, timeout=15)

    def select_smoke(self, matrix):
        step = next(step for step in self.jobs["prepare"]["steps"]
                    if step.get("id") == "smoke_matrix")
        with tempfile.TemporaryDirectory(prefix="smoke-matrix-") as directory:
            output = Path(directory) / "outputs"
            result = self.execute_step(step["run"], {
                "BUILD_MATRIX_JSON": json.dumps(matrix), "GITHUB_OUTPUT": str(output),
            }, directory)
            self.assertEqual(0, result.returncode, result.stderr)
            return dict(line.split("=", 1) for line in output.read_text().splitlines())

    def evaluate_condition(self, job, prepare="success", update="true", has_smoke="true",
                           smoke="success", cancelled=False, mode="latest",
                           hash_gate="true", publish="success"):
        # Evaluate the actual workflow gate with synthetic GitHub job results.
        expression = self.jobs[job]["if"].removeprefix("${{").removesuffix("}}").strip()
        values = {
            "needs.prepare.result": prepare,
            "needs.prepare.outputs.need_update": update,
            "needs.prepare.outputs.has_smoke": has_smoke,
            "needs.smoke.result": smoke,
            "needs.prepare.outputs.hash_gate_applied": hash_gate,
            "needs.publish.result": publish,
            "inputs.mode": mode,
        }
        expression = re.sub(r"(?:needs|inputs)(?:\.[a-z_]+)+", lambda match: repr(values[match[0]]), expression)
        expression = expression.replace("cancelled()", repr(cancelled))
        expression = expression.replace("&&", " and ").replace("||", " or ")
        expression = re.sub(r"!(?!=)", " not ", expression)
        return eval(expression.strip(), {"__builtins__": {}}, {})

    def test_vermeer_source_smoke_runs_in_the_isolated_job(self):
        wrapper = yaml.safe_load((ROOT / ".github/workflows/publish_latest_vermeer_image.yml").read_text())
        config = wrapper["jobs"]["publish"]["with"]
        matrix = json.loads(config["build_matrix_json"])
        self.assertEqual(1, len(matrix))
        self.assertTrue(matrix[0]["smoke_test"])
        self.assertIn('bash vermeer/scripts/test-runtime-image.sh "$IMAGE_URL"', matrix[0]["smoke_test_cmd"])
        self.assertTrue(any(step["name"].startswith("Run smoke test") for step in self.smoke_steps))
        self.assertNotIn("smoke_test_cmd", json.dumps(self.jobs["publish"]))
        self.assertEqual(["prepare", "smoke"], self.jobs["publish"]["needs"])
        self.assertIn("inputs.publish == true", config["publish"])
        self.assertIn("inputs.image_tag", config["enable_hash_gate"])

    def test_smoke_runner_cannot_publish_or_export_caches_to_the_publisher(self):
        smoke = self.jobs["smoke"]
        self.assertEqual("ubuntu-latest", smoke["runs-on"])
        self.assertEqual({"contents": "read"}, smoke["permissions"])
        self.assertNotIn("secrets.", json.dumps(smoke))
        self.assertNotIn("outputs", smoke)
        for step in self.smoke_steps:
            with self.subTest(step=step["name"]):
                action = step.get("uses", "")
                self.assertNotIn("login-action", action)
                self.assertNotIn("upload-artifact", action)
                self.assertNotIn("cache-to", step.get("with", {}))
                if "docker/build-push-action" in action:
                    self.assertIs(False, step["with"]["push"])
                    self.assertIs(True, step["with"]["load"])
        self.assertEqual("ubuntu-latest", self.jobs["publish"]["runs-on"])
        publisher = json.dumps(self.jobs["publish"])
        self.assertNotIn("download-artifact", publisher)
        self.assertNotIn("needs.smoke.outputs", publisher)
        self.assertNotIn("SMOKE_TEST_CMD", publisher)

    def test_both_jobs_checkout_and_verify_the_same_prepared_commit_before_login(self):
        for job in ("smoke", "publish"):
            steps = self.jobs[job]["steps"]
            checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
            with self.subTest(job=job):
                self.assertEqual("${{ inputs.source_repository }}", checkout["with"]["repository"])
                self.assertEqual("${{ needs.prepare.outputs.checkout_ref }}", checkout["with"]["ref"])
                self.assertIs(False, checkout["with"]["persist-credentials"])
                verify = next(index for index, step in enumerate(steps)
                              if step["name"].startswith("Verify checked-out source"))
                for index, step in enumerate(steps):
                    if "login-action" in step.get("uses", ""):
                        self.assertLess(verify, index)

    def test_source_verification_rejects_a_moved_or_invalid_revision(self):
        script = next(step["run"] for step in self.steps
                      if step["name"].startswith("Verify checked-out source"))
        with tempfile.TemporaryDirectory(prefix="checked-out-source-") as directory:
            subprocess.run(["git", "init", "--quiet", directory], check=True, capture_output=True)
            subprocess.run(["git", "-c", "user.name=Workflow Test", "-c", "user.email=test@example.org",
                            "commit", "--quiet", "--allow-empty", "-m", "fixture"], cwd=directory,
                           check=True, capture_output=True)
            sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=directory, text=True).strip()
            for expected, success in ((sha, True), (sha.upper(), True), ("0" * 40, False), ("master", False)):
                with self.subTest(expected=expected):
                    result = self.execute_step(script, {"EXPECTED_SOURCE_SHA": expected}, directory)
                    self.assertEqual(success, result.returncode == 0, result.stderr)

    def test_mixed_matrix_selects_only_required_smokes(self):
        first = {"module": "first", "smoke_test": True}
        second = {"module": "second", "smoke_test": False}
        third = {"module": "third"}
        selected = self.select_smoke([first, second, third])
        self.assertEqual([first], json.loads(selected["matrix"]))
        self.assertEqual("true", selected["has_smoke"])
        self.assertTrue(self.evaluate_condition("smoke"))
        self.assertTrue(self.evaluate_condition("publish"))

    def test_smoke_build_reads_the_same_cache_channel_without_exporting_it(self):
        smoke_script = next(step["run"] for step in self.smoke_steps
                            if step["name"].startswith("Resolve smoke build parameters"))
        publish_script = next(step["run"] for step in self.steps
                              if step["name"].startswith("Resolve build parameters"))
        cases = (("latest", "latest", "true", "hugegraph/vermeer:buildcache-latest"),
                 ("latest", "candidate", "true", "hugegraph/vermeer:buildcache-latest"),
                 ("latest", "candidate", "false", "hugegraph/vermeer:buildcache-latest"),
                 ("latest", "a" * 128, "true", "hugegraph/vermeer:buildcache-latest"),
                 ("release", "1.2.3", "true", "hugegraph/vermeer:buildcache-release"),
                 ("latest", "candidate", "false", ""))
        for mode, tag, publish, cache in cases:
            with self.subTest(mode=mode, tag=tag, publish=publish, cache=cache), \
                    tempfile.TemporaryDirectory(prefix="smoke-cache-") as directory:
                environment = {
                    "MODE": mode, "VERSION_TAG": tag, "PUBLISH_IMAGES": publish,
                    "COMPONENT": "vermeer", "MODULE": "runtime",
                    "IMAGE_REPO_LATEST": "hugegraph/vermeer", "IMAGE_REPO_RELEASE": "hugegraph/release",
                    "PLATFORMS_LATEST": "linux/amd64,linux/arm64", "PLATFORMS_RELEASE": "linux/amd64",
                    "REGISTRY_CACHE_IMAGE": cache, "GITHUB_STEP_SUMMARY": str(Path(directory) / "summary"),
                }
                outputs = []
                for name, script in (("smoke", smoke_script), ("publish", publish_script)):
                    output = Path(directory) / name
                    result = self.execute_step(script, dict(environment, GITHUB_OUTPUT=str(output)), directory)
                    self.assertEqual(0, result.returncode, result.stderr)
                    outputs.append(dict(line.split("=", 1) for line in output.read_text().splitlines()))
                self.assertEqual(outputs[1]["image_url"], outputs[0]["image_url"])
                self.assertEqual(outputs[1]["cache_from"], outputs[0]["cache_from"])
                self.assertEqual({"image_url", "cache_from"}, set(outputs[0]))

    def test_all_false_or_unrequested_smokes_do_not_block_publication(self):
        release = yaml.safe_load((ROOT / ".github/workflows/publish_release_vermeer_image.yml").read_text())
        release_matrix = json.loads(release["jobs"]["publish"]["with"]["build_matrix_json"])
        for matrix in (release_matrix, [{"module": "legacy", "smoke_test": False}], [{"module": "legacy"}]):
            with self.subTest(matrix=matrix):
                selected = self.select_smoke(matrix)
                self.assertEqual([], json.loads(selected["matrix"]))
                self.assertEqual("false", selected["has_smoke"])
                self.assertFalse(self.evaluate_condition("smoke", has_smoke="false", smoke="skipped"))
                self.assertTrue(self.evaluate_condition("publish", has_smoke="false", smoke="skipped"))
        self.assertEqual("${{ fromJson(needs.prepare.outputs.smoke_matrix) }}",
                         self.jobs["smoke"]["strategy"]["matrix"]["include"])

    def test_hash_marker_updates_after_optional_skipped_smoke_but_not_failed_or_cancelled_publication(self):
        self.assertTrue(self.evaluate_condition("update_latest_hash", has_smoke="false", smoke="skipped"))
        for overrides in ({"publish": "failure"}, {"publish": "skipped"}, {"prepare": "failure"},
                          {"update": "false"}, {"hash_gate": "false"}, {"mode": "release"},
                          {"cancelled": True}):
            with self.subTest(overrides=overrides):
                self.assertFalse(self.evaluate_condition("update_latest_hash", **overrides))

    def test_failed_skipped_or_cancelled_required_smoke_blocks_publication(self):
        for smoke in ("failure", "cancelled", "skipped"):
            with self.subTest(smoke=smoke):
                self.assertFalse(self.evaluate_condition("publish", smoke=smoke))
        self.assertFalse(self.evaluate_condition("publish", has_smoke="false", smoke="failure"))
        self.assertFalse(self.evaluate_condition("publish", has_smoke="false", smoke="success"))
        self.assertFalse(self.evaluate_condition("publish", has_smoke="", smoke="success"))
        for prepare in ("failure", "cancelled", "skipped"):
            with self.subTest(prepare=prepare):
                self.assertFalse(self.evaluate_condition("publish", prepare=prepare))
        self.assertFalse(self.evaluate_condition("publish", update="false"))
        self.assertFalse(self.evaluate_condition("publish", cancelled=True))
        self.assertFalse(self.evaluate_condition("publish", has_smoke="false", smoke="skipped", cancelled=True))


if __name__ == "__main__":
    unittest.main()
