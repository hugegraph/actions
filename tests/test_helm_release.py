"""Exercise Helm publication failure boundaries without registries or credentials."""

import copy
import base64
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import yaml

SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/helm_release.py"
SPEC = importlib.util.spec_from_file_location("helm_release", SCRIPT)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64


def response(value, headers=None):
    result = io.BytesIO(json.dumps(value).encode())
    result.headers = headers or {}
    return result


def http_error(code, value, headers=None):
    return urllib.error.HTTPError("https://registry.example/v2/chart/manifests/1", code,
                                  "error", headers or {}, io.BytesIO(json.dumps(value).encode()))


def dockerhub_token(actions=("pull", "push"), repository="hugegraph/hugegraph-helm"):
    payload = json.dumps({"access": [{"type": "repository", "name": repository,
                                      "actions": list(actions)}]}).encode()
    return "header." + base64.urlsafe_b64encode(payload).decode().rstrip("=") + ".signature"


def archive(path, metadata):
    raw = yaml.safe_dump(metadata).encode()
    with tarfile.open(path, "w:gz") as tar:
        member = tarfile.TarInfo("hugegraph-helm/Chart.yaml")
        member.size = len(raw)
        tar.addfile(member, io.BytesIO(raw))


class ResolveTests(unittest.TestCase):
    def test_github_api_child_does_not_inherit_registry_credentials(self):
        credentials = {key: "secret" for key in release.REGISTRY_CREDENTIALS}
        with patch.dict(os.environ, dict(credentials, GH_TOKEN="api-token")), \
                patch.object(release.subprocess, "check_output", return_value=b'{"sha":"test"}') as command:
            self.assertEqual(release.github("commits/master"), {"sha": "test"})
        env = command.call_args.kwargs["env"]
        self.assertEqual(env["GH_TOKEN"], "api-token")
        self.assertFalse(set(credentials) & set(env))

    def test_ref_is_resolved_to_an_immutable_apache_commit(self):
        with patch.object(release, "github", return_value={"sha": SHA}) as github:
            self.assertEqual(release.resolve("refs/heads/master"), SHA)
        github.assert_called_once_with("commits/refs%2Fheads%2Fmaster")

    def test_invalid_ref_and_malformed_remote_sha_are_rejected(self):
        with patch.object(release, "github", return_value={"sha": "master"}) as github:
            with self.assertRaisesRegex(ValueError, "Invalid source ref"):
                release.resolve("master\nother")
            github.assert_not_called()
            with self.assertRaisesRegex(ValueError, "Invalid source SHA"):
                release.resolve("master")

    def test_release_rejects_unmerged_source_before_checkout_or_source_tests(self):
        with patch.object(release, "github", return_value={"sha": SHA}), \
                patch.object(release, "master_ancestor", side_effect=ValueError("unmerged")) as ancestry, \
                patch.object(release, "output") as output:
            with self.assertRaisesRegex(ValueError, "unmerged"):
                release.resolve("feature-branch", "release")
            ancestry.assert_called_once_with(SHA)
            output.assert_not_called()


class ChartChangesTests(unittest.TestCase):
    def test_first_run_and_changed_directory_need_publication(self):
        chart_sha = "c" * 40
        for last_hash in ("", "d" * 40):
            with self.subTest(last_hash=last_hash), \
                    patch.object(release, "github", return_value=[{"sha": chart_sha}]) as github, \
                    patch.object(release, "output") as output:
                self.assertEqual(release.changes(SHA, last_hash), chart_sha)
                github.assert_called_once_with(f"commits?sha={SHA}&path=helm%2Fhugegraph&per_page=1")
                output.assert_called_once_with(chart_sha=chart_sha, need_update="true")

    def test_unrelated_source_commits_do_not_republish_the_chart(self):
        chart_sha = "c" * 40
        for source_sha in (SHA, "d" * 40):
            with self.subTest(source_sha=source_sha), \
                    patch.object(release, "github", return_value=[{"sha": chart_sha}]) as github, \
                    patch.object(release, "output") as output:
                release.changes(source_sha, chart_sha)
                github.assert_called_once_with(f"commits?sha={source_sha}&path=helm%2Fhugegraph&per_page=1")
                output.assert_called_once_with(chart_sha=chart_sha, need_update="false")

    def test_invalid_source_or_marker_is_rejected_before_api_access(self):
        for source_sha, last_hash in (("master", ""), (SHA, "not-a-sha")):
            with self.subTest(source_sha=source_sha, last_hash=last_hash), \
                    patch.object(release, "github") as github, \
                    patch.object(release, "output") as output:
                with self.assertRaises(ValueError):
                    release.changes(source_sha, last_hash)
                github.assert_not_called()
                output.assert_not_called()

    def test_missing_or_malformed_history_is_not_treated_as_unchanged(self):
        for history in ([], {}, [None], [{}], [{"sha": "master"}], [{"sha": None}]):
            with self.subTest(history=history), \
                    patch.object(release, "github", return_value=history), \
                    patch.object(release, "output") as output:
                with self.assertRaises(ValueError):
                    release.changes(SHA)
                output.assert_not_called()

    def test_api_failure_does_not_emit_a_skip_or_success_marker(self):
        with patch.object(release, "github", side_effect=subprocess.CalledProcessError(1, "gh")), \
                patch.object(release, "output") as output:
            with self.assertRaises(subprocess.CalledProcessError):
                release.changes(SHA)
            output.assert_not_called()


