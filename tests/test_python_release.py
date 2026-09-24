"""Exercise release failures without credentials, package indexes, or uploads."""

import importlib.util
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/python_release.py"
SPEC = importlib.util.spec_from_file_location("release", SCRIPT)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
SHA = "a" * 40


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dist = self.root / "dist"
        self.dist.mkdir()
        self.wheel = self.dist / "hugegraph_python-1.7.0-py3-none-any.whl"
        self.sdist = self.dist / "hugegraph_python-1.7.0.tar.gz"
        self.artifacts()
        self.env = patch.dict(os.environ, {"GITHUB_OUTPUT": str(self.root / "output")})
        self.env.start()
        self.addCleanup(self.env.stop)
        release.manifest(self.dist, SHA, "1.7.0")
        self.manifest_sha = release.digest(self.dist / "manifest.json")

    def artifacts(self, name="hugegraph-python", version="1.7.0"):
        metadata = f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n".encode()
        with zipfile.ZipFile(self.wheel, "w") as archive:
            archive.writestr("hugegraph_python-1.7.0.dist-info/METADATA", metadata)
        with tarfile.open(self.sdist, "w:gz") as archive:
            member = tarfile.TarInfo("hugegraph_python-1.7.0/PKG-INFO")
            member.size = len(metadata)
            archive.addfile(member, io.BytesIO(metadata))

    def publish(self):
        release.publish(self.dist, SHA, "1.7.0", self.manifest_sha, "testpypi")

    def test_exact_artifacts_verify(self):
        (self.dist / ".gitignore").write_text("*")
        self.assertEqual(
            len(release.verify(self.dist, SHA, "1.7.0", self.manifest_sha)), 2
        )

    def test_tampered_manifest_rejected_before_network(self):
        (self.dist / "manifest.json").write_text("{}")
        with (
            patch.object(release, "remote_files") as remote,
            self.assertRaisesRegex(ValueError, "Manifest hash"),
        ):
            self.publish()
        remote.assert_not_called()

    def test_modified_artifact_rejected(self):
        with zipfile.ZipFile(self.wheel, "a") as archive:
            archive.writestr("changed", "changed")
        with self.assertRaisesRegex(ValueError, "Manifest contents"):
            self.publish()

    def test_source_or_version_mismatch_rejected(self):
        for sha, version in [("b" * 40, "1.7.0"), (SHA, "1.8.0")]:
            with self.subTest(sha=sha, version=version), self.assertRaises(ValueError):
                release.verify(self.dist, sha, version, self.manifest_sha)

    def test_wrong_distribution_or_version_rejected(self):
        for name, version in [
            ("hugegraph-python-client", "1.7.0"),
            ("hugegraph-python", "1.8.0"),
        ]:
            self.artifacts(name, version)
            with (
                self.subTest(name=name, version=version),
                self.assertRaisesRegex(ValueError, "metadata"),
            ):
                release.manifest(self.dist, SHA, "1.7.0")

    def test_extra_missing_and_symlink_files_rejected(self):
        extra = self.dist / "extra.txt"
        extra.write_text("extra")
        with self.assertRaises(ValueError):
            self.publish()
        extra.unlink()
        self.wheel.unlink()
        with self.assertRaises(ValueError):
            self.publish()
        self.wheel.symlink_to(self.sdist)
        with self.assertRaisesRegex(ValueError, "regular"):
            self.publish()

    def test_conflict_in_last_file_prevents_all_uploads(self):
        # Wheel is missing; conflicting sdist sorts last. No partial upload allowed.
        with (
            patch.object(
                release, "remote_files", return_value={self.sdist.name: "b" * 64}
            ),
            patch.object(release.subprocess, "run") as upload,
            self.assertRaisesRegex(ValueError, "Remote hash conflict"),
        ):
            self.publish()
        upload.assert_not_called()

    def test_identical_retry_is_noop_without_token(self):
        files = release.inventory(self.dist, "1.7.0")
        with (
            patch.object(release, "remote_files", return_value=files),
            patch.object(release.subprocess, "run") as upload,
            patch.dict(os.environ, {}, clear=True),
        ):
            self.publish()
        upload.assert_not_called()

    def test_partial_retry_uploads_only_missing_file(self):
        with (
            patch.object(
                release,
                "remote_files",
                return_value={self.wheel.name: release.digest(self.wheel)},
            ),
            patch.object(release.subprocess, "run") as upload,
            patch.dict(os.environ, {"UV_PUBLISH_TOKEN": "test-placeholder"}),
        ):
            self.publish()
        self.assertEqual(
            upload.call_args.args[0],
            [
                "uv",
                "publish",
                "--no-config",
                "--trusted-publishing",
                "never",
                "--publish-url",
                "https://test.pypi.org/legacy/",
                "--check-url",
                "https://test.pypi.org/simple/",
                str(self.sdist.resolve()),
            ],
        )

    def test_missing_token_fails_before_upload(self):
        with (
            patch.object(release, "remote_files", return_value={}),
            patch.object(release.subprocess, "run") as upload,
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(ValueError, "Missing environment token"),
        ):
            self.publish()
        upload.assert_not_called()

    def test_upload_failure_propagates_without_retry(self):
        with (
            patch.object(release, "remote_files", return_value={}),
            patch.object(
                release.subprocess,
                "run",
                side_effect=subprocess.CalledProcessError(1, "uv"),
            ) as upload,
            patch.dict(os.environ, {"UV_PUBLISH_TOKEN": "test-placeholder"}),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            self.publish()
        self.assertEqual(upload.call_count, 1)

    def test_only_404_means_missing_release(self):
        for status in (404, 403, 429, 500):
            error = urllib.error.HTTPError(
                "https://test.pypi.org", status, "error", {}, None
            )
            with patch.object(release.urllib.request, "urlopen", side_effect=error):
                if status == 404:
                    self.assertEqual(release.remote_files("testpypi", "1.7.0"), {})
                else:
                    with self.assertRaises(urllib.error.HTTPError):
                        release.remote_files("testpypi", "1.7.0")

    def test_remote_hash_required(self):
        body = io.BytesIO(
            json.dumps(
                {"urls": [{"filename": self.wheel.name, "digests": {"sha256": ""}}]}
            ).encode()
        )
        with (
            patch.object(release.urllib.request, "urlopen", return_value=body),
            self.assertRaisesRegex(ValueError, "remote SHA-256"),
        ):
            release.remote_files("pypi", "1.7.0")

    def test_annotated_and_lightweight_tag_resolution(self):
        for annotated in (False, True):
            responses = [{"object": {"type": "commit", "sha": SHA}}]
            if annotated:
                responses.insert(0, {"object": {"type": "tag", "sha": "b" * 40}})
            with patch.object(release, "github", side_effect=responses) as github:
                release.resolve("refs/tags/1.7.0", "pypi")
                self.assertEqual(github.call_args_list[0].args, ("git/ref/tags/1.7.0",))

    def test_official_missing_tag_cannot_fall_back_to_branch(self):
        with (
            patch.object(
                release, "github", side_effect=subprocess.CalledProcessError(1, "gh")
            ),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            release.resolve("main", "pypi")

    def test_branch_resolves_exact_commit(self):
        with patch.object(release, "github", return_value={"sha": SHA}) as github:
            release.resolve("refs/heads/cx-python-release", "testpypi")
        github.assert_called_once_with("commits/refs%2Fheads%2Fcx-python-release")

    def test_metadata_guards_before_build(self):
        module = self.root / release.MODULE
        module.mkdir()
        project = module / "pyproject.toml"
        project.write_text(
            '[project]\nname="hugegraph-python-client"\nversion="1.7.0"\n'
        )
        with self.assertRaisesRegex(ValueError, "Distribution name"):
            release.metadata(self.root, "testpypi", "")
        project.write_text('[project]\nname="hugegraph-python"\nversion="1.7.0"\n')
        for tag in ("main", "client-v1.7.0", "v1.7.0", "", "1.7.0-rc1"):
            with (
                self.subTest(tag=tag),
                self.assertRaisesRegex(ValueError, "Tag/version"),
            ):
                release.metadata(self.root, "pypi", tag)
        release.metadata(self.root, "pypi", "1.7.0")


if __name__ == "__main__":
    unittest.main()
