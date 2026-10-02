"""
Link-level inter-site shaping (latency + bandwidth) with tc on the nodes.

For each network_link declared in the template, the controller resolves
`from`/`to` site names through the template's own `node_site_mapping` (a
{k8s_node, site_name} table declared right in the template — no node
labelling needed), gets each named node's InternalIP, and applies tc htb +
netem rules on its physical NIC so traffic toward the peer node is delayed
and (optionally) rate-limited.

Why node-NIC shaping beats per-pod (the old Chaos Mesh approach):
  Latency and bandwidth are properties of the inter-NODE link, not of pods.
  With Calico VXLAN-always, all inter-site pod traffic is encapsulated between
  node InternalIPs and egresses the node's physical NIC.  Shaping the NIC once,
  matched by peer-node IP, means every pod on that node inherits the rules
  automatically — adding or removing pods never disturbs the tc config and there
  is no per-pod IP to re-resolve.

rtt_ms is the round-trip time.  netem applies rtt_ms/2 as one-way delay on
each side so the sum equals rtt_ms.  bandwidth_mbps is per-direction.

The controller reaches each node via `multipass exec` (k8s node name ==
Multipass VM name).  If multipass is unavailable (e.g. controller running
inside a cloud cluster) shaping is skipped and a warning is logged.

Never raises — shaping is auxiliary to materialising the template itself.
But it does report: apply() returns which links are actually in effect and
why the others aren't, keeps that as last_result() for /overview, and sets
emulator_link_shaping_applied{pair} so dashboards don't take the configured
figures for what the network is doing.
"""

import json
import logging
import os
import subprocess

from prometheus_client import Gauge

import k8s
import linkspec

log = logging.getLogger(__name__)

MULTIPASS      = os.environ.get("MULTIPASS_BIN", "multipass")
UNSHAPED_RATE  = "10gbit"
EXEC_TIMEOUT_S = 30

LINK_APPLIED = Gauge(
    "emulator_link_shaping_applied",
    "1 if the template's network_link for this pair is shaped with tc as of "
    "the last materialise, 0 if shaping was skipped or failed. Read "
    "emulator_configured_* as intent only where this is 0.",
    ["pair"])

# Outcome of the most recent apply()/teardown(), for GET /overview. Not
# persisted: after a controller restart nothing is known until the next
# materialise re-applies the links.
_UNKNOWN = {"status": "unknown",
            "detail": "no materialise since the controller started"}
_last_result: dict = dict(_UNKNOWN)


def last_result() -> dict:
    return dict(_last_result)


def _record(links: dict[str, str | None]) -> dict:
    """Summarise per-link outcomes ({pair: None if applied, else the
    reason}), publish them on LINK_APPLIED, and remember them."""
    global _last_result
    LINK_APPLIED.clear()
    for pair, reason in links.items():
        LINK_APPLIED.labels(pair=pair).set(0 if reason else 1)
    applied = sum(1 for r in links.values() if r is None)
    if not links:
        status = "none"
    elif applied == len(links):
        status = "applied"
    elif applied:
        status = "partial"
    else:
        status = "not_applied"
    _last_result = {
        "status": status,
        "links": {pair: ({"applied": True} if reason is None
                         else {"applied": False, "reason": reason})
                  for pair, reason in links.items()},
    }
    return last_result()


# ── Node discovery ─────────────────────────────────────────────────────────────

def _resolve_sites(node_site_mapping) -> dict[str, tuple[str, str]]:
    """{site_name: (node_name, internal_ip)}, resolved from the template's own
    node_site_mapping ([{"k8s_node", "site_name"}, ...]) against live k8s
    nodes — no node labels involved.

    Lists all nodes once and indexes by name; a k8s_node named in the mapping
    that doesn't exist as a live node is skipped with a warning (rather than
    failing the whole resolution) since materialiser.validate() only checks
    the template's own shape, not live cluster state."""
    if not node_site_mapping:
        return {}
    status, body = k8s.get("/api/v1/nodes")
    if status != 200:
        log.warning("netem: listing nodes returned %s; skipping shaping", status)
        return {}
    ip_by_node: dict[str, str] = {}
    for n in json.loads(body).get("items", []):
        name = n.get("metadata", {}).get("name")
        ip = next((a["address"]
                   for a in n.get("status", {}).get("addresses", [])
                   if a.get("type") == "InternalIP"), None)
        if name and ip:
            ip_by_node[name] = ip
    out: dict[str, tuple[str, str]] = {}
    for entry in node_site_mapping:
        node, site = entry.get("k8s_node"), entry.get("site_name")
        ip = ip_by_node.get(node)
        if ip is None:
            log.warning("netem: node_site_mapping names k8s_node %r (site %r) "
                        "but no such node is live; skipping", node, site)
            continue
        out[site] = (node, ip)
    return out


