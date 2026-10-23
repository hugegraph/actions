"""Validate Gremlin responses and Pod/PVC identity across Helm recovery checks."""

import argparse
import json
from pathlib import Path


def check_response(response, phase):
    if response.get("status", {}).get("code") != 200:
        raise ValueError(f"{phase}: Gremlin request failed: {response}")
    if response.get("result", {}).get("data") != [f"helm-ci {phase} passed"]:
        raise ValueError(f"{phase}: expected smoke assertions did not complete: {response}")


def check_recovery(before_pods, after_pods, before_claims, after_claims):
    result = {"pods": {}, "claims": {}}
    for component in ("store", "server"):
        matches = [[pod for pod in snapshot["items"]
                    if pod["metadata"].get("labels", {}).get("app.kubernetes.io/component") == component]
                   for snapshot in (before_pods, after_pods)]
        if any(len(pods) != 1 for pods in matches):
            raise ValueError(f"{component}: expected exactly one Pod before and after recreation")
        before, after = (pods[0] for pods in matches)
        old_uid, new_uid = before["metadata"]["uid"], after["metadata"]["uid"]
        if not old_uid or not new_uid or old_uid == new_uid:
            raise ValueError(f"{component}: Pod was not recreated")
        if not any(condition.get("type") == "Ready" and condition.get("status") == "True"
                   for condition in after.get("status", {}).get("conditions", [])):
            raise ValueError(f"{component}: replacement Pod is not ready")
        old_mounts, new_mounts = ({volume["persistentVolumeClaim"]["claimName"]
                                 for volume in pod["spec"].get("volumes", [])
                                 if "persistentVolumeClaim" in volume} for pod in (before, after))
        if old_mounts != new_mounts or (component == "store" and len(new_mounts) != 1):
            raise ValueError(f"{component}: replacement Pod changed its persistent claims")
        result["pods"][component] = {"before_uid": old_uid, "after_uid": new_uid}
    before, after = ({claim["metadata"]["name"]: claim for claim in snapshot["items"]}
                     for snapshot in (before_claims, after_claims))
    if len(before) != 2 or before.keys() != after.keys():
        raise ValueError("Expected the same two persistent claims after recreation")
    for name, old in before.items():
        new = after[name]
        if (not old["metadata"].get("uid") or old["metadata"]["uid"] != new["metadata"].get("uid")
                or not old["spec"].get("volumeName")
                or old["spec"]["volumeName"] != new["spec"].get("volumeName")
                or new.get("status", {}).get("phase") != "Bound"):
            raise ValueError(f"{name}: claim or bound volume changed during recreation")
        result["claims"][name] = {"uid": new["metadata"]["uid"], "volume": new["spec"]["volumeName"]}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    response = commands.add_parser("response")
    response.add_argument("phase", choices=("verify", "cleanup"))
    response.add_argument("path", type=Path)
    recovery = commands.add_parser("recovery")
    recovery.add_argument("snapshots", type=Path, nargs=4,
                          metavar="JSON")
    args = parser.parse_args()
    if args.command == "response":
        check_response(json.loads(args.path.read_text()), args.phase)
    else:
        print(json.dumps(check_recovery(*(json.loads(path.read_text()) for path in args.snapshots)), indent=2))


if __name__ == "__main__":
    main()
