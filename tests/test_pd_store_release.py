import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest


WORKFLOW = (Path(__file__).resolve().parents[1] /
            ".github/workflows/publish_release_pd_store_server_image.yml").read_text()


def run_script(name):
    block = WORKFLOW.split("      - name: " + name + "\n", 1)[1]
    block = re.split(r"^  [a-z_]+:|^      - ", block, maxsplit=1, flags=re.MULTILINE)[0]
    return textwrap.dedent(block.split("        run: |\n", 1)[1])


FAKE_REGISTRY = r'''#!/usr/bin/env python3
import hashlib
import json
import os
from pathlib import Path
import sys

path = Path(os.environ["REGISTRY_STATE"])
state = json.loads(path.read_text())
args = sys.argv[1:]

def save():
    path.write_text(json.dumps(state))

if Path(sys.argv[0]).name == "curl":
    url = args[-1]
    if url.endswith("/users/login"):
        print('{"token":"test-token"}')
        sys.exit(0)
    suffix = url.split("/repositories/", 1)[1]
    repo, tag = suffix.rstrip("/").split("/tags/", 1)
    ref = repo + ":" + tag
    method = args[args.index("-X") + 1] if "-X" in args else "GET"
    status = 200 if ref in state["tags"] else 404
    body = json.dumps({"digest":state["tags"].get(ref)})
    if method == "DELETE" and ref in state["tags"]:
        del state["tags"][ref]
        save()
        status, body = 204, ""
    if "-o" in args:
        Path(args[args.index("-o") + 1]).write_text(body)
    if "-w" in args:
        print(status, end="")
    elif body:
        print(body)
elif args[:3] == ["buildx", "imagetools", "inspect"]:
    ref = args[3]
    digest = ref.split("@", 1)[1] if "@" in ref else state["tags"].get(ref)
    if not digest:
        sys.exit(1)
    print(json.dumps({"manifest":{"digest":digest}}))
elif args[:3] == ["buildx", "imagetools", "create"]:
    target = args[args.index("--tag") + 1]
    digest = args[-1].split("@", 1)[1]
    if target == os.environ.get("FAIL_FINAL") and not state.get("failed_once"):
        state["failed_once"] = True
        save()
        sys.exit(42)
    # Buildx wraps a single manifest by default; indexes are copied unchanged.
    if state["media"][digest] == "manifest" and "--prefer-index=false" not in args:
        digest = "sha256:" + hashlib.sha256(("index:" + digest).encode()).hexdigest()
    state["tags"][target] = digest
    save()
elif args[:1] == ["pull"] or args[:2] == ["image", "inspect"]:
    digest = args[-1].split("@", 1)[1]
    architecture = args[args.index("--platform") + 1].split("/")[1]
    if architecture not in state["architectures"][digest]:
        sys.exit(1)
    if args[0] == "image":
        print(json.dumps([{"Os":"linux", "Architecture":architecture, "Config":{"Labels":{
            "org.opencontainers.image.source":"https://github.com/apache/hugegraph",
            "org.opencontainers.image.revision":os.environ["SOURCE_SHA"],
            "org.apache.hugegraph.rocksdb-runtime":state["runtime"][digest]}}}]))
else:
    sys.exit("Unexpected registry command: " + repr(args))
'''


class PairedReleaseTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="paired-release-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        for command in ("docker", "curl"):
            executable = self.directory / command
            executable.write_text(FAKE_REGISTRY)
            executable.chmod(0o755)
        sleep = self.directory / "sleep"
        sleep.write_text("#!/bin/sh\nexit 0\n")
        sleep.chmod(0o755)
        self.base = "ci-release-1.8.0-0123456789ab-10001"
        self.state_file = self.directory / "registry.json"
        self.state = dict(tags={}, media={}, runtime={}, architectures={})
        self.candidates, self.original = {}, {}
        for repo in ("hugegraph/pd", "hugegraph/store", "hugegraph/server", "hugegraph/hugegraph"):
            for suffix in ("", "-topling"):
                candidate = repo + ":" + self.base + suffix
                final = repo + ":1.8.0" + suffix
                digest = "sha256:" + hashlib.sha256(candidate.encode()).hexdigest()
                old = "sha256:" + hashlib.sha256(final.encode()).hexdigest()
                self.candidates[candidate] = digest
                self.original[final] = old
                self.state["tags"][final] = old
                for value in (digest, old):
                    self.state["media"][value] = "manifest" if suffix else "index"
                    self.state["runtime"][value] = ("hstore" if repo.endswith("/server") else
                                                    "topling" if suffix else "standard")
                    self.state["architectures"][value] = ["amd64"] if suffix else ["amd64", "arm64"]

    def execute(self, name, fail_final=""):
        self.state_file.write_text(json.dumps(self.state))
        environment = dict(os.environ, PATH=str(self.directory) + os.pathsep + os.environ["PATH"],
                           REGISTRY_STATE=str(self.state_file), SOURCE_SHA="0123456789abcdef0123456789abcdef01234567",
                           CANDIDATE_BASE=self.base, FINAL_BASE="1.8.0", DOCKERHUB_USERNAME="fixture",
                           DOCKERHUB_PASSWORD="fixture", FAIL_FINAL=fail_final,
                           GITHUB_STEP_SUMMARY=str(self.directory / "summary"))
        result = subprocess.run(["bash", "-c", run_script(name)], env=environment,
                                capture_output=True, text=True)
        self.state = json.loads(self.state_file.read_text())
        return result

    def cleanup(self, promote_result):
        block = WORKFLOW.split("  cleanup_candidates:\n", 1)[1]
        condition = re.search(r"^    if: (.+)$", block, re.MULTILINE).group(1)
        # Evaluate the workflow's job-result equality, without running GitHub Actions.
        match = re.fullmatch(r"\$\{\{ needs\.promote\.result == '(\w+)' }}", condition)
        self.assertIsNotNone(match, "Cleanup must depend on successful paired promotion")
        if promote_result == match.group(1):
            result = self.execute("Remove release candidate tags")
            self.assertEqual(result.returncode, 0, result.stderr)

    def assert_final_candidates(self):
        for candidate, digest in self.candidates.items():
            suffix = "-topling" if candidate.endswith("-topling") else ""
            final = candidate.split(":", 1)[0] + ":1.8.0" + suffix
            self.assertEqual(self.state["tags"][final], digest)

    def test_failed_matrix_retry_preserves_successful_sibling(self):
        successful = {ref: digest for ref, digest in self.candidates.items() if not ref.endswith("-topling")}
        self.state["tags"].update(successful)
        for outcome in ("skipped", "failure", "cancelled"):
            self.cleanup(outcome)
            self.assertEqual({ref: self.state["tags"][ref] for ref in successful}, successful)
        # Only the failed cell reruns; the successful standard tags are not rebuilt.
        self.state["tags"].update({ref: digest for ref, digest in self.candidates.items() if ref.endswith("-topling")})
        result = self.execute("Promote validated standard and Topling sets together")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_final_candidates()
        self.cleanup("success")
        self.assertTrue(all(ref not in self.state["tags"] for ref in self.candidates))
        self.assert_final_candidates()

    def test_failed_promotion_rolls_back_and_retains_retry_inputs(self):
        self.state["tags"].update(self.candidates)
        result = self.execute("Promote validated standard and Topling sets together", "hugegraph/store:1.8.0-topling")
        self.assertNotEqual(result.returncode, 0)
        for ref, digest in self.original.items():
            self.assertEqual(self.state["tags"][ref], digest)
        self.cleanup("failure")
        self.assertTrue(all(self.state["tags"][ref] == digest for ref, digest in self.candidates.items()))
        result = self.execute("Promote validated standard and Topling sets together")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_final_candidates()


if __name__ == "__main__":
    unittest.main()