class PublicationGateTests(unittest.TestCase):
    def setUp(self):
        self.data = {"source_sha": SHA, "channel": "dev", "version": "0.2.0-dev.123"}

    def test_source_channel_and_dev_run_are_bound_to_trusted_inputs(self):
        release.publication_gate(self.data, SHA, "dev", "123", "0.2.0-dev.123")
        for overrides in ({"source_sha": "c" * 40}, {"channel": "release"}, {"version": "0.2.0"},
                          {"version": "0.2.0-dev.124"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                release.publication_gate(dict(self.data, **overrides), SHA, "dev", "123", "0.2.0-dev.123")

    def test_release_rechecks_ancestry_and_published_service_in_clean_publisher(self):
        self.data.update(channel="release", version="0.2.0", app_version="1.8.0")
        with patch.object(release, "release_gate") as gate:
            release.publication_gate(self.data, SHA, "release", "123", "0.2.0")
            gate.assert_called_once_with({"appVersion": "1.8.0"}, SHA)


class AncestryTests(unittest.TestCase):
    def test_identical_and_ancestor_commits_are_accepted(self):
        for status in ("identical", "ahead"):
            comparison = {"status": status, "base_commit": {"sha": SHA},
                          "merge_base_commit": {"sha": SHA}}
            with self.subTest(status=status), patch.object(release, "github", return_value=comparison) as github:
                release.master_ancestor(SHA)
                github.assert_called_once_with(f"compare/{SHA}...master")

    def test_unmerged_branch_and_forked_commits_are_rejected(self):
        for status in ("behind", "diverged"):
            comparison = {"status": status, "base_commit": {"sha": SHA},
                          "merge_base_commit": {"sha": "c" * 40}}
            with self.subTest(status=status), patch.object(release, "github", return_value=comparison):
                with self.assertRaisesRegex(ValueError, "ancestor of apache/hugegraph master"):
                    release.master_ancestor(SHA)

    def test_direction_and_merge_base_must_agree_with_resolved_sha(self):
        for comparison in (
                {"status": "ahead", "base_commit": {"sha": SHA}, "merge_base_commit": {"sha": "c" * 40}},
                {"status": "ahead", "base_commit": {"sha": "c" * 40}, "merge_base_commit": {"sha": SHA}},
                {"status": "behind", "base_commit": {"sha": SHA}, "merge_base_commit": {"sha": SHA}},
                {"status": "ahead"}):
            with self.subTest(comparison=comparison), patch.object(release, "github", return_value=comparison):
                with self.assertRaisesRegex(ValueError, "ancestor of apache/hugegraph master"):
                    release.master_ancestor(SHA)


class RegistryTests(unittest.TestCase):
    def lookup(self, credentials=None):
        return release.registry_manifest("registry-1.docker.io", "hugegraph/hugegraph-helm", "0.2.0", credentials)

    def test_existing_manifest_returns_digest(self):
        with patch.object(release, "request", return_value=response(
                {"schemaVersion": 2}, {"Docker-Content-Digest": DIGEST})):
            self.assertEqual(self.lookup(), DIGEST)

    def test_signing_subject_must_bind_manifest_bytes_to_tested_chart(self):
        manifest = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "config": {"mediaType": "application/vnd.cncf.helm.config.v1+json"},
                    "layers": [{"mediaType": "application/vnd.cncf.helm.chart.content.v1.tar+gzip",
                                "digest": DIGEST}]}
        raw = json.dumps(manifest).encode()
        expected = "sha256:" + release.hashlib.sha256(raw).hexdigest()
        with patch.object(release, "request", return_value=response(manifest)):
            self.assertEqual(release.registry_manifest("ghcr.io", "hugegraph/hugegraph-helm", "0.2.0",
                                                      package_sha256="b" * 64), expected)
        helm_manifest = copy.deepcopy(manifest)
        del helm_manifest["mediaType"]
        helm_digest = "sha256:" + release.hashlib.sha256(json.dumps(helm_manifest).encode()).hexdigest()
        with patch.object(release, "request", return_value=response(helm_manifest,
                {"Content-Type": "application/vnd.oci.image.manifest.v1+json"})):
            self.assertEqual(release.registry_manifest("ghcr.io", "hugegraph/hugegraph-helm", "0.2.0",
                                                      package_sha256="b" * 64), helm_digest)
        for mutate in (lambda m: m.update(mediaType="application/vnd.oci.image.index.v1+json"),
                       lambda m: m["config"].update(mediaType="application/vnd.oci.image.config.v1+json"),
                       lambda m: m["layers"][0].update(digest="sha256:" + "c" * 64),
                       lambda m: m["layers"].append(copy.deepcopy(m["layers"][0]))):
            modified = copy.deepcopy(manifest)
            mutate(modified)
            with patch.object(release, "request", return_value=response(modified)), self.assertRaises(ValueError):
                release.registry_manifest("ghcr.io", "hugegraph/hugegraph-helm", "0.2.0",
                                          package_sha256="b" * 64)
        with patch.object(release, "request", return_value=response(manifest, {"Docker-Content-Digest": DIGEST})), \
                self.assertRaisesRegex(ValueError, "digest does not match"):
            release.registry_manifest("ghcr.io", "hugegraph/hugegraph-helm", "0.2.0", package_sha256="b" * 64)

    def test_only_explicit_oci_missing_allows_publication(self):
        for code in ("MANIFEST_UNKNOWN", "NAME_UNKNOWN"):
            with self.subTest(code=code), patch.object(release, "request", side_effect=http_error(
                    404, {"errors": [{"code": code}]})):
                self.assertIsNone(self.lookup())

    def test_generic_404_auth_rate_limit_and_server_errors_fail_closed(self):
        for status, body in ((404, {}), (404, {"errors": [{"code": "DENIED"}]}),
                             (403, {}), (429, {}), (500, {})):
            with self.subTest(status=status, body=body), patch.object(release, "request", side_effect=http_error(status, body)):
                with self.assertRaisesRegex(ValueError, "refusing to treat it as absent"):
                    self.lookup()

    def test_network_failure_is_not_absence(self):
        with patch.object(release, "request", side_effect=urllib.error.URLError("offline")):
            with self.assertRaises(urllib.error.URLError):
                self.lookup()

    def test_bearer_challenge_uses_scoped_token_and_credentials_only_at_token_endpoint(self):
        unauthorized = http_error(401, {}, {"WWW-Authenticate":
            'Bearer realm="https://auth.docker.io/token",service="registry.docker.io",scope="ignore"'})
        replies = [unauthorized, response({"token": "test-token"}),
                   response({"schemaVersion": 2}, {"Docker-Content-Digest": DIGEST})]
        calls = []
        def perform(url, headers):
            calls.append((url, headers.copy()))
            result = replies.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        with patch.object(release, "request", side_effect=perform):
            self.assertEqual(self.lookup(("user", "password")), DIGEST)
        self.assertNotIn("Authorization", calls[0][1])
        self.assertTrue(calls[1][1]["Authorization"].startswith("Basic "))
        self.assertIn("scope=repository%3Ahugegraph%2Fhugegraph-helm%3Apull", calls[1][0])
        self.assertEqual(calls[2][1]["Authorization"], "Bearer test-token")

    def test_publish_preflight_requests_pull_push_scope_on_both_registries(self):
        for host, realm in (("ghcr.io", "https://ghcr.io/token"),
                            ("registry-1.docker.io", "https://auth.docker.io/token")):
            unauthorized = http_error(401, {}, {"WWW-Authenticate": f'Bearer realm="{realm}"'})
            replies = [unauthorized, response({"token": dockerhub_token() if host == "registry-1.docker.io" else "token"}),
                       http_error(404, {"errors": [{"code": "MANIFEST_UNKNOWN"}]})]
            calls = []
            def perform(url, headers):
                calls.append((url, headers.copy()))
                result = replies.pop(0)
                if isinstance(result, Exception):
                    raise result
                return result
            with self.subTest(host=host), patch.object(release, "request", side_effect=perform):
                self.assertIsNone(release.registry_manifest(host, "hugegraph/hugegraph-helm",
                    "0.2.0", ("user", "password"), actions="pull,push"))
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(calls[1][0]).query)
            self.assertEqual(query["scope"], ["repository:hugegraph/hugegraph-helm:pull,push"])
            self.assertTrue(calls[1][1]["Authorization"].startswith("Basic "))

    def test_dockerhub_read_only_wrong_repository_and_malformed_tokens_fail_preflight(self):
        for token in (dockerhub_token(("pull",)), dockerhub_token(repository="hugegraph/another-chart"),
                      "opaque", "header.invalid!.signature", "header.bnVsbA.signature"):
            unauthorized = http_error(401, {}, {"WWW-Authenticate":
                'Bearer realm="https://auth.docker.io/token",service="registry.docker.io"'})
            with self.subTest(token=token), patch.object(release, "request", side_effect=[
                    unauthorized, response({"token": token})]) as request:
                with self.assertRaisesRegex(ValueError, "Docker Hub"):
                    release.registry_manifest("registry-1.docker.io", "hugegraph/hugegraph-helm",
                                              "0.2.0", ("user", "password"), actions="pull,push")
                self.assertEqual(request.call_count, 2)

    def test_dockerhub_write_preflight_cannot_skip_the_token_scope_check(self):
        for result in (response({"schemaVersion": 2}),
                       http_error(404, {"errors": [{"code": "MANIFEST_UNKNOWN"}]})):
            with self.subTest(result=result), patch.object(release, "request", side_effect=[result]):
                with self.assertRaisesRegex(ValueError, "Docker Hub"):
                    release.registry_manifest("registry-1.docker.io", "hugegraph/hugegraph-helm",
                                              "0.2.0", ("user", "password"), actions="pull,push")

    def test_authenticated_404_is_explicitly_required(self):
        unauthorized = http_error(401, {}, {"WWW-Authenticate": 'Bearer realm="https://auth.docker.io/token"'})
        with patch.object(release, "request", side_effect=[unauthorized, response({"token": "token"}),
                http_error(404, {"errors": [{"code": "MANIFEST_UNKNOWN"}]})]):
            self.assertIsNone(self.lookup(("user", "password")))

    def test_token_endpoint_cannot_steal_credentials(self):
        for realm in ("https://evil.example/token", "http://auth.docker.io/token", "https://user@auth.docker.io/token"):
            unauthorized = http_error(401, {}, {"WWW-Authenticate": f'Bearer realm="{realm}"'})
            with self.subTest(realm=realm), patch.object(release, "request", side_effect=unauthorized) as request:
                with self.assertRaisesRegex(ValueError, "Untrusted"):
                    self.lookup(("user", "secret"))
                self.assertEqual(request.call_count, 1)

    def test_redirects_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "redirect"):
            release.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://evil.example")

    def test_registry_image_reference_parsing(self):
        self.assertEqual(release.split_image("hugegraph/pd:1.8.0"),
                         ("registry-1.docker.io", "hugegraph/pd", "1.8.0"))
        self.assertEqual(release.split_image("ghcr.io/team/pd@" + DIGEST),
                         ("ghcr.io", "team/pd", DIGEST))
        for image in ("hugegraph/pd", "hugegraph/pd:latest", "hugegraph/pd:1.8.0-rc1", "hugegraph/pd@sha256:bad"):
            with self.subTest(image=image), self.assertRaises(ValueError):
                release.fixed_image(image)


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.dist = self.root / "dist"
        self.source = self.root / "source"
        self.chart_path = self.source / "helm/hugegraph"
        self.chart_path.mkdir(parents=True)
        self.metadata = {"apiVersion": "v2", "name": "hugegraph-helm", "version": "0.2.0", "appVersion": "1.8.0"}
        self.values = {component: {"image": {"repository": "hugegraph/" + component, "tag": "1.8.0"}}
                       for component in ("pd", "store", "server", "hubble")}
        self.write_source()
        self.env = patch.dict(os.environ, {"GITHUB_OUTPUT": str(self.root / "output"),
                                          "GHCR_USERNAME": "user", "GHCR_PASSWORD": "secret",
                                          "DOCKERHUB_USERNAME": "user", "DOCKERHUB_PASSWORD": "secret",
                                          "GITHUB_WORKFLOW_REF": release.SIGNING_WORKFLOW + "@refs/heads/master",
                                          "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.example/token",
                                          "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "oidc-request-token"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.commands = []
        self.registry_lookup = release.registry_manifest
        self.mocks = [patch.object(release.subprocess, "check_output", side_effect=self.check_output),
                      patch.object(release.subprocess, "run", side_effect=self.helm_run),
                      patch.object(release, "registry_manifest", return_value=DIGEST),
                      patch.object(release, "github", side_effect=self.github)]
        for mock in self.mocks:
            mock.start()
            self.addCleanup(mock.stop)

    def github(self, endpoint):
        if endpoint.startswith("compare/"):
            return {"status": "identical", "base_commit": {"sha": SHA},
                    "merge_base_commit": {"sha": SHA}}
        return {"tag_name": "1.8.0", "draft": False, "prerelease": False,
                "published_at": "2027-01-01T00:00:00Z"}

    def write_source(self, revision="1.8.0"):
        (self.source / "pom.xml").write_text('<project xmlns="http://maven.apache.org/POM/4.0.0"><properties>'
                                            f'<revision>{revision}</revision></properties></project>')
        (self.chart_path / "Chart.yaml").write_text(yaml.safe_dump(self.metadata))
        (self.chart_path / "values.yaml").write_text(yaml.safe_dump(self.values))

    def check_output(self, args, **kwargs):
        if args[0] == "git":
            return "" if "ls-files" in args else SHA + "\n"
        if args[:2] == ["helm", "template"]:
            values = copy.deepcopy(self.values)
            if "-f" in args:
                overrides = yaml.safe_load(Path(args[-1]).read_text())
                for component, override in overrides.items():
                    values[component]["image"].update(override["image"])
                    if "waitImage" in override:
                        values[component]["waitImage"] = override["waitImage"]
            images = list(release.component_images(values, self.metadata["appVersion"]).values())
            images += list(release.helper_images(values).values())
            images += getattr(self, "extra_images", [])
            return yaml.safe_dump({"kind": "Pod", "spec": {"containers": [{"image": image} for image in images]}})
        raise AssertionError(args)

    def helm_run(self, args, **kwargs):
        self.commands.append((args, kwargs))
        if args[:3] == ["helm", "registry", "login"]:
            Path(kwargs["env"]["HELM_REGISTRY_CONFIG"]).write_text(json.dumps({"auths": {args[3]: {}}}))
        elif args[:2] == ["cosign", "login"]:
            (Path(kwargs["env"]["DOCKER_CONFIG"]) / "config.json").write_text(
                json.dumps({"auths": {args[2]: {}}}))
        elif args[:2] == ["helm", "package"]:
            metadata = yaml.safe_load((Path(args[2]) / "Chart.yaml").read_text())
            archive(Path(args[-1]) / f"hugegraph-helm-{metadata['version']}.tgz", metadata)
        elif args[:2] == ["helm", "pull"]:
            source = next(self.dist.glob("*.tgz"))
            shutil.copyfile(source, Path(args[-1]) / source.name)
        return subprocess.CompletedProcess(args, 0)

    def build(self, channel="dev"):
        release.package(self.source, SHA, "master", channel, "123", self.dist)
        return release.digest(self.dist / "manifest.json")

    def publish(self, manifest_sha):
        release.publish(self.dist, manifest_sha, SHA, "dev", "123", "0.2.0-dev.123")

    def seal(self):
        manifest_sha = self.build()
        data = json.loads((self.dist / "manifest.json").read_text())
        validation = self.root / "validation.json"
        validation.write_text(json.dumps({"status": "passed", "source_sha": SHA,
                                         "package_sha256": data["package_sha256"]}))
        release.seal(self.dist, manifest_sha, validation)
        return release.digest(self.dist / "manifest.json")

    def test_development_version_only_changes_temporary_copy(self):
        original = (self.chart_path / "Chart.yaml").read_bytes()
        manifest_sha = self.build()
        self.assertEqual((self.chart_path / "Chart.yaml").read_bytes(), original)
        data, artifact = release.verify(self.dist, manifest_sha, sealed=False)
        self.assertEqual(data["version"], "0.2.0-dev.123")
        self.assertEqual(release.chart_metadata(artifact)["version"], "0.2.0-dev.123")
        pins = yaml.safe_load((self.dist / "image-values.yaml").read_text())
        self.assertEqual(pins["pd"]["image"]["digest"], DIGEST)
        self.assertEqual(data["image_digests"]["hugegraph/pd:1.8.0"], DIGEST)

    def test_latest_app_and_component_images_are_allowed_in_dev(self):
        self.metadata["appVersion"] = "latest"
        for component in self.values.values():
            component["image"]["tag"] = "latest"
        self.write_source()
        self.build()

    def test_independent_init_and_hook_images_are_pinned(self):
        self.values["store"]["waitImage"] = "busybox:1.36.1"
        self.values["server"]["waitImage"] = "docker.io/library/alpine:3.20.0"
        self.write_source()
        self.build()
        overrides = yaml.safe_load((self.dist / "image-values.yaml").read_text())
        self.assertEqual(overrides["store"]["waitImage"], "registry-1.docker.io/library/busybox@" + DIGEST)
        self.assertEqual(overrides["server"]["waitImage"], "registry-1.docker.io/library/alpine@" + DIGEST)

    def test_unknown_unpinnable_rendered_image_is_rejected(self):
        self.extra_images = ["busybox:1.36.1"]
        with self.assertRaisesRegex(ValueError, "cannot be pinned"):
            self.build()
        self.assertFalse((self.dist / "manifest.json").exists())

    def test_extra_image_already_using_a_digest_is_accepted(self):
        self.extra_images = ["busybox@" + DIGEST]
        self.build()

    def test_release_preserves_source_and_requires_published_service(self):
        original = (self.chart_path / "Chart.yaml").read_bytes()
        manifest_sha = self.build("release")
        data, _ = release.verify(self.dist, manifest_sha, sealed=False)
        self.assertEqual(data["version"], "0.2.0")
        self.assertEqual((self.chart_path / "Chart.yaml").read_bytes(), original)
        self.assertEqual([call.args[0] for call in release.github.call_args_list],
                         [f"compare/{SHA}...master", "releases/tags/1.8.0"])

    def test_unmerged_source_cannot_publish_even_with_master_ref_label(self):
        comparison = {"status": "behind", "base_commit": {"sha": SHA},
                      "merge_base_commit": {"sha": "c" * 40}}
        with patch.object(release, "github", return_value=comparison) as github:
            with self.assertRaisesRegex(ValueError, "ancestor of apache/hugegraph master"):
                self.build("release")
        github.assert_called_once_with(f"compare/{SHA}...master")
        self.assertFalse(self.dist.exists())

    def test_audited_master_can_be_ahead_of_service_release(self):
        self.write_source("1.9.0-SNAPSHOT")
        self.build("release")

    def test_old_source_is_rejected_including_dev(self):
        self.write_source("1.7.0")
        with self.assertRaisesRegex(ValueError, "1.8.0 and later"):
            self.build()

    def test_unresolved_checkout_is_rejected(self):
        with patch.object(release.subprocess, "check_output", return_value="c" * 40):
            with self.assertRaisesRegex(ValueError, "Checkout does not match"):
                self.build()

    def test_modified_tracked_source_is_rejected(self):
        with patch.object(release.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaisesRegex(ValueError, "Tracked source changed"):
                self.build()

    def test_untracked_chart_files_are_rejected(self):
        def git_output(args, **kwargs):
            return "helm/hugegraph/untracked.yaml" if "ls-files" in args else SHA
        with patch.object(release.subprocess, "check_output", side_effect=git_output):
            with self.assertRaisesRegex(ValueError, "Untracked chart files"):
                self.build()

    def test_release_never_allows_latest_app_version(self):
        self.metadata["appVersion"] = "latest"
        self.write_source()
        with self.assertRaisesRegex(ValueError, "appVersion must be x.y.z"):
            self.build("release")

    def test_draft_prerelease_and_missing_published_at_rejected(self):
        for value in ({"draft": True}, {"prerelease": True}, {"published_at": None}, {"tag_name": "1.7.0"}):
            data = {"tag_name": "1.8.0", "draft": False, "prerelease": False, "published_at": "today"}
            data.update(value)
            def github(endpoint):
                return self.github(endpoint) if endpoint.startswith("compare/") else data
            with self.subTest(value=value), patch.object(release, "github", side_effect=github):
                with self.assertRaisesRegex(ValueError, "published stable"):
                    self.build("release")

    def test_release_rejects_latest_images_and_wrong_service_version(self):
        for component, tag in (("pd", "latest"), ("server", "1.9.0"), ("hubble", "latest")):
            original = self.values[component]["image"]["tag"]
            self.values[component]["image"]["tag"] = tag
            self.write_source()
            with self.subTest(component=component, tag=tag), self.assertRaises(ValueError):
                self.build("release")
            shutil.rmtree(self.dist)
            self.values[component]["image"]["tag"] = original

    def test_global_registry_does_not_rewrite_already_qualified_images(self):
        self.values["global"] = {"imageRegistry": "mirror.example/cache"}
        self.values["server"]["image"]["repository"] = "ghcr.io/hugegraph/server"
        images = release.component_images(self.values, "1.8.0")
        self.assertEqual(images["server"], "ghcr.io/hugegraph/server:1.8.0")
        self.assertEqual(images["pd"], "mirror.example/cache/hugegraph/pd:1.8.0")

    def test_hubble_can_have_independent_release_version(self):
        self.values["hubble"]["image"]["tag"] = "1.6.0"
        self.write_source()
        self.build("release")

    def test_unavailable_image_fails_before_bundle_can_be_sealed(self):
        with patch.object(release, "registry_manifest", return_value=None):
            with self.assertRaisesRegex(ValueError, "Image is not published"):
                self.build()
        self.assertFalse((self.dist / "manifest.json").exists())

    def test_all_rendered_container_kinds_including_hooks_are_examined(self):
        documents = [{"spec": {"initContainers": [{"image": "init:1.0.0"}],
                              "containers": [{"image": "main:1.0.0"}]}},
                     {"metadata": {"annotations": {"helm.sh/hook": "test"}},
                      "spec": {"containers": [{"image": "hook:1.0.0"}]}}]
        self.assertEqual(release.rendered_images(documents), ["hook:1.0.0", "init:1.0.0", "main:1.0.0"])

    def test_package_name_not_application_name_is_required(self):
        self.metadata["name"] = "hugegraph"
        self.write_source()
        with self.assertRaisesRegex(ValueError, "Chart.name"):
            self.build()

    def test_seal_binds_success_to_original_tested_bytes(self):
        manifest_sha = self.seal()
        data, _ = release.verify(self.dist, manifest_sha)
        self.assertIn("validation_sha256", data)
        (self.dist / "validation.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "validation result"):
            release.verify(self.dist, manifest_sha)

    def test_failed_or_different_source_and_package_cannot_seal(self):
        manifest_sha = self.build()
        data = json.loads((self.dist / "manifest.json").read_text())
        for overrides in ({"status": "failed"}, {"source_sha": "c" * 40}, {"package_sha256": "d" * 64}):
            result = {"status": "passed", "source_sha": SHA, "package_sha256": data["package_sha256"]}
            result.update(overrides)
            marker = self.root / "failed.json"
            marker.write_text(json.dumps(result))
            with self.subTest(overrides=overrides), self.assertRaisesRegex(ValueError, "exact source and package"):
                release.seal(self.dist, manifest_sha, marker)

    def test_missing_original_package_never_rebuilds(self):
        manifest_sha = self.seal()
        next(self.dist.glob("*.tgz")).unlink()
        with self.assertRaisesRegex(ValueError, "Package hash mismatch"):
            self.publish(manifest_sha)
        self.assertFalse(any(args[:3] == ["helm", "registry", "login"] for args, _ in self.commands))

    def test_modified_or_extra_bundle_files_fail_before_login(self):
        manifest_sha = self.seal()
        for name in ("manifest.json", "image-values.yaml", "hugegraph-helm-0.2.0-dev.123.tgz", "extra.txt"):
            path = self.dist / name
            original = path.read_bytes() if path.exists() else None
            path.write_text("changed")
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.publish(manifest_sha)
            if original is None:
                path.unlink()
            else:
                path.write_bytes(original)
        self.assertFalse(any(args[:3] == ["helm", "registry", "login"] for args, _ in self.commands))

    def test_first_publish_verifies_authenticated_and_anonymous_downloads(self):
        manifest_sha = self.seal()
        def probe_result(*args, **kwargs):
            return DIGEST if "package_sha256" in kwargs else None
        with patch.object(release, "registry_manifest", side_effect=probe_result) as probe:
            self.publish(manifest_sha)
            self.assertEqual(probe.call_count, 6)
            for host in ("ghcr.io", "registry-1.docker.io"):
                probe.assert_any_call(host, "hugegraph/hugegraph-helm",
                    "0.2.0-dev.123", ("user", "secret"), actions="pull,push")
        pushes = [args for args, _ in self.commands if args[:2] == ["helm", "push"]]
        self.assertEqual(len(pushes), 2)
        self.assertEqual({p[-1] for p in pushes}, {"oci://ghcr.io/hugegraph", "oci://registry-1.docker.io/hugegraph"})
        pulls = [kwargs for args, kwargs in self.commands if args[:2] == ["helm", "pull"]]
        self.assertEqual(len(pulls), 4)
        for pull in pulls:
            self.assertNotIn("GHCR_PASSWORD", pull["env"])
            self.assertNotIn("DOCKERHUB_PASSWORD", pull["env"])
            self.assertFalse(set(release.OIDC_ENVIRONMENT) & set(pull["env"]))
        self.assertNotEqual(pulls[0]["env"]["HELM_REGISTRY_CONFIG"], pulls[1]["env"]["HELM_REGISTRY_CONFIG"])
        login = next(kwargs for args, kwargs in self.commands if args[:3] == ["helm", "registry", "login"])
        self.assertEqual(login["input"], "secret\n")
        self.assertFalse(any("secret" in str(args) for args, _ in self.commands))
        signatures = [(args, kwargs) for args, kwargs in self.commands if args[:2] == ["cosign", "sign"]]
        self.assertEqual({args[-1] for args, _ in signatures}, {
            "ghcr.io/hugegraph/hugegraph-helm@" + DIGEST,
            "registry-1.docker.io/hugegraph/hugegraph-helm@" + DIGEST})
        for _, kwargs in signatures:
            self.assertEqual(kwargs["env"]["ACTIONS_ID_TOKEN_REQUEST_TOKEN"], "oidc-request-token")
            self.assertFalse(set(release.REGISTRY_CREDENTIALS) & set(kwargs["env"]))
        verifications = [(args, kwargs) for args, kwargs in self.commands if args[:2] == ["cosign", "verify"]]
        self.assertEqual(len(verifications), 2)
        for args, kwargs in verifications:
            self.assertEqual(args[args.index("--certificate-identity") + 1],
                             "https://github.com/" + release.SIGNING_WORKFLOW + "@refs/heads/master")
            self.assertEqual(args[args.index("--certificate-oidc-issuer") + 1], release.OIDC_ISSUER)
            self.assertFalse(set(release.OIDC_ENVIRONMENT) & set(kwargs["env"]))
            config = Path(kwargs["env"]["DOCKER_CONFIG"]) / "config.json"
            # Temporary registry configurations are cleaned when publication finishes.
            self.assertFalse(config.exists())

    def test_existing_identical_version_skips_push_and_still_checks_anonymous(self):
        manifest_sha = self.seal()
        self.publish(manifest_sha)
        self.assertFalse(any(args[:2] == ["helm", "push"] for args, _ in self.commands))
        self.assertEqual(sum(args[:2] == ["helm", "pull"] for args, _ in self.commands), 6)
        self.assertEqual(sum(args[:2] == ["cosign", "sign"] for args, _ in self.commands), 2)

    def test_existing_different_package_fails_without_overwrite(self):
        manifest_sha = self.seal()
        def replace_pull(args, **kwargs):
            if args[:2] == ["helm", "pull"]:
                (Path(args[-1]) / next(self.dist.glob("*.tgz")).name).write_text("different bytes")
            else:
                self.helm_run(args, **kwargs)
        with patch.object(release.subprocess, "run", side_effect=replace_pull):
            with self.assertRaisesRegex(ValueError, "refusing overwrite"):
                self.publish(manifest_sha)
        self.assertFalse(any(args[:2] == ["helm", "push"] for args, _ in self.commands))

    def test_second_registry_conflict_blocks_first_registry_write(self):
        manifest_sha = self.seal()
        def conflict(args, **kwargs):
            if args[:2] == ["helm", "pull"]:
                (Path(args[-1]) / next(self.dist.glob("*.tgz")).name).write_text("another run's bytes")
            else:
                return self.helm_run(args, **kwargs)
        for states in ((None, DIGEST), (DIGEST, None)):
            self.commands.clear()
            with self.subTest(states=states), patch.object(release, "registry_manifest", side_effect=states), \
                    patch.object(release.subprocess, "run", side_effect=conflict):
                with self.assertRaisesRegex(ValueError, "refusing overwrite"):
                    self.publish(manifest_sha)
            self.assertFalse(any(args[:2] == ["helm", "push"] for args, _ in self.commands))

    def test_network_failure_in_second_preflight_blocks_all_writes(self):
        manifest_sha = self.seal()
        with patch.object(release, "registry_manifest", side_effect=[None, urllib.error.URLError("offline")]):
            with self.assertRaises(urllib.error.URLError):
                self.publish(manifest_sha)
        self.assertFalse(any(args[:2] == ["helm", "push"] for args, _ in self.commands))

    def test_dockerhub_pull_only_token_blocks_uploads_to_both_registries(self):
        manifest_sha = self.seal()
        unauthorized = http_error(401, {}, {"WWW-Authenticate":
            'Bearer realm="https://auth.docker.io/token",service="registry.docker.io"'})
        replies = [http_error(404, {"errors": [{"code": "MANIFEST_UNKNOWN"}]}),
                   unauthorized, response({"token": dockerhub_token(("pull",))})]
        with patch.object(release, "registry_manifest", side_effect=self.registry_lookup), \
                patch.object(release, "request", side_effect=replies):
            with self.assertRaisesRegex(ValueError, "lacks pull/push access"):
                self.publish(manifest_sha)
        self.assertFalse(any(args[:2] == ["helm", "push"] for args, _ in self.commands))

    def test_partial_publication_recovers_only_missing_registry_with_original_bytes(self):
        manifest_sha = self.seal()
        def probe(host, *args, **kwargs):
            return DIGEST if host == "ghcr.io" or "package_sha256" in kwargs else None
        with patch.object(release, "registry_manifest", side_effect=probe):
            self.publish(manifest_sha)
        pushes = [args for args, _ in self.commands if args[:2] == ["helm", "push"]]
        self.assertEqual([p[-1] for p in pushes], ["oci://registry-1.docker.io/hugegraph"])

    def test_both_registry_preflights_finish_before_first_push(self):
        manifest_sha = self.seal()
        probes = []
        def probe(host, *args, **kwargs):
            if "package_sha256" in kwargs:
                return DIGEST
            probes.append(host)
            return None
        def run(args, **kwargs):
            if args[:2] == ["helm", "push"]:
                self.assertEqual(probes, ["ghcr.io", "registry-1.docker.io"])
            return self.helm_run(args, **kwargs)
        with patch.object(release, "registry_manifest", side_effect=probe), \
                patch.object(release.subprocess, "run", side_effect=run):
            self.publish(manifest_sha)

    def test_failed_second_push_then_retry_keeps_first_registry_original_package(self):
        manifest_sha = self.seal()
        original_sha = release.digest(next(self.dist.glob("*.tgz")))
        published = set()
        fail_dockerhub = True
        def probe(host, *args, **kwargs):
            return DIGEST if host in published else None
        def run(args, **kwargs):
            if args[:2] == ["helm", "push"]:
                host = args[-1].removeprefix("oci://").split("/")[0]
                if host == "registry-1.docker.io" and fail_dockerhub:
                    raise subprocess.CalledProcessError(1, args)
                published.add(host)
            return self.helm_run(args, **kwargs)
        with patch.object(release, "registry_manifest", side_effect=probe), \
                patch.object(release.subprocess, "run", side_effect=run):
            with self.assertRaisesRegex(ValueError, "Publication incomplete"):
                self.publish(manifest_sha)
            self.assertEqual(published, {"ghcr.io"})
            fail_dockerhub = False
            self.commands.clear()
            self.publish(manifest_sha)
        self.assertEqual(published, {"ghcr.io", "registry-1.docker.io"})
        pushes = [args for args, _ in self.commands if args[:2] == ["helm", "push"]]
        self.assertEqual([p[-1] for p in pushes], ["oci://registry-1.docker.io/hugegraph"])
        self.assertEqual(release.digest(next(self.dist.glob("*.tgz"))), original_sha)

    def test_private_package_fails_anonymous_verification(self):
        manifest_sha = self.seal()
        def private_pull(args, **kwargs):
            if args[:2] == ["helm", "pull"] and not json.loads(
                    Path(kwargs["env"]["HELM_REGISTRY_CONFIG"]).read_text())["auths"]:
                raise subprocess.CalledProcessError(1, args)
            return self.helm_run(args, **kwargs)
        with patch.object(release.subprocess, "run", side_effect=private_pull):
            with self.assertRaisesRegex(ValueError, "Publication incomplete"):
                self.publish(manifest_sha)

    def test_missing_oidc_or_unexpected_workflow_blocks_all_publication(self):
        manifest_sha = self.seal()
        for overrides in ({"ACTIONS_ID_TOKEN_REQUEST_TOKEN": ""},
                          {"ACTIONS_ID_TOKEN_REQUEST_URL": ""},
                          {"GITHUB_WORKFLOW_REF": "fork/actions/.github/workflows/publish_helm.yml@refs/heads/master"},
                          {"GITHUB_WORKFLOW_REF": release.SIGNING_WORKFLOW + "@refs/pull/12/merge"}):
            self.commands.clear()
            with self.subTest(overrides=overrides), patch.dict(os.environ, overrides), self.assertRaises(ValueError):
                self.publish(manifest_sha)
            self.assertFalse(self.commands)

    def test_signature_failure_can_recover_without_rebuilding_or_reuploading_chart(self):
        manifest_sha = self.seal()
        original = next(self.dist.glob("*.tgz")).read_bytes()
        def fail_verification(args, **kwargs):
            if args[:2] == ["cosign", "verify"] and args[2].startswith("ghcr.io/"):
                self.assertEqual(json.loads((Path(kwargs["env"]["DOCKER_CONFIG"]) / "config.json").read_text()),
                                 {"auths": {}})
                raise subprocess.CalledProcessError(1, args)
            return self.helm_run(args, **kwargs)
        with patch.object(release.subprocess, "run", side_effect=fail_verification):
            with self.assertRaisesRegex(ValueError, "Publication incomplete"):
                self.publish(manifest_sha)
        self.assertTrue(any(args[:2] == ["cosign", "sign"] and args[-1].startswith("registry-1.docker.io/")
                            for args, _ in self.commands))
        self.commands.clear()
        self.publish(manifest_sha)
        self.assertFalse(any(args[:2] in (["helm", "push"], ["helm", "package"]) for args, _ in self.commands))
        self.assertEqual(next(self.dist.glob("*.tgz")).read_bytes(), original)

    def test_changed_or_missing_manifest_cannot_be_signed(self):
        manifest_sha = self.seal()
        for result in (None, "sha256:" + "c" * 64):
            self.commands.clear()
            def probe(*args, **kwargs):
                return result if "package_sha256" in kwargs else DIGEST
            with self.subTest(result=result), patch.object(release, "registry_manifest", side_effect=probe):
                with self.assertRaisesRegex(ValueError, "Publication incomplete"):
                    self.publish(manifest_sha)
            self.assertFalse(any(args[:2] == ["cosign", "sign"] for args, _ in self.commands))

    def test_manifest_tag_change_during_signing_fails(self):
        manifest_sha = self.seal()
        post_probes = {}
        def probe(host, *args, **kwargs):
            if "package_sha256" in kwargs:
                post_probes[host] = post_probes.get(host, 0) + 1
                if post_probes[host] == 2:
                    return "sha256:" + "c" * 64
            return DIGEST
        with patch.object(release, "registry_manifest", side_effect=probe):
            with self.assertRaisesRegex(ValueError, "tag changed while signing"):
                self.publish(manifest_sha)

    def test_signature_discovery_can_lag_without_weakening_verification(self):
        manifest_sha = self.seal()
        attempts = {}
        def lagging_referrers(args, **kwargs):
            if args[:2] == ["cosign", "verify"]:
                attempts[args[2]] = attempts.get(args[2], 0) + 1
                if args[2].startswith("registry-1.docker.io/") and attempts[args[2]] < 3:
                    raise subprocess.CalledProcessError(10, args)
            return self.helm_run(args, **kwargs)
        with patch.object(release.subprocess, "run", side_effect=lagging_referrers), \
                patch.object(release.time, "sleep") as sleep:
            self.publish(manifest_sha)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 15])
        for args, _ in self.commands:
            if args[:2] == ["cosign", "verify"]:
                self.assertIn("--certificate-identity", args)
                self.assertIn("--certificate-oidc-issuer", args)

    def test_invalid_identity_fails_immediately_and_missing_signatures_have_a_bound(self):
        manifest_sha = self.seal()
        for code in (10, 12):
            attempts = []
            def verification_failure(args, **kwargs):
                if args[:2] == ["cosign", "verify"]:
                    attempts.append(args[2])
                    raise subprocess.CalledProcessError(code, args)
                return self.helm_run(args, **kwargs)
            with self.subTest(code=code), patch.object(release.subprocess, "run", side_effect=verification_failure), \
                    patch.object(release.time, "sleep") as sleep:
                with self.assertRaisesRegex(ValueError, "Publication incomplete"):
                    self.publish(manifest_sha)
                self.assertEqual(len(attempts), 10 if code == 10 else 2)
                self.assertEqual(sleep.call_count, 8 if code == 10 else 0)


if __name__ == "__main__":
    unittest.main()
