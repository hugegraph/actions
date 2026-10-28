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
        cls.steps = cls.workflow["jobs"]["publish"]["steps"]
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

    def test_vermeer_source_smoke_precedes_publication(self):
        wrapper = yaml.safe_load((ROOT / ".github/workflows/publish_latest_vermeer_image.yml").read_text())
        config = wrapper["jobs"]["publish"]["with"]
        matrix = json.loads(config["build_matrix_json"])
        self.assertEqual(1, len(matrix))
        self.assertTrue(matrix[0]["smoke_test"])
        self.assertIn('bash vermeer/scripts/test-runtime-image.sh "$IMAGE_URL"', matrix[0]["smoke_test_cmd"])
        smoke = next(index for index, step in enumerate(self.steps)
                     if step["name"].startswith("Run smoke test"))
        for index, step in enumerate(self.steps):
            if step["name"].startswith("Build and push image"):
                self.assertLess(smoke, index)
        self.assertIn("inputs.publish == true", config["publish"])
        self.assertIn("inputs.image_tag", config["enable_hash_gate"])


if __name__ == "__main__":
    unittest.main()
