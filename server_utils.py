import math
from datetime import datetime, timezone
from dateutil import parser


def server_is_online(server, threshold_seconds=10):
    last_active_str = server.get("last_active")
    if not last_active_str:
        return False
    try:
        last_active_dt = parser.isoparse(last_active_str).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return False
    return (datetime.now(timezone.utc) - last_active_dt).total_seconds() < threshold_seconds


def get_server_group_ids(server):
    groups = server.get("groups") or server.get("group_ids") or []
    if isinstance(groups, (str, int)):
        groups = [groups]
    return {int(group_id) for group_id in groups if str(group_id).isdigit()}


def sort_servers_for_display(servers, online_func=server_is_online):
    return sorted(
        servers,
        key=lambda server: (not online_func(server), str(server.get("name", "")).lower()),
    )


def filter_servers(
    servers,
    query=None,
    status="all",
    group_id=None,
    high_load=False,
    high_traffic=False,
    high_load_threshold=80,
    high_traffic_threshold_bytes=10 * 1024**3,
):
    query = (query or "").strip().lower()
    out = []
    for server in servers:
        name = str(server.get("name", ""))
        if query and query not in name.lower():
            continue
        online = server_is_online(server)
        if status == "online" and not online:
            continue
        if status == "offline" and online:
            continue
        if group_id is not None and int(group_id) not in get_server_group_ids(server):
            continue
        state = server.get("state") or {}
        if high_load and float(state.get("cpu") or 0) < high_load_threshold:
            continue
        traffic = int(state.get("net_in_transfer") or 0) + int(
            state.get("net_out_transfer") or 0
        )
        if high_traffic and traffic < high_traffic_threshold_bytes:
            continue
        out.append(server)
    return sort_servers_for_display(out)


def paginate_servers(servers, page=1, per_page=8):
    total = len(servers)
    total_pages = max(1, math.ceil(total / per_page))
    page = min(max(1, int(page or 1)), total_pages)
    start = (page - 1) * per_page
    return {
        "items": list(servers)[start : start + per_page],
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    }


def attach_group_ids(servers, groups):
    mapping = {}
    for group_item in groups or []:
        group = group_item.get("group") or {}
        group_id = group.get("id") or group_item.get("id")
        for server_id in group_item.get("servers") or []:
            mapping.setdefault(int(server_id), set()).add(int(group_id))
    for server in servers:
        server["groups"] = sorted(mapping.get(int(server.get("id", 0)), set()))
    return servers


def compact_server_status(server):
    state = server.get("state") or {}
    online = server_is_online(server)
    cpu = float(state.get("cpu") or 0)
    traffic = int(state.get("net_in_transfer") or 0) + int(
        state.get("net_out_transfer") or 0
    )
    uptime = int(state.get("uptime") or 0)
    status = "在线" if online else "离线"
    return f"{status} CPU {cpu:.0f}% 流量 {format_bytes(traffic)} 运行 {uptime // 86400}天"


def format_bytes(size_in_bytes):
    if not size_in_bytes:
        return "0B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    power = min(int(math.floor(math.log(size_in_bytes, 1024))), len(units) - 1)
    return f"{size_in_bytes / (1024 ** power):.2f}{units[power]}"
