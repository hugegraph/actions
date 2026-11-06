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

"""Verify that AI images use one source revision and independent checkpoints."""

from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/publish_latest_ai_image.yml"
REUSABLE_WORKFLOW = ROOT / ".github/workflows/_publish_image_reusable.yml"


class AiImagePublisherTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Load workflow contracts once for focused structural checks."""

        cls.workflow = yaml.safe_load(WORKFLOW.read_text())
        cls.jobs = cls.workflow["jobs"]
        cls.preparer = cls.jobs["prepare"]
        cls.publisher = cls.jobs["publish"]
        cls.matrix = cls.publisher["strategy"]["matrix"]["include"]
        cls.prepare_step = cls.preparer["steps"][0]
        cls.prepare_script = cls.prepare_step["run"]
        cls.reusable_text = REUSABLE_WORKFLOW.read_text()

    def test_each_image_calls_the_reusable_publisher_independently(self):
        """Keep a slow or failed binary build isolated from the Python image."""

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
        self.assertNotIn("rag-bin", self.publisher.get("if", ""))

    def test_both_images_use_one_immutable_source_revision(self):
        """Resolve a moving branch once, then pass the same SHA to both legs."""

        self.assertEqual("prepare", self.publisher["needs"])
        self.assertEqual(
            "${{ needs.prepare.outputs.source_sha }}",
            self.publisher["with"]["source_ref"],
        )
        self.assertEqual(
            "${{ needs.prepare.outputs.default_source }}",
            self.publisher["with"]["default_source"],
        )
        self.assertEqual(
            "${{ steps.prepare.outputs.source_sha }}",
            self.preparer["outputs"]["source_sha"],
        )
        self.assertIn('git ls-remote "$source_url"', self.prepare_script)
        self.assertIn('echo "source_sha=$source_sha"', self.prepare_script)
        self.assertIn('default_source="${SOURCE_REPOSITORY}@${source_sha}"', self.prepare_script)

    def test_each_image_has_an_independent_hash_marker(self):
        """Seed and update each checkpoint without coupling build success."""

        entries = {entry["module"]: entry for entry in self.matrix}
        self.assertEqual("LAST_AI_RAG_HASH", entries["rag"]["last_hash_name"])
        self.assertEqual("LAST_AI_RAG_BIN_HASH", entries["rag-bin"]["last_hash_name"])
        self.assertEqual(
            "${{ needs.prepare.outputs.rag_hash }}",
            entries["rag"]["last_hash_value"],
        )
        self.assertEqual(
            "${{ needs.prepare.outputs.rag_bin_hash }}",
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
        self.assertIn('initial_hash="${LEGACY_HASH:-uninitialized}"', self.prepare_script)
        self.assertIn('rag_hash="${RAG_HASH:-$initial_hash}"', self.prepare_script)
        self.assertIn('rag_bin_hash="${RAG_BIN_HASH:-$initial_hash}"', self.prepare_script)

    def test_checkpoint_initialization_is_scoped_and_visible_to_this_run(self):
        """Avoid writes for validation builds and avoid stale vars context reads."""

        self.assertIn(
            'if [ "$enable_hash_gate" = "true" ] && [ "$PUBLISH" = "true" ]',
            self.prepare_script,
        )
        self.assertIn('[ -z "$RAG_HASH" ] || [ -z "$RAG_BIN_HASH" ]', self.prepare_script)
        self.assertIn(
            "gh variable set LAST_AI_RAG_HASH --body \"$rag_hash\" --repo hugegraph/actions",
            self.prepare_script,
        )
        self.assertIn(
            "gh variable set LAST_AI_RAG_BIN_HASH --body \"$rag_bin_hash\" --repo hugegraph/actions",
            self.prepare_script,
        )
        self.assertIn('echo "rag_hash=$rag_hash"', self.prepare_script)
        self.assertIn('echo "rag_bin_hash=$rag_bin_hash"', self.prepare_script)

    def test_reusable_hash_update_remains_success_gated(self):
        """Never checkpoint an image unless its own publish job succeeded."""

        self.assertIn("needs.publish.result == 'success'", self.reusable_text)
        self.assertIn("if: ${{ !cancelled()", self.reusable_text)

    def test_concurrency_keeps_running_and_newest_pending_request(self):
        """Serialize writes without accumulating obsolete latest builds."""

        concurrency = self.workflow["concurrency"]
        self.assertIn("publish-ai-latest-", concurrency["group"])
        self.assertIn("inputs.image_tag", concurrency["group"])
        self.assertNotIn("inputs.source_ref", concurrency["group"])
        self.assertIs(False, concurrency["cancel-in-progress"])
        self.assertNotIn("queue", concurrency)

    def test_workflow_uses_read_only_default_permissions(self):
        """Keep the default GitHub token read-only for every caller job."""

        self.assertEqual({"contents": "read"}, self.workflow["permissions"])


if __name__ == "__main__":
    unittest.main()
