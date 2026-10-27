#!/usr/bin/env bash

set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 - "$repo_root" <<'PY'
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

workflow = (Path(sys.argv[1]) / ".github/workflows/_publish_pd_store_server_reusable.yml").read_text()


def function(name):
    # Run the production stop/check function, preserving Bash's errexit behavior.
    match = re.search(r"^        " + name + r"\(\) \{\n.*?^        \}", workflow, re.MULTILINE | re.DOTALL)
    assert match, name
    return "\n".join(line[8:] for line in match[0].splitlines())


with tempfile.TemporaryDirectory(prefix="topling-stop-contract-") as temporary:
    directory = Path(temporary)
    docker = directory / "docker"
    docker.write_text("""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$DOCKER_CALLS"
case "$1" in
    compose|stop) exit "$STOP_STATUS" ;;
    inspect)
        case "$3" in
            *OOMKilled*) printf '%s\\n' "$OOM_KILLED" ;;
            *ExitCode*) printf '%s\\n' "$EXIT_CODE" ;;
            *) exit 90 ;;
        esac ;;
    *) exit 91 ;;
esac
""")
    docker.chmod(0o755)
    for name in ("stop_normally", "stop_standalone_normally"):
        script = "set -euo pipefail\ncompose_args=(-f compose.yml -f override.yml)\n" + function(name)
        script += "\n" + name + "\nprintf 'safe-to-switch\\n'\n"
        for stop_status, oom, code, expected_success in (
            ("0", "false", "0", True),
            ("0", "false", "143", True),
            ("42", "false", "0", False),
            ("0", "true", "0", False),
            ("0", "false", "137", False),
            ("0", "false", "1", False),
        ):
            calls = directory / "calls"
            calls.write_text("")
            environment = dict(os.environ, PATH=str(directory) + os.pathsep + os.environ["PATH"],
                               DOCKER_CALLS=str(calls), STOP_STATUS=stop_status,
                               OOM_KILLED=oom, EXIT_CODE=code)
            result = subprocess.run(["bash", "-c", script], env=environment, capture_output=True, text=True)
            assert (result.returncode == 0) == expected_success, (name, code, result.stdout, result.stderr)
            assert ("safe-to-switch" in result.stdout) == expected_success
            assert "stop -t 60" in calls.read_text(), calls.read_text()
        print(f"PASS: {name} blocks a switch after stop failure, OOM, SIGKILL or abnormal exit")

    options = (function("seed_buffer_config_lines") + "\n" +
               function("set_seed_buffer_options") + "\n" +
               function("restore_seed_buffer_options") + "\n")
    for layout in ("legacy", "persistent-directory", "file-symlink"):
        for case_number, original_options in enumerate((
            "",
            "rocksdb.write_buffer_size : 8388608\nrocksdb.min_write_buffer_number_to_merge = 3\n",
            "rocksdb.write_buffer_size=\\\n 8388608\nrocksdb.min_write_buffer_number_to_merge: 3\n",
        )):
            case = directory / (layout + "-" + str(case_number))
            (case / "conf").mkdir(parents=True)
            if layout == "persistent-directory":
                target = case / "data/.hugegraph-config/graphs"
                target.mkdir(parents=True)
                (case / "conf/graphs").symlink_to(target, target_is_directory=True)
            else:
                (case / "conf/graphs").mkdir()
            config = case / "conf/graphs/hugegraph.properties"
            if layout == "file-symlink":
                target = case / "data/hugegraph.properties"
                target.parent.mkdir()
                target.touch()
                config.symlink_to(target)
            unrelated = "custom.multiline=first\\\n rocksdb.write_buffer_size=literal\n"
            config.write_text("backend=rocksdb\nrocksdb.provider=rocksdb\n" + unrelated + original_options)
            config.chmod(0o640)
            receipt = case / "buffer-options"
            subprocess.run(["bash", "-c", options + 'set_seed_buffer_options "$1" "$2"',
                            "--", str(config), str(receipt)], check=True)
            assert receipt.read_text() == original_options
            assert "rocksdb.write_buffer_size=1048576\n" in config.read_text()
            assert "rocksdb.min_write_buffer_number_to_merge=1\n" in config.read_text()
            # Startup and dynamic API updates made after the snapshot must survive.
            config.write_text(config.read_text().replace("rocksdb.provider=rocksdb", "rocksdb.provider=topling") +
                              "custom.changed=preserved\n")
            dynamic = config.parent / "DEFAULT-ci_dynamic.properties"
            dynamic.write_text("backend=rocksdb\nrocksdb.provider=topling\nstore=ci_dynamic\n")
            restore = options + 'restore_seed_buffer_options "$1" "$2"'
            subprocess.run(["bash", "-c", restore, "--", str(config), str(receipt)], check=True)
            first_restore = config.read_text()
            assert original_options in first_restore
            assert "rocksdb.write_buffer_size=1048576\n" not in first_restore
            assert "rocksdb.min_write_buffer_number_to_merge=1\n" not in first_restore
            assert unrelated in first_restore
            assert "custom.changed=preserved\n" in first_restore
            assert "rocksdb.provider=topling\n" in first_restore
            assert dynamic.read_text() == "backend=rocksdb\nrocksdb.provider=topling\nstore=ci_dynamic\n"
            assert config.stat().st_mode & 0o777 == 0o640
            if layout == "file-symlink":
                assert config.is_symlink()
            if layout == "persistent-directory":
                assert (case / "conf/graphs").is_symlink()
            subprocess.run(["bash", "-c", restore, "--", str(config), str(receipt)], check=True)
            assert config.read_text() == first_restore
    print("PASS: seed options restore original/unset/continued values while preserving providers, dynamic definitions and symlinks")

    curl = directory / "curl"
    curl.write_text("""#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
args = sys.argv[1:]
Path(os.environ["QUERY_REQUEST_FILE"]).write_text(args[args.index("--data-binary") + 1])
print(json.dumps({"status":{"code":int(os.environ["QUERY_STATUS"])},"result":{"data":[]}}))
""")
    curl.chmod(0o755)
    request = directory / "request.json"
    environment = dict(os.environ, PATH=str(directory) + os.pathsep + os.environ["PATH"],
                       QUERY_REQUEST_FILE=str(request), QUERY_STATUS="200")
    import json
    for graph in ("", "ci_dynamic"):
        script = "set -euo pipefail\n" + function("standalone_query") + '\nstandalone_query "g.V().count().next()" "$1"'
        result = subprocess.run(["bash", "-c", script, "--", graph], env=environment,
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        name = graph or "hugegraph"
        assert json.loads(request.read_text())["aliases"] == {
            "graph":"DEFAULT-" + name, "g":"__g_DEFAULT-" + name}
    environment["QUERY_STATUS"] = "500"
    result = subprocess.run(["bash", "-c", script, "--", "ci_dynamic"], env=environment,
                            capture_output=True, text=True)
    assert result.returncode != 0
    print("PASS: dynamic graph queries select their own bindings and reject failed Gremlin responses")
PY
