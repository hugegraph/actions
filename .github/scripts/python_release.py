"""Guards for the single HugeGraph Python release workflow (Python 3.11+)."""

import argparse
import email.parser
import hashlib
import json
import os
import re
import subprocess
import tarfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

SOURCE = "apache/hugegraph-ai"
SOURCES = (SOURCE, "hugegraph/hugegraph-ai")
COMPONENTS = {
    "client": ("hugegraph-python", "hugegraph-python-client"),
    "mcp": ("hugegraph-mcp", "hugegraph-mcp"),
}
TARGETS = {
    "testpypi": ("https://test.pypi.org/legacy/", "https://test.pypi.org"),
    "pypi": ("https://upload.pypi.org/legacy/", "https://pypi.org"),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def output(**values):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        for key, value in values.items():
            require("\n" not in value and "\r" not in value, "Invalid output")
            stream.write(f"{key}={value}\n")


def github(endpoint, repository=SOURCE):
    return json.loads(
        subprocess.check_output(["gh", "api", f"repos/{repository}/{endpoint}"])
    )


def resolve(ref, repository=SOURCE):
    require(repository in SOURCES, "Unsupported source repository")
    require(bool(ref) and not any(c in ref for c in "\n\r"), "Invalid source ref")
    sha = github("commits/" + urllib.parse.quote(ref, safe=""), repository)["sha"]
    require(re.fullmatch(r"[0-9a-f]{40}", sha), "Invalid source SHA")
    output(source_sha=sha)


def metadata(source, target, version, component="client"):
    package, module = COMPONENTS[component]
    project = tomllib.loads((source / module / "pyproject.toml").read_text())["project"]
    require(project["name"] == package, f"Distribution name must be {package}")
    base = project["version"]
    number = r"(?:0|[1-9][0-9]*)"
    require(
        re.fullmatch(rf"{number}\.{number}\.{number}", base),
        "Source version must be x.y.z",
    )
    if target == "pypi":
        require(not version, "test_version must be empty for PyPI")
        version = base
    else:
        require(
            re.fullmatch(rf"{number}\.{number}\.{number}\.{number}", version),
            "TestPyPI requires test_version in x.y.z.n format",
        )
        require(
            version.rsplit(".", 1)[0] == base,
            "Test version must extend the source version",
        )
        subprocess.run(
            [
                "uv",
                "version",
                "--project",
                str(source / module),
                "--frozen",
                version,
            ],
            check=True,
        )
    output(version=version, module=module, package=package)


def artifact_metadata(path):
    if path.name.endswith(".whl"):
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
            require(len(names) == 1, "Wheel must contain exactly one METADATA")
            raw = archive.read(names[0])
    else:
        with tarfile.open(path, "r:gz") as archive:
            members = [
                m
                for m in archive.getmembers()
                if m.name.count("/") == 1 and m.name.endswith("/PKG-INFO")
            ]
            require(
                len(members) == 1 and members[0].isfile(),
                "Sdist must contain one root PKG-INFO",
            )
            raw = archive.extractfile(members[0]).read()
    msg = email.parser.BytesParser().parsebytes(raw)
    require(
        len(msg.get_all("Name", [])) == 1 and len(msg.get_all("Version", [])) == 1,
        "Ambiguous metadata",
    )
    return msg["Name"], msg["Version"]


def inventory(dist, version, component="client"):
    package, _ = COMPONENTS[component]
    # uv creates this hidden cache marker; upload-artifact omits hidden files.
    files = sorted(
        p for p in dist.iterdir() if p.name not in ("manifest.json", ".gitignore")
    )
    require(len(files) == 2, "Expected exactly one wheel and one sdist")
    require(sum(p.name.endswith(".whl") for p in files) == 1, "Expected one wheel")
    require(sum(p.name.endswith(".tar.gz") for p in files) == 1, "Expected one sdist")
    result = {}
    for path in files:
        require(path.is_file() and not path.is_symlink(), "Expected regular artifact")
        name_pattern = re.escape(package.replace("-", "_"))
        if path.name.endswith(".tar.gz"):
            name_pattern = name_pattern.replace("_", "[-_]")
        require(
            re.fullmatch(
                name_pattern + r"-[A-Za-z0-9_.+!-]+\.(whl|tar\.gz)",
                path.name,
            ),
            "Unexpected filename",
        )
        require(
            artifact_metadata(path) == (package, version), "Artifact metadata mismatch"
        )
        result[path.name] = digest(path)
    return result


def manifest(dist, sha, version, component="client", repository=SOURCE):
    require(repository in SOURCES, "Unsupported source repository")
    require(re.fullmatch(r"[0-9a-f]{40}", sha), "Invalid source SHA")
    data = {
        "source": repository,
        "source_sha": sha,
        "name": COMPONENTS[component][0],
        "version": version,
        "files": inventory(dist, version, component),
    }
    path = dist / "manifest.json"
    path.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n")
    output(manifest_sha=digest(path))


def verify(dist, sha, version, manifest_sha, component="client", repository=SOURCE):
    require(repository in SOURCES, "Unsupported source repository")
    path = dist / "manifest.json"
    require(path.is_file() and not path.is_symlink(), "Missing regular manifest")
    require(digest(path) == manifest_sha, "Manifest hash mismatch")
    data = json.loads(path.read_text())
    require(
        data
        == {
            "source": repository,
            "source_sha": sha,
            "name": COMPONENTS[component][0],
            "version": version,
            "files": inventory(dist, version, component),
        },
        "Manifest contents mismatch",
    )
    return data["files"]


def remote_files(target, version, component="client"):
    package, _ = COMPONENTS[component]
    url = f"{TARGETS[target][1]}/pypi/{package}/{urllib.parse.quote(version, safe='')}/json"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {}
        raise
    result = {}
    for item in data["urls"]:
        name, sha = item["filename"], item["digests"]["sha256"]
        require(re.fullmatch(r"[0-9a-f]{64}", sha), "Missing/invalid remote SHA-256")
        require(name not in result, "Duplicate remote filename")
        result[name] = sha
    return result


def test_client(version):
    """Pin one published TestPyPI client wheel without mixing package indexes."""
    require(
        re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){3}", version),
        "Test client version must be x.y.z.n",
    )
    url = f"https://test.pypi.org/pypi/hugegraph-python/{version}/json"
    with urllib.request.urlopen(url, timeout=30) as response:
        data = json.load(response)
    require(
        data["info"]["name"] == "hugegraph-python"
        and data["info"]["version"] == version,
        "Test client metadata mismatch",
    )
    filename = f"hugegraph_python-{version}-py3-none-any.whl"
    wheels = [item for item in data["urls"] if item["filename"] == filename]
    require(len(wheels) == 1, "Expected one universal TestPyPI client wheel")
    wheel = wheels[0]
    sha = wheel["digests"]["sha256"]
    require(re.fullmatch(r"[0-9a-f]{64}", sha), "Invalid TestPyPI client hash")
    parsed = urllib.parse.urlsplit(wheel["url"])
    require(
        parsed.scheme == "https"
        and parsed.netloc == "test-files.pythonhosted.org"
        and parsed.path.endswith("/" + filename)
        and not parsed.query
        and not parsed.fragment,
        "Unexpected TestPyPI client URL",
    )
    require(not wheel.get("yanked", False), "TestPyPI client wheel is yanked")
    output(client_requirement=f"hugegraph-python @ {wheel['url']}#sha256={sha}")


