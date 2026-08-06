# /// script
# dependencies = [
#   "requests",
# ]
# ///

import argparse
import json
import os
import random
import subprocess
import sys
import requests

# Hardcoded Default Configuration
DEFAULT_SERVER_IP = "YOUR_ZABBIX_SERVER_IP"
ZABBIX_API_URL = f"http://{DEFAULT_SERVER_IP}/zabbix/api_jsonrpc.php"
ZABBIX_API_TOKEN = "YOUR_ZABBIX_API_TOKEN_HERE"  # <--- Paste your token here
DEFAULT_PREFIX = "sim-host"
DEFAULT_IMAGE = "docker.io/zabbix/zabbix-agent2:alpine-7.0-latest"

# State file lives next to this script at all times
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sim_state.json")


# ==========================================
# STATE MANAGEMENT
# ==========================================
def load_state():
    """Load host state from the JSON state file."""
    if not os.path.exists(STATE_FILE):
        return {"hosts": {}}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"⚠️  Could not read state file: {e}. Starting with empty state.")
        return {"hosts": {}}


def save_state(state):
    """Persist host state to the JSON state file."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ==========================================
# HELPERS
# ==========================================
def run_cmd(cmd):
    """Execute a local podman command (always with sudo)."""
    # Ensure sudo is prepended if not already
    if cmd[0] != "sudo":
        cmd = ["sudo"] + cmd
    return subprocess.run(cmd, capture_output=True, text=True)


def zabbix_api(method, params):
    """Execute a Zabbix JSON-RPC API call using the hardcoded token."""
    if ZABBIX_API_TOKEN == "YOUR_ZABBIX_API_TOKEN_HERE":
        print("❌ Error: Please update 'ZABBIX_API_TOKEN' at the top of the script.")
        sys.exit(1)

    headers = {"Content-Type": "application/json-rpc"}
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "auth": ZABBIX_API_TOKEN,
        "id": 1,
    }
    try:
        res = requests.post(ZABBIX_API_URL, json=payload, headers=headers, timeout=10)
        res_json = res.json()
        if "error" in res_json:
            print(f"❌ Zabbix API Error: {res_json['error']['data']}")
            return None
        return res_json.get("result")
    except Exception as e:
        print(f"❌ HTTP Error connecting to Zabbix API: {e}")
        return None


def index_key(idx):
    """Normalize an index to the zero-padded string key used in state."""
    return f"{int(idx):02d}"


def host_name(prefix, idx):
    """Build container/host name from prefix and index."""
    return f"{prefix}-{index_key(idx)}"


def zabbix_host_exists(name):
    """Check if a host by that name exists in Zabbix. Returns hostid or None."""
    hosts = zabbix_api(
        "host.get",
        {
            "output": ["hostid"],
            "filter": {"host": name},
        },
    )
    if hosts and len(hosts) > 0:
        return hosts[0]["hostid"]
    return None


def zabbix_delete_host(name):
    """Delete a single host from Zabbix by name. Returns True on success."""
    hostid = zabbix_host_exists(name)
    if not hostid:
        print(f"  ⚠️  Host '{name}' not found in Zabbix — skipping API delete.")
        return True  # not an error, already gone

    res = zabbix_api("host.delete", [hostid])
    if res and "hostids" in res:
        print(f"  🗑️  Deleted '{name}' from Zabbix (hostid: {hostid}).")
        return True
    else:
        print(f"  ❌ Failed to delete '{name}' from Zabbix.")
        return False


# ==========================================
# COMMAND: START HOSTS
# ==========================================
def start_hosts(count, prefix, server_ip):
    state = load_state()

    # Find the next available index
    existing = [int(k) for k in state["hosts"].keys()]
    next_idx = max(existing) + 1 if existing else 1

    print(f"🚀 Launching {count} container host(s) starting at index {index_key(next_idx)}...")

    for i in range(next_idx, next_idx + count):
        key = index_key(i)
        name = host_name(prefix, i)
        cmd = [
            "podman", "run",
            "--name", name,
            "-d",
            "-e", f"ZBX_HOSTNAME={name}",
            "-e", f"ZBX_SERVER_HOST={server_ip}",
            "-e", "ZBX_PASSIVE_ALLOW=false",
            "-e", "ZBX_ACTIVE_ALLOW=true",
            DEFAULT_IMAGE,
        ]
        res = run_cmd(cmd)
        if res.returncode == 0:
            container_id = res.stdout.strip()
            state["hosts"][key] = {
                "name": name,
                "container_id": container_id,
                "status": "running",
            }
            print(f"  ✅ Started {name} (container: {container_id[:12]})")
        else:
            print(f"  ⚠️  Could not start {name}: {res.stderr.strip()}")

    save_state(state)


# ==========================================
# COMMAND: STOP HOSTS
# ==========================================
def stop_hosts(indices=None, random_count=None):
    """
    Stop hosts.
    - indices: list of specific index keys to stop (e.g. ['01', '03'])
    - random_count: number of running hosts to randomly pick and stop
    - if both are None, stop ALL known hosts
    """
    state = load_state()

    if not state["hosts"]:
        print("📭 No hosts in state. Nothing to stop.")
        return

    # Resolve which hosts to stop
    if indices:
        targets = {k: v for k, v in state["hosts"].items() if k in indices and v["status"] == "running"}
        missing = [k for k in indices if k not in state["hosts"]]
        already_stopped = [k for k in indices if k in state["hosts"] and state["hosts"][k]["status"] != "running"]
        if missing:
            print(f"⚠️  Indices not in state: {', '.join(missing)}")
        if already_stopped:
            print(f"⚠️  Already stopped: {', '.join(already_stopped)}")
    elif random_count is not None:
        running = {k: v for k, v in state["hosts"].items() if v["status"] == "running"}
        if random_count > len(running):
            print(f"⚠️  Only {len(running)} running host(s) available (asked for {random_count}). Stopping all.")
            random_count = len(running)
        picked_keys = random.sample(list(running.keys()), random_count)
        targets = {k: running[k] for k in picked_keys}
    else:
        # Stop all running
        targets = {k: v for k, v in state["hosts"].items() if v["status"] == "running"}

    if not targets:
        print("✅ No running hosts to stop.")
        return

    print(f"💥 Stopping {len(targets)} host(s): {', '.join(v['name'] for v in targets.values())}")

    for key, host in targets.items():
        res = run_cmd(["podman", "stop", host["name"]])
        if res.returncode == 0:
            state["hosts"][key]["status"] = "stopped"
            print(f"  🛑 Stopped {host['name']}. Watch Zabbix to see the alert trigger!")
        else:
            print(f"  ❌ Could not stop {host['name']}: {res.stderr.strip()}")

    save_state(state)


# ==========================================
# COMMAND: REMOVE HOST (local container + Zabbix)
# ==========================================
def remove_hosts(indices):
    """Remove hosts: stop & delete container, delete from Zabbix, remove from state."""
    state = load_state()

    if not state["hosts"]:
        print("📭 No hosts in state. Nothing to remove.")
        return

    for idx in indices:
        key = index_key(idx)
        if key not in state["hosts"]:
            print(f"⚠️  Index '{key}' not in state — skipping.")
            continue

        host = state["hosts"][key]
        name = host["name"]
        print(f"🗑️  Removing {name}...")

        # Stop if running
        if host["status"] == "running":
            res = run_cmd(["podman", "stop", name])
            if res.returncode != 0:
                print(f"  ⚠️  Could not stop {name} (continuing anyway): {res.stderr.strip()}")

        # Remove container
        res = run_cmd(["podman", "rm", name])
        if res.returncode == 0:
            print(f"  🗑️  Removed container '{name}'.")
        else:
            print(f"  ⚠️  Could not remove container '{name}': {res.stderr.strip()}")

        # Delete from Zabbix
        zabbix_delete_host(name)

        # Remove from state
        del state["hosts"][key]
        print(f"  ✅ {name} fully removed.")

    save_state(state)


# ==========================================
# COMMAND: CLEANUP (stop all, remove from Zabbix, prune, clear state)
# ==========================================
def cleanup():
    """Full cleanup: stop all containers, remove from Zabbix, prune, clear state."""
    state = load_state()

    if not state["hosts"]:
        print("📭 No hosts in state. Running podman container prune anyway...")
    else:
        print(f"🧹 Cleaning up {len(state['hosts'])} host(s)...")

        for key, host in state["hosts"].items():
            name = host["name"]

            # Stop if running
            if host["status"] == "running":
                run_cmd(["podman", "stop", name])

            # Remove container
            run_cmd(["podman", "rm", name])

            # Delete from Zabbix
            zabbix_delete_host(name)

    # Prune all stopped containers
    res = run_cmd(["podman", "container", "prune", "-f"])
    print("🧹 Podman container prune completed.")
    if res.stdout.strip():
        print(res.stdout.strip())

    # Clear state
    save_state({"hosts": {}})
    print("✅ State cleared. All clean!")


# ==========================================
# MAIN CLI PARSER
# ==========================================
def main():
    parser = argparse.ArgumentParser(
        description="Zabbix Host Simulation Manager"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ---- start ----
    start_parser = subparsers.add_parser(
        "start", help="Spin up simulated agent containers"
    )
    start_parser.add_argument("count", type=int, help="Number of hosts to start")
    start_parser.add_argument("--prefix", default=DEFAULT_PREFIX, help="Host name prefix")
    start_parser.add_argument(
        "--server", default=DEFAULT_SERVER_IP, help="Zabbix server IP"
    )

    # ---- stop ----
    stop_parser = subparsers.add_parser(
        "stop", help="Stop containers to simulate host outage"
    )
    stop_parser.add_argument(
        "--index", action="append", dest="indices", metavar="N",
        help="Stop a specific host by index (can repeat: --index 01 --index 03)"
    )
    stop_parser.add_argument(
        "--random", type=int, metavar="N",
        help="Stop N randomly chosen running hosts"
    )

    # ---- remove ----
    remove_parser = subparsers.add_parser(
        "remove", help="Remove containers locally AND delete from Zabbix"
    )
    remove_parser.add_argument(
        "--index", required=True, action="append", dest="indices", metavar="N",
        help="Index of host to remove (can repeat: --index 01 --index 03)"
    )

    # ---- cleanup ----
    cleanup_parser = subparsers.add_parser(
        "cleanup", help="Stop all containers, remove from Zabbix, prune locally, clear state"
    )

    args = parser.parse_args()

    if args.command == "start":
        start_hosts(args.count, args.prefix, args.server)
    elif args.command == "stop":
        if args.indices and args.random:
            print("❌ Cannot use --index and --random together.")
            sys.exit(1)
        stop_hosts(indices=args.indices, random_count=args.random)
    elif args.command == "remove":
        remove_hosts(args.indices)
    elif args.command == "cleanup":
        cleanup()


if __name__ == "__main__":
    main()
