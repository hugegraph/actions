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
PACKAGE = "hugegraph-python"
MODULE = "hugegraph-python-client"
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


def github(endpoint):
    return json.loads(
        subprocess.check_output(["gh", "api", f"repos/{SOURCE}/{endpoint}"])
    )


def resolve(ref, target):
    require(bool(ref) and not any(c in ref for c in "\n\r"), "Invalid source ref")
    tag = ref.removeprefix("refs/tags/")
    if target == "pypi":
        obj = github("git/ref/tags/" + urllib.parse.quote(tag, safe=""))["object"]
        while obj["type"] == "tag":
            obj = github("git/tags/" + obj["sha"])["object"]
        require(obj["type"] == "commit", "Tag must identify a commit")
        sha = obj["sha"]
    else:
        sha = github("commits/" + urllib.parse.quote(ref, safe=""))["sha"]
        tag = ""
    require(re.fullmatch(r"[0-9a-f]{40}", sha), "Invalid source SHA")
    output(source_sha=sha, tag=tag)


def metadata(source, target, tag):
    project = tomllib.loads((source / MODULE / "pyproject.toml").read_text())["project"]
    require(project["name"] == PACKAGE, f"Distribution name must be {PACKAGE}")
    version = project["version"]
    require(re.fullmatch(r"[0-9][A-Za-z0-9.!+]*", version), "Invalid static version")
    if target == "pypi":
        require(tag == version, "Tag/version mismatch")
    output(version=version)


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


def inventory(dist, version):
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
        require(
            re.fullmatch(
                r"hugegraph_python-[A-Za-z0-9_.+!-]+\.(whl|tar\.gz)", path.name
            ),
            "Unexpected filename",
        )
        require(
            artifact_metadata(path) == (PACKAGE, version), "Artifact metadata mismatch"
        )
        result[path.name] = digest(path)
    return result


def manifest(dist, sha, version):
    require(re.fullmatch(r"[0-9a-f]{40}", sha), "Invalid source SHA")
    data = {
        "source": SOURCE,
        "source_sha": sha,
        "name": PACKAGE,
        "version": version,
        "files": inventory(dist, version),
    }
    path = dist / "manifest.json"
    path.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n")
    output(manifest_sha=digest(path))


def verify(dist, sha, version, manifest_sha):
    path = dist / "manifest.json"
    require(path.is_file() and not path.is_symlink(), "Missing regular manifest")
    require(digest(path) == manifest_sha, "Manifest hash mismatch")
    data = json.loads(path.read_text())
    require(
        data
        == {
            "source": SOURCE,
            "source_sha": sha,
            "name": PACKAGE,
            "version": version,
            "files": inventory(dist, version),
        },
        "Manifest contents mismatch",
    )
    return data["files"]


def remote_files(target, version):
    url = f"{TARGETS[target][1]}/pypi/{PACKAGE}/{urllib.parse.quote(version, safe='')}/json"
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


def publish(dist, sha, version, manifest_sha, target):
    files = verify(dist, sha, version, manifest_sha)
    remote = remote_files(target, version)
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
        "command", choices=("resolve", "metadata", "manifest", "verify", "publish")
    )
    parser.add_argument("--target", choices=TARGETS, default="testpypi")
    parser.add_argument("--source-ref", default="main")
    parser.add_argument("--source", type=Path, default=Path("source"))
    parser.add_argument("--tag", default="")
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument("--sha", default="")
    parser.add_argument("--version", default="")
    parser.add_argument("--manifest-sha", default="")
    args = parser.parse_args()
    if args.command == "resolve":
        resolve(args.source_ref, args.target)
    elif args.command == "metadata":
        metadata(args.source, args.target, args.tag)
    elif args.command == "manifest":
        manifest(args.dist, args.sha, args.version)
    elif args.command == "verify":
        verify(args.dist, args.sha, args.version, args.manifest_sha)
    else:
        publish(args.dist, args.sha, args.version, args.manifest_sha, args.target)


if __name__ == "__main__":
    main()