def publish(
    dist, sha, version, manifest_sha, target, component="client", repository=SOURCE
):
    require(
        target != "pypi" or repository == SOURCE,
        "PyPI publishing requires apache/hugegraph-ai source",
    )
    files = verify(dist, sha, version, manifest_sha, component, repository)
    remote = remote_files(target, version, component)
    # Finish the entire preflight before starting uv, including partial retries.
    unexpected = sorted(remote.keys() - files.keys())
    require(not unexpected, f"Unexpected remote artifacts: {', '.join(unexpected)}")
    for name, checksum in files.items():
        require(
            name not in remote or remote[name] == checksum,
            f"Remote hash conflict: {name}",
        )
    pending = [str((dist / name).resolve()) for name in files if name not in remote]
    if not pending:
        print("All artifacts already exist with identical SHA-256; nothing to upload.")
        return
    require(bool(os.environ.get("UV_PUBLISH_TOKEN")), "Missing environment token")
    subprocess.run(
        [
            "uv",
            "publish",
            "--no-config",
            "--trusted-publishing",
            "never",
            "--publish-url",
            TARGETS[target][0],
            "--check-url",
            TARGETS[target][1] + "/simple/",
            *pending,
        ],
        check=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("resolve", "metadata", "manifest", "verify", "test-client", "publish"),
    )
    parser.add_argument("--target", choices=TARGETS, default="testpypi")
    parser.add_argument("--source-ref", default="main")
    parser.add_argument("--source-repository", choices=SOURCES, default=SOURCE)
    parser.add_argument("--component", choices=COMPONENTS, default="client")
    parser.add_argument("--source", type=Path, default=Path("source"))
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument("--sha", default="")
    parser.add_argument("--version", default="")
    parser.add_argument("--test-version", default="")
    parser.add_argument("--manifest-sha", default="")
    args = parser.parse_args()
    if args.command == "test-client":
        test_client(args.version)
    elif args.command == "resolve":
        resolve(args.source_ref, args.source_repository)
    elif args.command == "metadata":
        metadata(args.source, args.target, args.test_version, args.component)
    elif args.command == "manifest":
        manifest(
            args.dist, args.sha, args.version, args.component, args.source_repository
        )
    elif args.command == "verify":
        verify(
            args.dist,
            args.sha,
            args.version,
            args.manifest_sha,
            args.component,
            args.source_repository,
        )
    else:
        publish(
            args.dist,
            args.sha,
            args.version,
            args.manifest_sha,
            args.target,
            args.component,
            args.source_repository,
        )


if __name__ == "__main__":
    main()
