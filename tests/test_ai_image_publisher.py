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
        cls.workflow = yaml.safe_load(WORKFLOW.read_text())
        cls.jobs = cls.workflow["jobs"]
        cls.initializer = cls.jobs["initialize_hash_variables"]
        cls.publisher = cls.jobs["publish"]
        cls.matrix = cls.publisher["strategy"]["matrix"]["include"]

    def test_each_image_calls_the_reusable_publisher_independently(self):
        self.assertEqual("./.github/workflows/_publish_image_reusable.yml",
                         self.publisher["uses"])
        self.assertIs(False, self.publisher["strategy"]["fail-fast"])
        self.assertEqual({"rag", "rag-bin"}, {entry["module"] for entry in self.matrix})
        self.assertEqual(2, len(self.matrix))
        self.assertIn("matrix.module", self.publisher["with"]["build_matrix_json"])

    def test_each_image_has_an_independent_hash_marker(self):
        entries = {entry["module"]: entry for entry in self.matrix}
        self.assertEqual("LAST_AI_RAG_HASH", entries["rag"]["last_hash_name"])
        self.assertEqual("LAST_AI_RAG_BIN_HASH", entries["rag-bin"]["last_hash_name"])
        self.assertEqual("${{ vars.LAST_AI_RAG_HASH }}",
                         entries["rag"]["last_hash_value"])
        self.assertEqual("${{ vars.LAST_AI_RAG_BIN_HASH }}",
                         entries["rag-bin"]["last_hash_value"])
        self.assertEqual("${{ matrix.last_hash_name }}",
                         self.publisher["with"]["last_hash_name"])
        self.assertEqual("${{ matrix.last_hash_value }}",
                         self.publisher["with"]["last_hash_value"])

    def test_missing_markers_are_initialized_once_from_the_legacy_hash(self):
        condition = self.initializer["if"]
        script = self.initializer["steps"][0]["run"]
        environment = self.initializer["steps"][0]["env"]
        self.assertIn("vars.LAST_AI_RAG_HASH == ''", condition)
        self.assertIn("vars.LAST_AI_RAG_BIN_HASH == ''", condition)
        self.assertEqual("${{ vars.LAST_AI_HASH }}", environment["LEGACY_HASH"])
        self.assertIn("gh variable set LAST_AI_RAG_HASH", script)
        self.assertIn("gh variable set LAST_AI_RAG_BIN_HASH", script)

    def test_failed_binary_publish_cannot_cancel_or_block_the_rag_publish(self):
        self.assertIs(False, self.publisher["strategy"]["fail-fast"])
        self.assertEqual("initialize_hash_variables", self.publisher["needs"])
        condition = self.publisher["if"]
        self.assertIn("needs.initialize_hash_variables.result == 'success'", condition)
        self.assertIn("needs.initialize_hash_variables.result == 'skipped'", condition)
        self.assertNotIn("rag-bin", condition)

    def test_duplicate_runs_are_queued_instead_of_wasting_builds(self):
        concurrency = self.workflow["concurrency"]
        self.assertIn("publish-ai-latest-", concurrency["group"])
        self.assertIs(False, concurrency["cancel-in-progress"])


if __name__ == "__main__":
    unittest.main()
