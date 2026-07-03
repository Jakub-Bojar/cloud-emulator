"""
Site topology: declarative validation of network_links, and the configured
inter-site metrics.

A template declares the network links between sites, e.g.

    "network_links": [
      { "from": "site-A", "to": "site-B", "rtt_ms": 30, "bandwidth_mbps": 1000 },
      { "from": "site-B", "to": "site-C", "rtt_ms": 10 }
    ]

The site names ("site-A", "site-B", …) match the `site=` node labels set with
  kubectl label node <node> site=site-A tier=origin

The controller applies these links via tc netem on the node NICs (netem.py).
This module handles:
  - validate_network_links  — parse and shape-check the field so a malformed
    declaration 400s at POST time rather than being silently ignored.
  - parse_network_links     — same parsing, returns a structured list netem.py
    can iterate without re-parsing the raw JSON.
  - set_configured_rtt / set_configured_bw — publish the declared values on
    /metrics so dashboards can show intent vs measured.

rtt_ms is the round-trip time; bandwidth_mbps is per-direction.
"""

from prometheus_client import Gauge

_MAX_RTT_MS = 10_000  # 10 s — beyond any plausible WAN emulation


# ── Configured inter-site metrics ─────────────────────────────────────────────

CONFIGURED_RTT = Gauge(
    "emulator_configured_rtt_ms",
    "Configured round-trip latency (ms) from the template's network_links. "
    "Compare against measured worker_peer_rtt_ms.",
    ["pair"])

CONFIGURED_BW = Gauge(
    "emulator_configured_bandwidth_mbps",
    "Configured link bandwidth cap (Mbps) from the template's network_links. "
    "Compare against measured worker_peer_egress_mbps.",
    ["pair"])


def set_configured_rtt(pairs: dict[str, float]) -> None:
    CONFIGURED_RTT.clear()
    for label, rtt_ms in pairs.items():
        CONFIGURED_RTT.labels(pair=label).set(rtt_ms)


def set_configured_bw(pairs: dict[str, float]) -> None:
    CONFIGURED_BW.clear()
    for label, mbps in pairs.items():
        CONFIGURED_BW.labels(pair=label).set(mbps)


# ── Validation + parsing ───────────────────────────────────────────────────────

def validate_network_links(
        links, valid_sites: set[str] | None = None,
) -> tuple[dict[str, float], dict[str, float]]:
    """template.network_links → (rtt_by_pair, bw_by_pair).

    Each entry: {"from": site_name, "to": site_name, "rtt_ms": number,
                 "bandwidth_mbps"?: number}

    site names are free-form strings — matched against the template's own
    `node_site_mapping` site names when `valid_sites` is given (materialiser.py
    passes this whenever the template declares a mapping), so a typo'd site
    name 400s here instead of netem.py silently skipping the link at
    materialise time.  Pass None (the default) to skip that cross-check —
    e.g. when the template only wants configured-metric publishing without
    node_site_mapping.  rtt_ms is the round-trip time (netem applies half on
    each side).  bandwidth_mbps is optional per-direction cap.

    Returned dicts are keyed by a deterministic "site-A ↔ site-B" label.
    Raises ValueError on bad shape, self-links, an unknown site (when
    valid_sites is given), or duplicate pairs."""
    parsed = _parse_network_links(links)
    rtt_by_pair: dict[str, float] = {}
    bw_by_pair:  dict[str, float] = {}
    for i, entry in enumerate(parsed):
        if valid_sites is not None:
            for key in ("from", "to"):
                if entry[key] not in valid_sites:
                    raise ValueError(
                        f"network_links[{i}].{key} {entry[key]!r} has no "
                        "matching entry in the template's node_site_mapping")
        label = entry["pair"]
        rtt_by_pair[label] = entry["rtt_ms"]
        if "bandwidth_mbps" in entry:
            bw_by_pair[label] = entry["bandwidth_mbps"]
    return rtt_by_pair, bw_by_pair


def parse_network_links(links) -> list[dict]:
    """Return a structured list for netem.py.

    Each element: {"from": str, "to": str, "rtt_ms": float,
                   "bandwidth_mbps"?: float, "pair": str}
    Returns [] when links is None or empty."""
    return _parse_network_links(links)


def _parse_network_links(links) -> list[dict]:
    if links is None:
        return []
    if not isinstance(links, list):
        raise ValueError(
            'network_links must be a list of '
            '{"from", "to", "rtt_ms", "bandwidth_mbps"?} objects')
    seen: set[tuple[str, str]] = set()
    result: list[dict] = []
    for i, link in enumerate(links):
        if not isinstance(link, dict):
            raise ValueError(f"network_links[{i}] must be an object")
        src = link.get("from")
        dst = link.get("to")
        if not isinstance(src, str) or not src:
            raise ValueError(f"network_links[{i}].from must be a non-empty string")
        if not isinstance(dst, str) or not dst:
            raise ValueError(f"network_links[{i}].to must be a non-empty string")
        if src == dst:
            raise ValueError(
                f"network_links[{i}]: a link cannot join a site to itself "
                f"({src!r})")
        key = (min(src, dst), max(src, dst))
        if key in seen:
            raise ValueError(
                f"a link between {src!r} and {dst!r} is declared more than "
                "once (links are symmetric — give each pair once)")
        seen.add(key)
        rtt = link.get("rtt_ms")
        if isinstance(rtt, bool) or not isinstance(rtt, (int, float)):
            raise ValueError(f"network_links[{i}].rtt_ms must be a number")
        if rtt <= 0:
            raise ValueError(f"network_links[{i}].rtt_ms must be > 0")
        if rtt > _MAX_RTT_MS:
            raise ValueError(
                f"network_links[{i}].rtt_ms {rtt} exceeds the 10 s cap")
        a, b = sorted((src, dst))
        entry: dict = {
            "from": src, "to": dst,
            "rtt_ms": float(rtt),
            "pair": f"{a} ↔ {b}",
        }
        bw = link.get("bandwidth_mbps")
        if bw is not None:
            if isinstance(bw, bool) or not isinstance(bw, (int, float)) or bw <= 0:
                raise ValueError(
                    f"network_links[{i}].bandwidth_mbps must be a positive number")
            entry["bandwidth_mbps"] = float(bw)
        result.append(entry)
    return result
