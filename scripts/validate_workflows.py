import json
from pathlib import Path

paths = sorted(Path("workflows").glob("*.json"))
assert len(paths) == 3, f"expected exactly three workflows, found {len(paths)}"
required_roles = {
    "scheduleTrigger": "n8n-nodes-base.scheduleTrigger",
    "webhook": "n8n-nodes-base.webhook",
    "worker": "n8n-nodes-base.httpRequest",
}

for path in paths:
    workflow = json.loads(path.read_text())
    names = {node["name"] for node in workflow["nodes"]}
    node_types = {node["type"] for node in workflow["nodes"]}
    assert len(names) == len(workflow["nodes"]), f"{path}: duplicate node names"
    for role, node_type in required_roles.items():
        assert node_type in node_types, f"{path}: missing {role} node"
    for source, branches in workflow.get("connections", {}).items():
        assert source in names, f"{path}: unknown source {source}"
        for outputs in branches.values():
            for output in outputs:
                for connection in output:
                    assert (
                        connection["node"] in names
                    ), f"{path}: unknown target {connection['node']}"
    print(f"{path}: valid JSON; {len(names)} nodes; connections resolve")
