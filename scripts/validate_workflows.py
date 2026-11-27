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
    workers = [n for n in workflow["nodes"] if n["type"] == required_roles["worker"]]
    if path.name in ("01-order-intake.json", "02-support-triage.json"):
        expected = "process-next" if path.name.startswith("01") else "triage-next"
        regular_url = f"http://api:8000/api/n8n/{expected}?batch_size=5"
        by_url = {worker["parameters"]["url"]: worker["name"] for worker in workers}
        expected_urls = {regular_url} if path.name.startswith("01") else {regular_url, regular_url + "&mock_only=true"}
        assert len(workers) == len(expected_urls) and set(by_url) == expected_urls
        schedules = [n for n in workflow["nodes"] if n["type"] == required_roles["scheduleTrigger"]]
        assert len(schedules) == len(expected_urls)
        for schedule in schedules:
            interval = schedule["parameters"]["rule"]["interval"]
            if interval == [{"field":"minutes", "minutesInterval":1}]:
                url = regular_url
            else:
                assert path.name == "02-support-triage.json"
                assert interval == [{"field":"cronExpression", "expression":"30 * * * * *"}]
                url = regular_url + "&mock_only=true"
            assert workflow["connections"][schedule["name"]]["main"] == [[{"node": by_url[url], "type":"main", "index":0}]]
        assert sum(s["parameters"]["rule"]["interval"] == [{"field":"minutes", "minutesInterval":1}] for s in schedules) == 1
    print(f"{path}: valid JSON; {len(names)} nodes; connections resolve")
