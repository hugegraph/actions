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

"""Verify that the AI images publish and checkpoint independently."""

from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/publish_latest_ai_image.yml"


class AiImagePublisherTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Load the workflow once for the structural contract tests."""

        cls.workflow = yaml.safe_load(WORKFLOW.read_text())
        cls.jobs = cls.workflow["jobs"]
        cls.initializer = cls.jobs["initialize_hash_variables"]
        cls.publisher = cls.jobs["publish"]
        cls.matrix = cls.publisher["strategy"]["matrix"]["include"]

    def test_each_image_calls_the_reusable_publisher_independently(self):
        """Keep the slow binary build isolated from the Python image build."""

        self.assertEqual(
            "./.github/workflows/_publish_image_reusable.yml",
            self.publisher["uses"],
        )
        self.assertIs(False, self.publisher["strategy"]["fail-fast"])
        self.assertEqual(
            {"rag", "rag-bin"},
            {entry["module"] for entry in self.matrix},
        )
        self.assertEqual(2, len(self.matrix))
        self.assertIn("matrix.module", self.publisher["with"]["build_matrix_json"])

    def test_each_image_has_an_independent_hash_marker(self):
        """Checkpoint each image only after that image publishes successfully."""

        entries = {entry["module"]: entry for entry in self.matrix}
        self.assertEqual("LAST_AI_RAG_HASH", entries["rag"]["last_hash_name"])
        self.assertEqual("LAST_AI_RAG_BIN_HASH", entries["rag-bin"]["last_hash_name"])
        self.assertEqual(
            "${{ vars.LAST_AI_RAG_HASH }}",
            entries["rag"]["last_hash_value"],
        )
        self.assertEqual(
            "${{ vars.LAST_AI_RAG_BIN_HASH }}",
            entries["rag-bin"]["last_hash_value"],
        )
        self.assertEqual(
            "${{ matrix.last_hash_name }}",
            self.publisher["with"]["last_hash_name"],
        )
        self.assertEqual(
            "${{ matrix.last_hash_value }}",
            self.publisher["with"]["last_hash_value"],
        )

    def test_missing_markers_are_initialized_only_for_gated_publication(self):
        """Do not require variable-write credentials for validation builds."""

        condition = " ".join(self.initializer["if"].split())
        script = self.initializer["steps"][0]["run"]
        environment = self.initializer["steps"][0]["env"]
        self.assertIn("vars.LAST_AI_RAG_HASH == ''", condition)
        self.assertIn("vars.LAST_AI_RAG_BIN_HASH == ''", condition)
        self.assertIn("inputs.publish == true", condition)
        self.assertIn("'apache/hugegraph-ai'", condition)
        self.assertIn("(inputs.source_ref || 'main') == 'main'", condition)
        self.assertIn("(inputs.image_tag || '') == ''", condition)
        self.assertEqual("${{ vars.LAST_AI_HASH }}", environment["LEGACY_HASH"])
        self.assertIn("gh variable set LAST_AI_RAG_HASH", script)
        self.assertIn("gh variable set LAST_AI_RAG_BIN_HASH", script)

    def test_failed_binary_publish_cannot_cancel_or_block_the_rag_publish(self):
        """Allow either reusable workflow invocation to finish independently."""

        self.assertIs(False, self.publisher["strategy"]["fail-fast"])
        self.assertEqual("initialize_hash_variables", self.publisher["needs"])
        condition = self.publisher["if"]
        self.assertIn("needs.initialize_hash_variables.result == 'success'", condition)
        self.assertIn("needs.initialize_hash_variables.result == 'skipped'", condition)
        self.assertNotIn("rag-bin", condition)

    def test_duplicate_runs_are_queued_instead_of_replaced(self):
        """Preserve every explicit publication request in the concurrency queue."""

        concurrency = self.workflow["concurrency"]
        self.assertIn("publish-ai-latest-", concurrency["group"])
        self.assertIs(False, concurrency["cancel-in-progress"])
        self.assertEqual("max", concurrency["queue"])

    def test_workflow_uses_read_only_default_permissions(self):
        """Keep the default GitHub token read-only for every caller job."""

        self.assertEqual({"contents": "read"}, self.workflow["permissions"])


if __name__ == "__main__":
    unittest.main()
