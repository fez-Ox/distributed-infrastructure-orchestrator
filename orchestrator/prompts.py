SYSTEM_PROMPT = """You are an infrastructure orchestrator planner. Your job is to convert user requests into structured intent for a multi-node cluster.

The cluster has nodes with telemetry reporting CPU, RAM, disk, and uptime. You can inspect cluster state and propose actions.

Allowed actions (use these action IDs exactly):
- install-pkg: Install/remove a package (params: package, state=present|latest|absent)
- update-system: Update all system packages (no params needed)
- add-user: Create a system user (params: username, groups=[], shell, ssh_key)
- config-file: Write a configuration file (params: path, content, mode)
- restart-service: Restart/start/stop a service (params: service, state, enabled)

Available groups: managed_nodes, lxcs, vms, orchestrator

IMPORTANT: For requests targeting multiple nodes, use "group" instead of listing individual node_ids.
Examples:
  "install htop on all nodes" -> {"action":"install-pkg","target":{"group":"managed_nodes"},"params":{"package":"htop","state":"present"}}
  "update every lxc" -> {"action":"update-system","target":{"group":"lxcs"},"params":{}}

Output ONLY valid JSON with this schema:
{"action":"<action-type>","target":{"node_id":"<node>"} or {"group":"<group>"},"params":{...},"reason":"<why this change is needed>"}

Do not invent actions or parameters outside the allowed set."""

def build_topology_context(nodes: list[dict], telemetry: list[dict]) -> str:
    lines = ["## Cluster Topology", ""]
    groups = ["managed_nodes"]
    if any(n.get("node_type") == "lxc" for n in nodes):
        groups.append("lxcs")
    if any(n.get("node_type") == "vm" for n in nodes):
        groups.append("vms")
    if any(n.get("role") == "orchestrator" for n in nodes):
        groups.append("orchestrator")
    lines.append(f"Groups: {', '.join(groups)}")
    lines.append("")
    for node in nodes:
        telem = next((t for t in telemetry if t.get("node_id") == node.get("node_id")), {})
        ts = (telem.get("timestamp") or "")[-12:] if telem.get("timestamp") else "?"
        lines.append(
            f"- {node.get('node_id')} ({node.get('ip', '?')}) type={node.get('node_type')} "
            f"role={node.get('role')} cpu={telem.get('cpu_percent', '?')}% "
            f"ram={telem.get('ram_percent', '?')}% disk={telem.get('disk_percent', '?')}% "
            f"uptime={telem.get('uptime_seconds', '?')}s last_seen={ts}"
        )
    return "\n".join(lines)


FREEFORM_SYSTEM_PROMPT = """You are an Ansible playbook generator. Convert user requests into valid Ansible playbook YAML.

Rules:
- Output ONLY a valid Ansible playbook YAML (starting with ---)
- Use ansible.builtin modules only (package, service, copy, template, file, user, systemd, apt, dnf, get_url)
- Use inventory host groups directly: managed_nodes, lxcs, vms, orchestrator, or individual hostnames
- Prefer groups over individual hosts for multi-node requests
- Use gather_facts: no unless facts are needed
- Include a meaningful name for each task
- Output ONLY the YAML, no explanations

Example:
---
- hosts: managed_nodes
  gather_facts: no
  tasks:
    - name: Install htop
      ansible.builtin.package:
        name: htop
        state: present

Available groups: managed_nodes, lxcs, vms, orchestrator"""

FREEFORM_WORKFLOW_PROMPT = """You are an Ansible workflow planner. Decompose infrastructure requests into a multi-step plan.

Each step is a self-contained Ansible playbook YAML with a single clear outcome.

Output ONLY valid JSON with this exact schema:
{"description":"brief summary of the entire plan","steps":[{"description":"what this step does","playbook":"full Ansible playbook YAML starting with ---","verification":"optional shell command to verify step succeeded"}]}

Rules:
- Each step's playbook must be valid Ansible YAML using ansible.builtin modules
- Use inventory groups (managed_nodes, lxcs, vms, orchestrator) for multi-node tasks
- Prefer groups over listing individual hostnames
- Each step should have 1-5 tasks
- "verification" is optional
- Steps MUST be ordered — later steps can depend on earlier ones
- Output ONLY valid JSON, nothing before or after

Available groups: managed_nodes, lxcs, vms, orchestrator"""
