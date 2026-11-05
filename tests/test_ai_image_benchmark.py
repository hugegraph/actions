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

"""Unit tests for the observational AI image benchmark."""

from __future__ import annotations

import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github/scripts/benchmark_ai_images.py"
SPEC = importlib.util.spec_from_file_location("benchmark_ai_images", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
BENCHMARK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCHMARK)


class AiImageBenchmarkTest(unittest.TestCase):
    def test_parse_docker_memory_units(self):
        self.assertEqual(256 * 1024**2, BENCHMARK.parse_memory_bytes("256MiB / 7.8GiB"))
        self.assertEqual(1_500_000, BENCHMARK.parse_memory_bytes("1.5MB / 8GB"))

    def test_failed_startup_keeps_pull_and_size_results(self):
        def fake_run(*command: str, check: bool = True):
            del check
            if command[:2] == ("docker", "pull"):
                return subprocess.CompletedProcess(command, 0, "pulled\n", "")
            if "{{.Size}}" in command:
                return subprocess.CompletedProcess(command, 0, "1048576\n", "")
            if "{{json .RepoDigests}}" in command:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    '["hugegraph/rag-bin@sha256:test"]\n',
                    "",
                )
            raise AssertionError(f"unexpected command: {command}")

        with (
            mock.patch.object(BENCHMARK, "run", side_effect=fake_run),
            mock.patch.object(
                BENCHMARK,
                "benchmark_start",
                side_effect=RuntimeError("ModuleNotFoundError: missing compiled module"),
            ),
        ):
            result = BENCHMARK.benchmark_image(
                "rag-bin",
                "hugegraph/rag-bin:latest",
                rounds=3,
                timeout_seconds=30,
            )

        self.assertEqual("failed", result["status"])
        self.assertEqual(1048576, result["local_size_bytes"])
        self.assertEqual(1, len(result["starts"]))
        self.assertIn("ModuleNotFoundError", result["error"])
        self.assertNotIn("median_ready_seconds", result)

    def test_comparison_keeps_size_ratio_when_binary_cannot_start(self):
        comparison = BENCHMARK.build_comparison(
            [
                {
                    "label": "rag",
                    "local_size_bytes": 100,
                    "median_ready_seconds": 10,
                    "median_memory_bytes": 20,
                },
                {
                    "label": "rag-bin",
                    "local_size_bytes": 250,
                    "status": "failed",
                },
            ]
        )

        self.assertEqual({"local_size_ratio": 2.5}, comparison)

    def test_summary_surfaces_failed_image_without_hiding_ready_image(self):
        summary = BENCHMARK.render_summary(
            {
                "images": [
                    {
                        "label": "rag",
                        "image": "hugegraph/rag:latest",
                        "status": "ready",
                        "local_size_bytes": 1024**2,
                        "pull_seconds": 1.25,
                        "starts": [
                            {
                                "round": 1,
                                "ready_seconds": 2.5,
                                "memory_bytes": 2 * 1024**2,
                            }
                        ],
                        "median_ready_seconds": 2.5,
                        "median_memory_bytes": 2 * 1024**2,
                    },
                    {
                        "label": "rag-bin",
                        "image": "hugegraph/rag-bin:latest",
                        "status": "failed",
                        "local_size_bytes": 3 * 1024**2,
                        "pull_seconds": 2.5,
                        "starts": [
                            {
                                "round": 1,
                                "error": "ModuleNotFoundError: missing module",
                            }
                        ],
                        "error": "traceback\nModuleNotFoundError: missing module",
                    },
                ],
                "comparison": {"local_size_ratio": 3.0},
            }
        )

        self.assertIn("✅ ready", summary)
        self.assertIn("❌ failed", summary)
        self.assertIn("ModuleNotFoundError: missing module", summary)
        self.assertIn("Local image size: **3.00×**", summary)


if __name__ == "__main__":
    unittest.main()