# ── tc script builders ─────────────────────────────────────────────────────────

def _vm_sh(vm: str, script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [MULTIPASS, "exec", vm, "--", "sudo", "sh", "-c", script],
        capture_output=True, text=True, timeout=EXEC_TIMEOUT_S)


def _tc_rate(mbps: float) -> str:
    """Convert Mbps float to a tc bit-unit string."""
    gbps = mbps / 1000.0
    if gbps >= 1 and gbps == int(gbps):
        return f"{int(gbps)}gbit"
    if mbps == int(mbps):
        return f"{int(mbps)}mbit"
    return f"{mbps:.3g}mbit"


def _iface_line(node_ip: str) -> str:
    """Shell fragment that sets $IFACE to the NIC carrying node_ip."""
    return (f"IFACE=$(ip -o -4 addr show | grep -F '{node_ip}/' "
            "| awk '{print $2}' | head -1)")


def _clear_script(node_ip: str) -> str:
    return (f"{_iface_line(node_ip)}; "
            '[ -n "$IFACE" ] && tc qdisc del dev "$IFACE" root 2>/dev/null; true')


def _shape_script(node_ip: str,
                  peers: list[tuple[str, float, float | None]]) -> str:
    """Build the tc script for one node.

    peers is a list of (peer_ip, one_way_delay_ms, bandwidth_mbps_or_None).
    Installs an htb root with an unshaped default class (line-rate traffic for
    everything not explicitly matched), plus one htb class + optional netem
    child + u32 filter per peer node IP."""
    lines = [
        _iface_line(node_ip),
        '[ -z "$IFACE" ] && { echo "netem: no NIC found" >&2; exit 1; }',
        'tc qdisc del dev "$IFACE" root 2>/dev/null || true',
        'tc qdisc add dev "$IFACE" root handle 1: htb default 9999',
        f'tc class add dev "$IFACE" parent 1: classid 1:9999 htb rate {UNSHAPED_RATE}',
    ]
    for idx, (peer_ip, delay_ms, bw_mbps) in enumerate(peers):
        cid = 10 + idx
        rate = _tc_rate(bw_mbps) if bw_mbps else UNSHAPED_RATE
        lines.append(
            f'tc class add dev "$IFACE" parent 1: classid 1:{cid} '
            f'htb rate {rate} ceil {rate}')
        if delay_ms > 0:
            lines.append(
                f'tc qdisc add dev "$IFACE" parent 1:{cid} '
                f'handle {cid}0: netem delay {delay_ms:g}ms')
        lines.append(
            f'tc filter add dev "$IFACE" parent 1: protocol ip '
            f'prio 1 u32 match ip dst {peer_ip}/32 flowid 1:{cid}')
    return "\n".join(lines)


def _run_script(node_name: str, script: str) -> str | None:
    """Run script on node_name via multipass. Returns None on success, else
    why it failed."""
    try:
        r = _vm_sh(node_name, script)
    except FileNotFoundError:
        log.warning("netem: multipass not found — install it or set "
                    "MULTIPASS_BIN; skipping link shaping")
        return (f"{MULTIPASS!r} not found where the controller runs, so tc "
                "can't reach the nodes (see RUNBOOK 'Known limitations')")
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("netem: exec on %s failed: %s", node_name, exc)
        return f"exec on node {node_name!r} failed: {exc}"
    if r.returncode != 0:
        err = (r.stderr or "").strip()[:300]
        log.warning("netem: tc on %s failed (rc=%s): %s",
                    node_name, r.returncode, err)
        return f"tc on node {node_name!r} failed (rc={r.returncode}): {err}"
    return None


