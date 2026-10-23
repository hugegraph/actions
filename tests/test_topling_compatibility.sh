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
PY
