"""Exercise release failures without credentials, package indexes, or uploads."""

import importlib.util
import io
import json
import os
import subprocess
import sys
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

    def test_post_test_verification_rejects_changed_build_output(self):
        command = [
            sys.executable,
            str(SCRIPT),
            "verify",
            "--dist",
            str(self.dist),
            "--sha",
            SHA,
            "--version",
            "1.7.0",
            "--manifest-sha",
            self.manifest_sha,
        ]
        before = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(before.returncode, 0, before.stderr)
        # Simulate test code replacing bytes while retaining valid package metadata.
        with zipfile.ZipFile(self.wheel, "a") as archive:
            archive.writestr("injected.py", "raise RuntimeError('changed artifact')")
        after = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertNotEqual(after.returncode, 0)
        self.assertIn("Manifest contents mismatch", after.stderr)

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

    def test_unexpected_remote_file_prevents_upload_and_noop(self):
        files = release.inventory(self.dist, "1.7.0")
        extra = "hugegraph_python-1.7.0-cp310-cp310-manylinux_2_17_x86_64.whl"
        for remote in ({extra: "b" * 64}, {**files, extra: "b" * 64}):
            with (
                self.subTest(remote=remote),
                patch.object(release, "remote_files", return_value=remote),
                patch.object(release.subprocess, "run") as upload,
                self.assertRaisesRegex(ValueError, "Unexpected remote artifacts"),
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

    def test_source_refs_resolve_to_exact_commit(self):
        for ref in (
            "main",
            "release-1.7",
            "refs/heads/release-1.7",
            "1.7.0",
            "refs/tags/1.7.0",
            SHA,
        ):
            with (
                self.subTest(ref=ref),
                patch.object(release, "github", return_value={"sha": SHA}) as github,
            ):
                release.resolve(ref)
            github.assert_called_once_with(
                "commits/" + release.urllib.parse.quote(ref, safe=""), release.SOURCE
            )
        self.assertIn("source_sha=" + SHA, (self.root / "output").read_text())

    def test_fork_source_resolves_and_unlisted_repository_is_rejected(self):
        with patch.object(release, "github", return_value={"sha": SHA}) as github:
            release.resolve("graph-mcp", "hugegraph/hugegraph-ai")
        github.assert_called_once_with("commits/graph-mcp", "hugegraph/hugegraph-ai")
        with patch.object(release, "github") as github:
            with self.assertRaisesRegex(ValueError, "Unsupported source"):
                release.resolve("main", "untrusted/repo")
        github.assert_not_called()

    def test_mcp_artifacts_bind_component_and_source(self):
        self.wheel.unlink()
        self.sdist.unlink()
        self.wheel = self.dist / "hugegraph_mcp-1.7.0-py3-none-any.whl"
        self.sdist = self.dist / "hugegraph_mcp-1.7.0.tar.gz"
        self.artifacts(name="hugegraph-mcp")
        release.manifest(self.dist, SHA, "1.7.0", "mcp", "hugegraph/hugegraph-ai")
        checksum = release.digest(self.dist / "manifest.json")
        files = release.verify(
            self.dist, SHA, "1.7.0", checksum, "mcp", "hugegraph/hugegraph-ai"
        )
        self.assertEqual(set(files), {self.wheel.name, self.sdist.name})
        with self.assertRaises(ValueError):
            release.verify(
                self.dist, SHA, "1.7.0", checksum, "client", "hugegraph/hugegraph-ai"
            )
        with self.assertRaisesRegex(ValueError, "Manifest contents"):
            release.verify(self.dist, SHA, "1.7.0", checksum, "mcp", release.SOURCE)
        with (
            patch.object(release, "remote_files", return_value={}) as remote,
            patch.object(release.subprocess, "run") as upload,
            patch.dict(os.environ, {"UV_PUBLISH_TOKEN": "test-placeholder"}),
        ):
            release.publish(
                self.dist,
                SHA,
                "1.7.0",
                checksum,
                "pypi",
                "mcp",
                "hugegraph/hugegraph-ai",
            )
        remote.assert_called_once_with("pypi", "1.7.0", "mcp")
        self.assertIn(str(self.wheel.resolve()), upload.call_args.args[0])

    def test_mcp_remote_lookup_uses_mcp_project(self):
        with patch.object(
            release.urllib.request, "urlopen", return_value=io.BytesIO(b'{"urls": []}')
        ) as request:
            self.assertEqual(release.remote_files("pypi", "1.7.1", "mcp"), {})
        request.assert_called_once_with(
            "https://pypi.org/pypi/hugegraph-mcp/1.7.1/json", timeout=30
        )

    def test_mcp_metadata_selects_only_mcp_module(self):
        module = self.root / "hugegraph-mcp"
        module.mkdir()
        (module / "pyproject.toml").write_text(
            '[project]\nname="hugegraph-mcp"\nversion="1.7.1"\n'
        )
        release.metadata(self.root, "pypi", "", "mcp")
        output = (self.root / "output").read_text()
        self.assertIn("module=hugegraph-mcp", output)
        self.assertIn("package=hugegraph-mcp", output)
        self.assertIn("version=1.7.1", output)

    def test_test_client_pins_exact_published_wheel(self):
        version = "1.7.1.2"
        filename = f"hugegraph_python-{version}-py3-none-any.whl"
        url = "https://test-files.pythonhosted.org/packages/hash/" + filename
        wheel = {"filename": filename, "url": url, "digests": {"sha256": "b" * 64}}
        data = {
            "info": {"name": "hugegraph-python", "version": version},
            "urls": [wheel],
        }
        with patch.object(
            release.urllib.request,
            "urlopen",
            return_value=io.BytesIO(json.dumps(data).encode()),
        ) as request:
            release.test_client(version)
        request.assert_called_once_with(
            f"https://test.pypi.org/pypi/hugegraph-python/{version}/json", timeout=30
        )
        self.assertIn(
            f"client_requirement=hugegraph-python @ {url}#sha256=" + "b" * 64,
            (self.root / "output").read_text(),
        )
        for updates in (
            {"url": "http://test-files.pythonhosted.org/" + filename},
            {"url": "https://evil.example/" + filename},
            {"digests": {"sha256": ""}},
            {"yanked": True},
        ):
            bad = {**data, "urls": [{**wheel, **updates}]}
            with (
                self.subTest(updates=updates),
                patch.object(
                    release.urllib.request,
                    "urlopen",
                    return_value=io.BytesIO(json.dumps(bad).encode()),
                ),
            ):
                with self.assertRaises(ValueError):
                    release.test_client(version)
        for wheels in ([], [wheel, wheel]):
            bad = {**data, "urls": wheels}
            with patch.object(
                release.urllib.request,
                "urlopen",
                return_value=io.BytesIO(json.dumps(bad).encode()),
            ):
                with self.assertRaisesRegex(ValueError, "Expected one"):
                    release.test_client(version)

    def test_invalid_test_client_version_fails_before_network(self):
        for version in ("", "1.7.1", "../json", "1.7.1.01", "1.7.1rc1"):
            with (
                self.subTest(version=version),
                patch.object(release.urllib.request, "urlopen") as request,
            ):
                with self.assertRaisesRegex(ValueError, "Test client version"):
                    release.test_client(version)
            request.assert_not_called()

    def test_unknown_ref_fails_without_fallback(self):
        with (
            patch.object(
                release, "github", side_effect=subprocess.CalledProcessError(1, "gh")
            ),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            release.resolve("missing-branch")

    def test_invalid_resolved_sha_is_rejected(self):
        with (
            patch.object(release, "github", return_value={"sha": "main"}),
            self.assertRaisesRegex(ValueError, "Invalid source SHA"),
        ):
            release.resolve("main")

    def test_metadata_guards_before_build(self):
        module = self.root / release.COMPONENTS["client"][1]
        module.mkdir()
        project = module / "pyproject.toml"
        project.write_text(
            '[project]\nname="hugegraph-python-client"\nversion="1.7.0"\n'
        )
        with self.assertRaisesRegex(ValueError, "Distribution name"):
            release.metadata(self.root, "testpypi", "1.7.0.1")
        project.write_text('[project]\nname="hugegraph-python"\nversion="1.7.0"\n')
        original = project.read_bytes()
        with patch.object(release.subprocess, "run") as update:
            release.metadata(self.root, "pypi", "")
        update.assert_not_called()
        self.assertEqual(project.read_bytes(), original)
        self.assertIn("version=1.7.0", (self.root / "output").read_text())

    def test_explicit_version_rules(self):
        module = self.root / release.COMPONENTS["client"][1]
        module.mkdir()
        (module / "pyproject.toml").write_text(
            '[project]\nname="hugegraph-python"\nversion="1.7.0"\n'
        )
        invalid = [
            ("testpypi", ""),
            ("testpypi", "1.7.0"),
            ("testpypi", "1.7.0rc1"),
            ("testpypi", "1.7.0.dev1"),
            ("testpypi", "1.7.0.01"),
            ("testpypi", "1.7.0.1.2"),
            ("testpypi", "1.8.0.1"),
            ("pypi", "1.7.0.1"),
            ("pypi", "1.7.0"),
            ("pypi", "1.7.0rc1"),
            ("pypi", "1.8.0"),
        ]
        for target, version in invalid:
            with (
                self.subTest(target=target, version=version),
                patch.object(release.subprocess, "run") as update,
                self.assertRaises(ValueError),
            ):
                release.metadata(self.root, target, version)
            update.assert_not_called()
        with patch.object(release.subprocess, "run") as update:
            release.metadata(self.root, "testpypi", "1.7.0.12")
        update.assert_called_once_with(
            [
                "uv",
                "version",
                "--project",
                str(module),
                "--frozen",
                "1.7.0.12",
            ],
            check=True,
        )
        self.assertIn("version=1.7.0.12", (self.root / "output").read_text())

    def test_source_version_must_have_three_numeric_parts(self):
        module = self.root / release.COMPONENTS["client"][1]
        module.mkdir()
        for version in ("1.7", "1.7.0.1", "1.7.0rc1", "1.7.0.dev1", "01.7.0"):
            (module / "pyproject.toml").write_text(
                f'[project]\nname="hugegraph-python"\nversion="{version}"\n'
            )
            with (
                self.subTest(version=version),
                patch.object(release.subprocess, "run") as update,
                self.assertRaisesRegex(ValueError, "Source version must be"),
            ):
                release.metadata(self.root, "pypi", "")
            update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
