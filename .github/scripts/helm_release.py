"""Package and publish the exact tested HugeGraph Helm artifact (Python 3.11+)."""

import argparse
import base64
import binascii
from contextlib import ExitStack
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

SOURCE = "apache/hugegraph"
NAME = "hugegraph-helm"
TARGETS = {
    "ghcr": ("ghcr.io", "hugegraph"),
    "dockerhub": ("registry-1.docker.io", "hugegraph"),
}
REGISTRY_CREDENTIALS = tuple(target.upper() + suffix for target in TARGETS
                             for suffix in ("_USERNAME", "_PASSWORD"))
OIDC_ENVIRONMENT = ("ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN")
OIDC_ISSUER = "https://token.actions.githubusercontent.com"
SIGNING_WORKFLOW = "hugegraph/actions/.github/workflows/publish_helm.yml"
STABLE = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
SHA = r"[0-9a-f]{40}"
SHA256 = r"[0-9a-f]{64}"
ACCEPT = ", ".join((
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def output(**values):
    if "GITHUB_OUTPUT" not in os.environ:
        return
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        for key, value in values.items():
            value = str(value)
            require("\n" not in value and "\r" not in value, "Invalid output")
            stream.write(f"{key}={value}\n")


def github(endpoint):
    env = os.environ.copy()
    for key in REGISTRY_CREDENTIALS:
        env.pop(key, None)
    return json.loads(subprocess.check_output(["gh", "api", f"repos/{SOURCE}/{endpoint}"], env=env))


def resolve(ref, channel="dev"):
    require(bool(ref) and not any(c in ref for c in "\n\r"), "Invalid source ref")
    sha = github("commits/" + urllib.parse.quote(ref, safe=""))["sha"]
    require(isinstance(sha, str) and re.fullmatch(SHA, sha), "Invalid source SHA")
    # This job runs only trusted pipeline code, before any selected-source tests.
    if channel == "release":
        master_ancestor(sha)
    output(source_sha=sha)
    return sha


def stable(version, label):
    require(isinstance(version, str) and re.fullmatch(STABLE, version), f"{label} must be x.y.z")
    return tuple(map(int, version.split(".")))


def source_version(source):
    root = ET.parse(source / "pom.xml").getroot()
    ns = {"m": "http://maven.apache.org/POM/4.0.0"}
    revision = root.find("m:properties/m:revision", ns)
    require(revision is not None and revision.text, "Source pom.xml must declare revision")
    value = revision.text.removesuffix("-SNAPSHOT")
    require(stable(value, "Source revision") >= (1, 8, 0), "Helm supports HugeGraph 1.8.0 and later only")
    return value


def master_ancestor(sha):
    """Formal artifacts must come from the Apache master history, regardless of ref label."""
    require(isinstance(sha, str) and re.fullmatch(SHA, sha), "Invalid source SHA")
    comparison = github(f"compare/{sha}...master")
    # GitHub compares base...head: master being ahead means source is an ancestor.
    require(comparison.get("status") in ("ahead", "identical")
            and comparison.get("base_commit", {}).get("sha") == sha
            and comparison.get("merge_base_commit", {}).get("sha") == sha,
            "Formal chart source must be an ancestor of apache/hugegraph master")


def release_gate(chart, sha):
    version = chart.get("appVersion")
    require(stable(version, "Release appVersion") >= (1, 8, 0), "Release appVersion must be at least 1.8.0")
    master_ancestor(sha)
    data = github("releases/tags/" + urllib.parse.quote(version, safe=""))
    require(data.get("tag_name") == version and data.get("draft") is False
            and data.get("prerelease") is False and data.get("published_at"),
            "HugeGraph appVersion must already have a published stable GitHub Release")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Registry redirects are not accepted")


def request(url, headers=None):
    """Keep credentials on the explicitly validated registry/token endpoint."""
    return urllib.request.build_opener(NoRedirect).open(
        urllib.request.Request(url, headers=headers or {}), timeout=60
    )


def dockerhub_write_access(token, repository):
    """Check granted scope from Docker Hub's HTTPS issuer; the registry verifies the JWT."""
    try:
        parts = token.split(".")
        require(len(parts) == 3, "Invalid Docker Hub token format")
        claims = json.loads(base64.b64decode(parts[1] + "=" * (-len(parts[1]) % 4),
                                            altchars=b"-_", validate=True))
    except (ValueError, binascii.Error, UnicodeError):
        raise ValueError("Cannot verify Docker Hub token write access") from None
    access = claims.get("access") if isinstance(claims, dict) else None
    require(isinstance(access, list) and any(
        isinstance(grant, dict) and grant.get("type") == "repository"
        and grant.get("name") == repository and isinstance(grant.get("actions"), list)
        and "pull" in grant["actions"] and "push" in grant["actions"]
        for grant in access), f"Docker Hub token lacks pull/push access to {repository}")


def registry_manifest(host, repository, reference, credentials=None, actions="pull", package_sha256=None):
    """Return a digest, or None only for an explicit OCI not-found error."""
    require(actions in ("pull", "pull,push"), "Invalid registry token actions")
    require(re.fullmatch(r"[a-z0-9.-]+(?::[0-9]+)?", host), "Invalid registry host")
    require(re.fullmatch(r"[a-z0-9._/-]+", repository) and ".." not in repository,
            "Invalid image repository")
    url = f"https://{host}/v2/{repository}/manifests/{urllib.parse.quote(reference, safe=':')}"
    headers = {"Accept": ACCEPT, "User-Agent": "hugegraph-helm-release"}
    check_write = host == "registry-1.docker.io" and actions == "pull,push"
    try:
        response = request(url, headers)
        require(not check_write, "Docker Hub write preflight requires a Bearer authentication challenge")
    except urllib.error.HTTPError as error:
        if error.code != 401:
            require(not (check_write and error.code == 404),
                    "Docker Hub write access was not verified before the missing manifest response")
            return registry_error(error)
        challenge = error.headers.get("WWW-Authenticate", "")
        require(challenge.lower().startswith("bearer "), "Registry must provide Bearer authentication")
        fields = dict(re.findall(r'(\w+)="([^"\r\n]*)"', challenge[7:]))
        realm = urllib.parse.urlsplit(fields.get("realm", ""))
        allowed = {host.split(":")[0]}
        if host == "registry-1.docker.io":
            allowed.add("auth.docker.io")
        require(realm.scheme == "https" and realm.hostname in allowed
                and realm.username is None and realm.password is None and not realm.fragment,
                "Untrusted registry token endpoint")
        query = urllib.parse.parse_qsl(realm.query)
        query.extend((("service", fields.get("service", host)),
                      ("scope", f"repository:{repository}:{actions}")))
        token_url = urllib.parse.urlunsplit((realm.scheme, realm.netloc, realm.path,
                                            urllib.parse.urlencode(query), ""))
        token_headers = {}
        if credentials:
            basic = base64.b64encode(":".join(credentials).encode()).decode()
            token_headers["Authorization"] = "Basic " + basic
        with request(token_url, token_headers) as token_response:
            token_data = json.load(token_response)
        token = token_data.get("token") or token_data.get("access_token")
        require(isinstance(token, str) and token and "\n" not in token and "\r" not in token,
                "Registry returned an invalid Bearer token")
        if check_write:
            dockerhub_write_access(token, repository)
        headers["Authorization"] = "Bearer " + token
        try:
            response = request(url, headers)
        except urllib.error.HTTPError as authenticated_error:
            return registry_error(authenticated_error)
    with response:
        body = response.read()
        data = json.loads(body)
        require(data.get("schemaVersion") == 2, "Registry returned an invalid OCI manifest")
        result = response.headers.get("Docker-Content-Digest") or "sha256:" + hashlib.sha256(body).hexdigest()
        require(re.fullmatch("sha256:" + SHA256, result), "Registry returned an invalid manifest digest")
        if package_sha256 is not None:
            require(result == "sha256:" + hashlib.sha256(body).hexdigest(), "Manifest digest does not match its bytes")
            require(data.get("mediaType") == "application/vnd.oci.image.manifest.v1+json"
                    and data.get("config", {}).get("mediaType") == "application/vnd.cncf.helm.config.v1+json",
                    "Signing requires a Helm OCI manifest")
            layers = [layer for layer in data.get("layers", [])
                      if layer.get("mediaType") == "application/vnd.cncf.helm.chart.content.v1.tar+gzip"]
            require(len(layers) == 1 and layers[0].get("digest") == "sha256:" + package_sha256,
                    "Signing subject does not contain the tested chart package")
        return result


def registry_error(error):
    if error.code == 404:
        try:
            errors = json.loads(error.read()).get("errors", [])
        except (ValueError, AttributeError):
            errors = []
        if errors and all(isinstance(item, dict) and item.get("code") in
                          ("MANIFEST_UNKNOWN", "NAME_UNKNOWN") for item in errors):
            return None
    raise ValueError(f"Registry request failed with HTTP {error.code}; refusing to treat it as absent") from None


def split_image(image):
    require(isinstance(image, str) and image and not any(c.isspace() for c in image), "Invalid image reference")
    if "@" in image:
        name, reference = image.rsplit("@", 1)
        require(re.fullmatch("sha256:" + SHA256, reference), "Images must use a sha256 digest")
    else:
        name, separator, reference = image.rpartition(":")
        require(separator and "/" not in reference, f"Image must have an explicit tag: {image}")
    first, separator, rest = name.partition("/")
    if separator and ("." in first or ":" in first or first == "localhost"):
        host, repository = first, rest
    else:
        host, repository = "registry-1.docker.io", name
        if "/" not in repository:
            repository = "library/" + repository
    if host in ("docker.io", "index.docker.io"):
        host = "registry-1.docker.io"
    return host, repository, reference


def fixed_image(image):
    _, _, reference = split_image(image)
    require(reference.startswith("sha256:") or re.fullmatch(STABLE, reference),
            f"Release image must use a stable x.y.z tag or sha256 digest: {image}")


def rendered_images(documents):
    result = set()
    def visit(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("containers", "initContainers", "ephemeralContainers"):
                    require(isinstance(value, list), "Invalid rendered containers")
                    for container in value:
                        require(isinstance(container, dict) and isinstance(container.get("image"), str),
                                "Rendered container has no image")
                        result.add(container["image"])
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)
    for document in documents:
        visit(document)
    require(result, "Chart did not render any images")
    return sorted(result)


def registry_image(reference, registry):
    first = reference.split("/", 1)[0]
    qualified = "/" in reference and ("." in first or ":" in first or first == "localhost")
    return registry + "/" + reference if registry and not qualified else reference


def component_images(values, app_version):
    images = {}
    registry = values.get("global", {}).get("imageRegistry", "").rstrip("/")
    for component in ("pd", "store", "server", "hubble"):
        image = values[component]["image"]
        repository = image["repository"]
        repository = registry_image(repository, registry)
        tag = str(image.get("tag") or app_version)
        reference = repository + ("@" + image["digest"] if image.get("digest") else ":" + tag)
        images[component] = reference
    return images


def helper_images(values):
    registry = values.get("global", {}).get("imageRegistry", "").rstrip("/")
    return {component: registry_image(values[component]["waitImage"], registry)
            for component in ("store", "server") if values[component].get("waitImage")}


def pinned_image(image, image_digest):
    host, repository, _ = split_image(image)
    return f"{host}/{repository}@{image_digest}"


def configured_images(values, app_version):
    images = component_images(values, app_version)
    for component, image in images.items():
        fixed_image(image)
        if component != "hubble" and "@" not in image:
            require(split_image(image)[2] == app_version, f"{component} image tag must match release appVersion")
    return list(images.values())


def chart_metadata(package):
    with tarfile.open(package, "r:gz") as archive:
        members = [member for member in archive.getmembers() if member.name == NAME + "/Chart.yaml"]
        require(len(members) == 1 and members[0].isfile(), "Package must contain one root Chart.yaml")
        return yaml.safe_load(archive.extractfile(members[0]))


def package(source, sha, ref, channel, run_number, dist):
    require(re.fullmatch(SHA, sha), "Invalid source SHA")
    require(subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip() == sha,
            "Checkout does not match resolved source SHA")
    require(subprocess.run(["git", "-C", str(source), "diff", "--quiet", "HEAD", "--"],
                           check=False).returncode == 0, "Tracked source changed after checkout")
    require(not subprocess.check_output(["git", "-C", str(source), "ls-files", "--others",
                                         "--", "helm/hugegraph"], text=True).strip(),
            "Untracked chart files are not part of the resolved source")
    source_version(source)
    chart_path = source / "helm/hugegraph"
    chart = yaml.safe_load((chart_path / "Chart.yaml").read_text())
    require(chart.get("name") == NAME, f"Chart.name must be {NAME}")
    stable(chart.get("version"), "Source Chart.version")
    require(channel in ("dev", "release"), "Invalid channel")
    version = chart["version"]
    if channel == "dev":
        require(re.fullmatch(r"[1-9][0-9]*", str(run_number)), "Invalid run number")
        version += "-dev." + str(run_number)
    else:
        release_gate(chart, sha)
    require(not dist.exists() or not any(dist.iterdir()), "Output directory must be empty")
    dist.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        copy = Path(temp) / NAME
        shutil.copytree(chart_path, copy)
        # Only this disposable copy receives a development chart version.
        if channel == "dev":
            metadata = copy / "Chart.yaml"
            text = metadata.read_text()
            text, count = re.subn(r"(?m)^version:.*$", "version: " + version, text)
            require(count == 1, "Chart must declare exactly one version")
            metadata.write_text(text)
        subprocess.run(["helm", "lint", str(copy), "--strict"], check=True)
        subprocess.run(["helm", "package", str(copy), "--destination", str(dist)], check=True)
    artifact = dist / f"{NAME}-{version}.tgz"
    require(artifact.is_file(), "Helm did not create the expected package")
    metadata = chart_metadata(artifact)
    require(metadata.get("name") == NAME and metadata.get("version") == version,
            "Packaged metadata does not match intended release")
    rendered = subprocess.check_output(["helm", "template", "helm-audit", str(artifact)], text=True)
    images = rendered_images(yaml.safe_load_all(rendered))
    values = yaml.safe_load((chart_path / "values.yaml").read_text())
    components = component_images(values, str(chart.get("appVersion", "")))
    if channel == "release":
        configured_images(values, chart["appVersion"])
    helpers = helper_images(values)
    images = sorted(set(images + list(components.values()) + list(helpers.values())))
    checked_images = {}
    for image in images:
        if channel == "release":
            fixed_image(image)
        image_digest = registry_manifest(*split_image(image))
        require(image_digest is not None, f"Image is not published: {image}")
        checked_images[image] = image_digest
    image_values = {component: {"image": {"digest": checked_images[image]}}
                    for component, image in components.items()}
    for component, image in helpers.items():
        image_values[component]["waitImage"] = pinned_image(image, checked_images[image])
    (dist / "image-values.yaml").write_text(yaml.safe_dump(image_values, sort_keys=True))
    pinned_rendered = subprocess.check_output([
        "helm", "template", "helm-audit", str(artifact), "-f", str(dist / "image-values.yaml")], text=True)
    pinned_images = rendered_images(yaml.safe_load_all(pinned_rendered))
    expected_pins = {pinned_image(image, value) for image, value in checked_images.items()}
    require(all("@" in image and pinned_image(image, split_image(image)[2]) in expected_pins
                for image in pinned_images), "Rendered image cannot be pinned by supported image values")
    data = {
        "schema": 1, "source": SOURCE, "source_ref": ref, "source_sha": sha,
        "channel": channel, "name": NAME, "version": version,
        "app_version": str(chart.get("appVersion", "")),
        "package": artifact.name, "package_sha256": digest(artifact),
        "images": images, "image_digests": checked_images,
        "pinned_images": sorted(expected_pins),
        "image_values_sha256": digest(dist / "image-values.yaml"),
    }
    write_json(dist / "manifest.json", data)
    output(version=version, package=artifact.name, package_sha=digest(artifact),
           manifest_sha=digest(dist / "manifest.json"))
    print(f"Packaged {NAME} {version}: sha256:{digest(artifact)}")


def write_json(path, data):
    path.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n")


def verify(dist, manifest_sha, sealed=True):
    require(re.fullmatch(SHA256, manifest_sha), "Invalid expected manifest SHA256")
    manifest = dist / "manifest.json"
    require(manifest.is_file() and not manifest.is_symlink() and digest(manifest) == manifest_sha,
            "Manifest hash mismatch")
    data = json.loads(manifest.read_text())
    require(data.get("schema") == 1 and data.get("source") == SOURCE and data.get("name") == NAME,
            "Unexpected manifest identity")
    require(re.fullmatch(SHA, data.get("source_sha", "")), "Invalid manifest source SHA")
    version = data.get("version", "")
    require(re.fullmatch(STABLE + r"(?:-dev\.[1-9][0-9]*)?", version), "Invalid manifest chart version")
    require(data.get("channel") in ("dev", "release") and
            ("-dev." in version) == (data["channel"] == "dev"), "Manifest channel mismatch")
    filename = f"{NAME}-{version}.tgz"
    require(data.get("package") == filename, "Unexpected package filename")
    artifact = dist / filename
    require(artifact.is_file() and not artifact.is_symlink() and
            digest(artifact) == data.get("package_sha256"), "Package hash mismatch")
    metadata = chart_metadata(artifact)
    require(metadata.get("name") == NAME and metadata.get("version") == version
            and str(metadata.get("appVersion", "")) == data.get("app_version"), "Package metadata mismatch")
    image_values = dist / "image-values.yaml"
    require(image_values.is_file() and not image_values.is_symlink() and
            digest(image_values) == data.get("image_values_sha256"), "Image values hash mismatch")
    allowed = {"manifest.json", filename, "image-values.yaml"}
    if sealed:
        validation = dist / "validation.json"
        require(validation.is_file() and not validation.is_symlink() and
                digest(validation) == data.get("validation_sha256"), "Missing or modified validation result")
        validate_result(json.loads(validation.read_text()), data)
        allowed.add("validation.json")
    require({path.name for path in dist.iterdir()} == allowed, "Unexpected bundle contents")
    return data, artifact


def validate_result(result, data):
    require(result.get("status") == "passed" and result.get("source_sha") == data["source_sha"]
            and result.get("package_sha256") == data["package_sha256"],
            "Validation must pass for the exact source and package")


def seal(dist, manifest_sha, validation):
    data, _ = verify(dist, manifest_sha, sealed=False)
    result = json.loads(validation.read_text())
    validate_result(result, data)
    write_json(dist / "validation.json", result)
    data["validation_sha256"] = digest(dist / "validation.json")
    write_json(dist / "manifest.json", data)
    output(manifest_sha=digest(dist / "manifest.json"))


def helm_environment(directory):
    env = os.environ.copy()
    env["HELM_REGISTRY_CONFIG"] = str(directory / "registry.json")
    env["DOCKER_CONFIG"] = str(directory / "docker")
    Path(env["DOCKER_CONFIG"]).mkdir()
    Path(env["HELM_REGISTRY_CONFIG"]).write_text('{"auths": {}}')
    (Path(env["DOCKER_CONFIG"]) / "config.json").write_text('{"auths": {}}')
    # Login receives only its own password through stdin. Helm never inherits
    # either registry's credentials (or the GitHub API token) as environment data.
    for key in (*REGISTRY_CREDENTIALS, "GITHUB_TOKEN", "GH_TOKEN", *OIDC_ENVIRONMENT):
        env.pop(key, None)
    return env


def pull_compare(uri, data, artifact, env):
    with tempfile.TemporaryDirectory() as temp:
        subprocess.run(["helm", "pull", uri, "--version", data["version"],
                        "--destination", temp], env=env, check=True)
        pulled = Path(temp) / artifact.name
        require(pulled.is_file() and digest(pulled) == data["package_sha256"],
                "Registry contains different bytes for this chart version; refusing overwrite")
        metadata = chart_metadata(pulled)
        require(metadata.get("name") == data["name"] and metadata.get("version") == data["version"],
                "Pulled chart metadata mismatch")


def publication_gate(data, source_sha, channel, run_number, version):
    """Bind the unprivileged validation bundle to trusted dispatch/source job inputs."""
    require(re.fullmatch(SHA, source_sha) and data["source_sha"] == source_sha,
            "Publication source does not match trusted source resolution")
    require(data["channel"] == channel, "Publication channel does not match dispatch")
    require(data["version"] == version, "Publication version does not match the workflow concurrency lock")
    if channel == "dev":
        require(re.fullmatch(r"[1-9][0-9]*", str(run_number)) and
                data["version"].endswith("-dev." + str(run_number)),
                "Development version must belong to this workflow run")
    else:
        release_gate({"appVersion": data["app_version"]}, source_sha)


def signing_identity():
    workflow_ref = os.environ.get("GITHUB_WORKFLOW_REF", "")
    require(re.fullmatch(re.escape(SIGNING_WORKFLOW) + r"@refs/(?:heads|tags)/[^\s]+", workflow_ref),
            "Signing requires the HugeGraph Helm publishing workflow identity")
    require(all(os.environ.get(key) for key in OIDC_ENVIRONMENT), "Signing requires GitHub Actions OIDC permission")
    return "https://github.com/" + workflow_ref


def sign_chart(target, reference, env, anonymous, identity):
    """Sign the immutable manifest and verify with no registry or OIDC credentials."""
    host = TARGETS[target][0]
    subprocess.run(["cosign", "login", host, "--username", os.environ[target.upper() + "_USERNAME"],
                    "--password-stdin"], input=os.environ[target.upper() + "_PASSWORD"] + "\n",
                   text=True, env=env, check=True)
    signing = dict(env, **{key: os.environ[key] for key in OIDC_ENVIRONMENT})
    subprocess.run(["cosign", "sign", "--yes", reference], env=signing, check=True)
    subprocess.run(["cosign", "verify", reference, "--certificate-identity", identity,
                    "--certificate-oidc-issuer", OIDC_ISSUER], env=anonymous,
                   stdout=subprocess.DEVNULL, check=True)


def publish(dist, manifest_sha, source_sha, channel, run_number, version):
    data, artifact = verify(dist, manifest_sha)
    publication_gate(data, source_sha, channel, run_number, version)
    identity = signing_identity()
    # The workflow holds ONE chart-version lock for this entire operation.
    # Inspect both registries before either write, including existing package bytes.
    with ExitStack() as stack:
        plans = []
        for target, (host, namespace) in TARGETS.items():
            username = os.environ.get(target.upper() + "_USERNAME", "")
            password = os.environ.get(target.upper() + "_PASSWORD", "")
            require(username and password, f"{target}: registry username and password are required")
            config = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            env = helm_environment(config)
            subprocess.run(["helm", "registry", "login", host, "--username", username,
                            "--password-stdin"], input=password + "\n", text=True, env=env, check=True)
            parent = f"oci://{host}/{namespace}"
            uri = parent + "/" + NAME
            existing = registry_manifest(host, namespace + "/" + NAME,
                                         data["version"], (username, password), actions="pull,push")
            if existing is not None:
                print(f"{target}: existing version found; comparing original package bytes")
                pull_compare(uri, data, artifact, env)
            plans.append((target, parent, uri, existing, env))
        failures = []
        for target, parent, uri, existing, env in plans:
            try:
                if existing is None:
                    subprocess.run(["helm", "push", str(artifact), parent], env=env, check=True)
                pull_compare(uri, data, artifact, env)
                with tempfile.TemporaryDirectory() as temporary:
                    anonymous = helm_environment(Path(temporary))
                    pull_compare(uri, data, artifact, anonymous)
                    host, repository = uri.removeprefix("oci://").split("/", 1)
                    manifest_digest = registry_manifest(host, repository, data["version"],
                                                        package_sha256=data["package_sha256"])
                    require(manifest_digest is not None, "Published chart manifest is missing")
                    require(existing is None or existing == manifest_digest, "Chart tag changed after preflight")
                    reference = f"{host}/{repository}@{manifest_digest}"
                    sign_chart(target, reference, env, anonymous, identity)
                    require(registry_manifest(host, repository, data["version"],
                                              package_sha256=data["package_sha256"]) == manifest_digest,
                            "Chart tag changed while signing")
                print(f"{target}: signed and anonymously verified {reference} as {identity}")
            except (ValueError, OSError, subprocess.CalledProcessError) as error:
                failures.append(f"{target}: {error}")
        require(not failures, "Publication incomplete; re-run the failed publish job with its original artifact. "
                + "; ".join(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("resolve")
    command.add_argument("--ref", required=True)
    command.add_argument("--channel", choices=("dev", "release"), default="dev")
    command = commands.add_parser("package")
    command.add_argument("--source", type=Path, required=True)
    command.add_argument("--sha", required=True)
    command.add_argument("--ref", required=True)
    command.add_argument("--channel", choices=("dev", "release"), required=True)
    command.add_argument("--run-number", required=True)
    command.add_argument("--dist", type=Path, required=True)
    for name in ("seal", "verify", "publish"):
        command = commands.add_parser(name)
        command.add_argument("--dist", type=Path, required=True)
        command.add_argument("--manifest-sha", required=True)
        if name == "seal":
            command.add_argument("--validation", type=Path, required=True)
        if name == "publish":
            command.add_argument("--source-sha", required=True)
            command.add_argument("--channel", choices=("dev", "release"), required=True)
            command.add_argument("--run-number", required=True)
            command.add_argument("--version", required=True)
    args = vars(parser.parse_args())
    name = args.pop("command")
    globals()[name](**args)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from None
