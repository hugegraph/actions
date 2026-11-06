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

"""Measure image size, pull time, HTTP-ready startup, and container memory."""

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
        # Give the freshly ready container a brief, identical settling interval.
        time.sleep(1)
        container_memory_text = run(
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{.MemUsage}}",
            name,
        ).stdout.strip()
        return {
            "round": round_number,
            "ready_seconds": round(ready_seconds, 3),
            "http_status": status,
            "container_memory": container_memory_text,
            "container_memory_bytes": parse_memory_bytes(container_memory_text),
        }
    finally:
        run("docker", "rm", "-f", "-v", name, check=False)


def benchmark_image(label: str, image: str, rounds: int, timeout_seconds: float) -> dict[str, Any]:
    """Benchmark one image without discarding metadata when startup fails."""

    result: dict[str, Any] = {
        "label": label,
        "image": image,
        "status": "failed",
        "starts": [],
    }

    print(f"::group::Pull {label} ({image})", flush=True)
    pull_started = time.monotonic()
    try:
        pull = run("docker", "pull", image)
        result["pull_seconds"] = round(time.monotonic() - pull_started, 3)
        print(pull.stdout, end="")
        print(pull.stderr, end="", file=sys.stderr)
    except Exception as error:  # noqa: BLE001 - preserve operational diagnostics in the artifact
        result["pull_seconds"] = round(time.monotonic() - pull_started, 3)
        result["error"] = str(error)
        return result
    finally:
        print("::endgroup::", flush=True)

    try:
        result["local_size_bytes"] = int(
            run("docker", "image", "inspect", "--format", "{{.Size}}", image).stdout.strip()
        )
        result["repo_digests"] = json.loads(
            run(
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .RepoDigests}}",
                image,
            ).stdout
        )
    except Exception as error:  # noqa: BLE001 - preserve operational diagnostics in the artifact
        result["error"] = str(error)
        return result

    for round_number in range(1, rounds + 1):
        print(f"Benchmarking {label} startup round {round_number}/{rounds}", flush=True)
        try:
            result["starts"].append(
                benchmark_start(image, label, round_number, timeout_seconds)
            )
        except Exception as error:  # noqa: BLE001 - the failed image is the benchmark result
            result["starts"].append({"round": round_number, "error": str(error)})
            result["error"] = str(error)
            print(f"{label} startup failed: {error}", file=sys.stderr, flush=True)
            break

    successful_starts = [entry for entry in result["starts"] if "ready_seconds" in entry]
    if successful_starts:
        result["median_ready_seconds"] = round(
            statistics.median(entry["ready_seconds"] for entry in successful_starts),
            3,
        )
        result["median_container_memory_bytes"] = round(
            statistics.median(
                entry["container_memory_bytes"] for entry in successful_starts
            )
        )
    if len(successful_starts) == rounds:
        result["status"] = "ready"
    return result


def mib(value: int | float) -> float:
    return value / 1024**2


def concise_error(value: str) -> str:
    """Return the most useful final diagnostic line for the Markdown summary."""

    lines = [line.strip() for line in value.splitlines() if line.strip()]
    return lines[-1] if lines else "unknown error"


def optional_seconds(value: Any) -> str:
    return f"{value:.3f}s" if isinstance(value, int | float) else "—"


def optional_mib(value: Any) -> str:
    return f"{mib(value):.1f} MiB" if isinstance(value, int | float) else "—"


def render_summary(results: dict[str, Any]) -> str:
    lines = [
        "## HugeGraph AI image benchmark",
        "",
        "The images are pulled once, then each container is started repeatedly on the same runner. ",
        "HTTP-ready time ends at the first response from `/`; Docker container memory usage is sampled one second later.",
        "A startup failure is recorded as a benchmark result instead of discarding successful image data.",
        "",
        "| Image | Status | Local size | Pull | Startup runs | Median startup | Median container memory |",
        "| --- | --- | ---: | ---: | --- | ---: | ---: |",
    ]
    for image in results["images"]:
        starts = ", ".join(
            optional_seconds(entry.get("ready_seconds"))
            if "ready_seconds" in entry
            else f"round {entry['round']}: failed"
            for entry in image["starts"]
        ) or "—"
        status = "✅ ready" if image["status"] == "ready" else "❌ failed"
        lines.append(
            f"| `{image['image']}` | {status} | {optional_mib(image.get('local_size_bytes'))} | "
            f"{optional_seconds(image.get('pull_seconds'))} | {starts} | "
            f"{optional_seconds(image.get('median_ready_seconds'))} | "
            f"{optional_mib(image.get('median_container_memory_bytes'))} |"
        )

    comparison = results.get("comparison", {})
    if comparison:
        lines.extend(["", "### Binary / Python ratios", ""])
        if "local_size_ratio" in comparison:
            lines.append(f"- Local image size: **{comparison['local_size_ratio']:.2f}×**")
        if "startup_ratio" in comparison:
            lines.append(
                f"- Median HTTP-ready startup: **{comparison['startup_ratio']:.2f}×**"
            )
        if "container_memory_ratio" in comparison:
            lines.append(
                f"- Median container memory: **{comparison['container_memory_ratio']:.2f}×**"
            )

    failures = [image for image in results["images"] if image.get("error")]
    if failures:
        lines.extend(["", "### Startup or pull failures", ""])
        for image in failures:
            lines.append(f"- `{image['image']}`: `{concise_error(image['error'])}`")
    return "\n".join(lines) + "\n"


def build_comparison(images: list[dict[str, Any]]) -> dict[str, float]:
    by_label = {entry["label"]: entry for entry in images}
    if not {"rag", "rag-bin"} <= by_label.keys():
        return {}

    source = by_label["rag"]
    binary = by_label["rag-bin"]
    comparison: dict[str, float] = {}
    if source.get("local_size_bytes") and binary.get("local_size_bytes"):
        comparison["local_size_ratio"] = round(
            binary["local_size_bytes"] / source["local_size_bytes"],
            3,
        )
    if source.get("median_ready_seconds") and binary.get("median_ready_seconds"):
        comparison["startup_ratio"] = round(
            binary["median_ready_seconds"] / source["median_ready_seconds"],
            3,
        )
    if source.get("median_container_memory_bytes") and binary.get(
        "median_container_memory_bytes"
    ):
        comparison["container_memory_ratio"] = round(
            binary["median_container_memory_bytes"]
            / source["median_container_memory_bytes"],
            3,
        )
    return comparison


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

    image_results = [
        benchmark_image(label, image, args.rounds, args.timeout)
        for label, image in images
    ]
    results: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "runner": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "rounds": args.rounds,
        "timeout_seconds": args.timeout,
        "all_images_ready": all(image["status"] == "ready" for image in image_results),
        "images": image_results,
        "comparison": build_comparison(image_results),
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
