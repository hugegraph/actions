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

"""Workflow policy tests for the explicit AI image benchmark."""

from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_WORKFLOW = ROOT / ".github/workflows/benchmark_ai_images.yml"
CHECK_WORKFLOW = ROOT / ".github/workflows/check_image_publisher.yml"


class AiImageBenchmarkWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Load benchmark and lightweight check workflows once."""

        cls.benchmark = yaml.safe_load(BENCHMARK_WORKFLOW.read_text())
        cls.checks = yaml.safe_load(CHECK_WORKFLOW.read_text())
        cls.benchmark_steps = cls.benchmark["jobs"]["benchmark"]["steps"]

    @staticmethod
    def triggers(workflow):
        """Return workflow triggers despite YAML 1.1 parsing `on` as boolean."""

        return workflow.get("on", workflow.get(True))

    def test_runtime_benchmark_is_manual_only(self):
        """Never pull and start production images during ordinary PR checks."""

        self.assertEqual(
            {"workflow_dispatch"},
            set(self.triggers(self.benchmark)),
        )
        self.assertNotIn("benchmark", self.checks["jobs"])
        self.assertNotIn(
            "Select optional checks",
            {step.get("name") for step in self.checks["jobs"]["checks"]["steps"]},
        )

    def test_failed_image_readiness_fails_the_manual_run(self):
        """Keep the JSON report while making a broken image visibly red."""

        readiness = next(
            step
            for step in self.benchmark_steps
            if step.get("name") == "Require all images to become ready"
        )
        self.assertIn("all_images_ready", readiness["run"])
        self.assertIn("raise SystemExit", readiness["run"])

        upload = next(
            step
            for step in self.benchmark_steps
            if step.get("name") == "Upload benchmark result"
        )
        self.assertEqual("${{ always() }}", upload["if"])


if __name__ == "__main__":
    unittest.main()
