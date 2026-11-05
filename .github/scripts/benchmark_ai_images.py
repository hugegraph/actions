#!/usr/bin/env python3

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

"""Measure local image size, pull time, HTTP-ready startup time and ready RSS."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_MEMORY_UNITS = {
    "B": 1,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
}


def run(*command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run one command and retain output for diagnostics."""

    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def parse_named_image(value: str) -> tuple[str, str]:
    """Parse LABEL=IMAGE while keeping Docker tag and digest syntax intact."""

    label, separator, image = value.partition("=")
    if not separator or not label or not image:
        raise argparse.ArgumentTypeError("image must use LABEL=IMAGE syntax")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", label):
        raise argparse.ArgumentTypeError(f"invalid image label: {label}")
    return label, image


def parse_memory_bytes(value: str) -> int:
    """Convert the used side of Docker's `MemUsage` field to bytes."""

    used = value.split("/", maxsplit=1)[0].strip().upper().replace(" ", "")
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([A-Z]+)", used)
    if not match or match.group(2) not in _MEMORY_UNITS:
        raise ValueError(f"Unsupported Docker memory value: {value}")
    return round(float(match.group(1)) * _MEMORY_UNITS[match.group(2)])


def container_state(name: str) -> str:
    return run("docker", "inspect", "--format", "{{.State.Status}}", name).stdout.strip()