# ── Public API ─────────────────────────────────────────────────────────────────

def apply(template: dict) -> dict:
    """Reconcile inter-site link shaping from the template's network_links.

    Never raises. Returns {"status", "links"}: status is "none" (no links
    declared), "applied", "partial" or "not_applied"; links maps each pair
    label to {"applied": bool, "reason"?: str}."""
    try:
        return _record(_apply(template))
    except Exception as exc:
        log.exception("netem: apply failed; continuing without link shaping")
        try:
            pairs = [l["pair"] for l in
                     linkspec.parse_network_links(template.get("network_links"))]
        except ValueError:
            pairs = []
        return _record({p: f"shaping raised {type(exc).__name__}: {exc}"
                        for p in pairs})


def _apply(template: dict) -> dict[str, str | None]:
    """Shape every declared link; returns {pair: None | reason skipped}."""
    links = linkspec.parse_network_links(template.get("network_links"))
    nodes = _resolve_sites(template.get("node_site_mapping"))
    if not nodes:
        reason = ("template has no node_site_mapping"
                  if not template.get("node_site_mapping")
                  else "none of the node_site_mapping nodes could be resolved")
        return {link["pair"]: reason for link in links}

    if not links:
        # No links declared — clear any rules left from a previous template.
        _teardown_nodes(nodes)
        return {}

    # Collect per-node peer specs so each node gets ONE comprehensive tc script
    # covering all its links (the htb root can only be added once per NIC).
    # peers_for[node_name] = [(peer_ip, one_way_ms, bw_mbps_or_None), ...]
    peers_for: dict[str, list[tuple[str, float, float | None]]] = {}
    node_ip_by_name: dict[str, str] = {name: ip for (name, ip) in nodes.values()}
    outcome: dict[str, str | None] = {}
    nodes_of: dict[str, tuple[str, str]] = {}

    for link in links:
        from_node = nodes.get(link["from"])
        to_node   = nodes.get(link["to"])
        missing = link["from"] if not from_node else (
            link["to"] if not to_node else None)
        if missing:
            log.warning("netem: site %r has no live node (see "
                        "node_site_mapping); skipping link %s→%s",
                        missing, link["from"], link["to"])
            outcome[link["pair"]] = f"site {missing!r} has no live node"
            continue
        from_name, from_ip = from_node
        to_name,   to_ip   = to_node
        delay_ms = link["rtt_ms"] / 2.0
        bw_mbps  = link.get("bandwidth_mbps")
        peers_for.setdefault(from_name, []).append((to_ip,   delay_ms, bw_mbps))
        peers_for.setdefault(to_name,   []).append((from_ip, delay_ms, bw_mbps))
        nodes_of[link["pair"]] = (from_name, to_name)

    # A link is in effect only if the scripts on both of its ends succeeded.
    failed: dict[str, str] = {}
    for node_name, peers in peers_for.items():
        node_ip = node_ip_by_name[node_name]
        script  = _shape_script(node_ip, peers)
        err = _run_script(node_name, script)
        if err is None:
            log.info("netem: shaped %s — %d link(s) (%.1f ms one-way on first)",
                     node_name, len(peers), peers[0][1])
        else:
            failed[node_name] = err
    for pair, ends in nodes_of.items():
        outcome[pair] = next((failed[n] for n in ends if n in failed), None)
    return outcome


def teardown(node_site_mapping=None) -> None:
    """Remove link shaping from every node named in node_site_mapping.

    Callers must pass the mapping from the template that was materialised
    (fetched before its ConfigMaps are deleted, since that's where it's
    stored) — teardown has no other way to know which nodes to clear.
    Never raises."""
    try:
        nodes = _resolve_sites(node_site_mapping)
        if nodes:
            _teardown_nodes(nodes)
    except Exception:
        log.exception("netem: teardown failed; continuing")
    _record({})


def _teardown_nodes(nodes: dict[str, tuple[str, str]]) -> None:
    for site, (node_name, node_ip) in nodes.items():
        if _run_script(node_name, _clear_script(node_ip)) is None:
            log.info("netem: cleared shaping on %s (site=%s)", node_name, site)