def published_port(name: str) -> int | None:
    result = run("docker", "port", name, "8001/tcp", check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return int(result.stdout.strip().splitlines()[0].rsplit(":", maxsplit=1)[1])


def http_status(port: int) -> int | None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/",
        headers={"User-Agent": "hugegraph-ai-image-benchmark/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status
    except urllib.error.HTTPError as error:
        # Any HTTP response proves that the application server is accepting requests.
        return error.code
    except (TimeoutError, urllib.error.URLError, ConnectionError):
        return None


def wait_until_ready(name: str, started_at: float, timeout_seconds: float) -> tuple[float, int]:
    deadline = started_at + timeout_seconds
    last_status: int | None = None
    while time.monotonic() < deadline:
        state = container_state(name)
        if state not in {"created", "running"}:
            logs = run("docker", "logs", name, check=False)
            raise RuntimeError(
                f"Container {name} exited before becoming ready (state={state})\n"
                f"stdout:\n{logs.stdout}\nstderr:\n{logs.stderr}"
            )
        port = published_port(name)
        if port is not None:
            last_status = http_status(port)
            if last_status is not None:
                return time.monotonic() - started_at, last_status
        time.sleep(0.25)

    logs = run("docker", "logs", name, check=False)
    raise TimeoutError(
        f"Container {name} did not answer HTTP within {timeout_seconds:.0f}s "
        f"(last_status={last_status})\nstdout:\n{logs.stdout}\nstderr:\n{logs.stderr}"
    )


def benchmark_start(image: str, label: str, round_number: int, timeout_seconds: float) -> dict[str, Any]:
    name = f"ai-image-bench-{label}-{os.getpid()}-{round_number}"
    run("docker", "rm", "-f", "-v", name, check=False)
    started_at = time.monotonic()
    try:
        run(
            "docker",
            "run",
            "--detach",
            "--name",
            name,
            "--publish",
            "127.0.0.1::8001",
            image,
        )
        ready_seconds, status = wait_until_ready(name, started_at, timeout_seconds)
        # Give the freshly ready process a brief, identical settling interval.
        time.sleep(1)
        memory_text = run("docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", name).stdout.strip()
        return {
            "round": round_number,
            "ready_seconds": round(ready_seconds, 3),
            "http_status": status,
            "memory": memory_text,
            "memory_bytes": parse_memory_bytes(memory_text),
        }
    finally:
        run("docker", "rm", "-f", "-v", name, check=False)


def benchmark_image(label: str, image: str, rounds: int, timeout_seconds: float) -> dict[str, Any]:
    print(f"::group::Pull {label} ({image})", flush=True)
    pull_started = time.monotonic()
    pull = run("docker", "pull", image)
    pull_seconds = time.monotonic() - pull_started
    print(pull.stdout, end="")
    print(pull.stderr, end="", file=sys.stderr)
    print("::endgroup::", flush=True)

    size_bytes = int(run("docker", "image", "inspect", "--format", "{{.Size}}", image).stdout.strip())
    repo_digests = json.loads(run("docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image).stdout)

    starts: list[dict[str, Any]] = []
    for round_number in range(1, rounds + 1):
        print(f"Benchmarking {label} startup round {round_number}/{rounds}", flush=True)
        starts.append(benchmark_start(image, label, round_number, timeout_seconds))

    ready_values = [entry["ready_seconds"] for entry in starts]
    memory_values = [entry["memory_bytes"] for entry in starts]
    return {
        "label": label,
        "image": image,
        "repo_digests": repo_digests,
        "local_size_bytes": size_bytes,
        "pull_seconds": round(pull_seconds, 3),
        "starts": starts,
        "median_ready_seconds": round(statistics.median(ready_values), 3),
        "median_memory_bytes": round(statistics.median(memory_values)),
    }


def mib(value: int | float) -> float:
    return value / 1024**2


def render_summary(results: dict[str, Any]) -> str:
    lines = [
        "## HugeGraph AI image benchmark",
        "",
        "The images are pulled once, then each container is started repeatedly on the same runner. ",
        "HTTP-ready time ends at the first response from `/`; ready RSS is sampled one second later.",
        "",
        "| Image | Local size | Pull | Startup runs | Median startup | Median ready RSS |",
        "| --- | ---: | ---: | --- | ---: | ---: |",
    ]
    for image in results["images"]:
        starts = ", ".join(f"{entry['ready_seconds']:.3f}s" for entry in image["starts"])
        lines.append(
            f"| `{image['image']}` | {mib(image['local_size_bytes']):.1f} MiB | "
            f"{image['pull_seconds']:.1f}s | {starts} | {image['median_ready_seconds']:.3f}s | "
            f"{mib(image['median_memory_bytes']):.1f} MiB |"
        )

    comparison = results.get("comparison")
    if comparison:
        lines.extend(
            [
                "",
                "### Binary / Python ratios",
                "",
                f"- Local image size: **{comparison['local_size_ratio']:.2f}×**",
                f"- Median HTTP-ready startup: **{comparison['startup_ratio']:.2f}×**",
                f"- Median ready RSS: **{comparison['memory_ratio']:.2f}×**",
            ]
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image",
        action="append",
        type=parse_named_image,
        default=[],
        help="image to benchmark in LABEL=IMAGE form (repeatable)",
    )
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--output", type=Path, default=Path("ai-image-benchmark.json"))
    args = parser.parse_args()

    images = args.image or [
        ("rag", "hugegraph/rag:latest"),
        ("rag-bin", "hugegraph/rag-bin:latest"),
    ]
    if args.rounds < 1:
        parser.error("--rounds must be at least 1")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")

    results: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "runner": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "rounds": args.rounds,
        "timeout_seconds": args.timeout,
        "images": [
            benchmark_image(label, image, args.rounds, args.timeout)
            for label, image in images
        ],
    }
    by_label = {entry["label"]: entry for entry in results["images"]}
    if {"rag", "rag-bin"} <= by_label.keys():
        source = by_label["rag"]
        binary = by_label["rag-bin"]
        results["comparison"] = {
            "local_size_ratio": round(binary["local_size_bytes"] / source["local_size_bytes"], 3),
            "startup_ratio": round(binary["median_ready_seconds"] / source["median_ready_seconds"], 3),
            "memory_ratio": round(binary["median_memory_bytes"] / source["median_memory_bytes"], 3),
        }

    args.output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    summary = render_summary(results)
    print(summary)
    if summary_path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary_path).open("a", encoding="utf-8") as stream:
            stream.write(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
