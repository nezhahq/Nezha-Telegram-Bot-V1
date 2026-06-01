import unittest
from datetime import datetime, timedelta, timezone

from server_utils import filter_servers, paginate_servers, sort_servers_for_display


def server(name, online=True, cpu=1, net=0, groups=None):
    return {
        "id": len(name),
        "name": name,
        "last_active": (
            datetime.now(timezone.utc) - timedelta(seconds=2 if online else 90)
        ).isoformat(),
        "state": {
            "cpu": cpu,
            "net_in_transfer": net,
            "net_out_transfer": net,
            "uptime": 3600,
        },
        "host": {},
        "groups": groups or [],
    }


class ServerUtilsTests(unittest.TestCase):
    def test_sort_online_first_then_name(self):
        servers = [server("z-offline", False), server("b-online"), server("a-online")]
        names = [s["name"] for s in sort_servers_for_display(servers)]
        self.assertEqual(names, ["a-online", "b-online", "z-offline"])

    def test_filter_by_query_status_group_and_load(self):
        servers = [
            server("hk-web-1", True, cpu=93, groups=[2]),
            server("hk-db-1", False, cpu=5, groups=[2]),
            server("us-web-1", True, cpu=10, groups=[3]),
        ]

        self.assertEqual(len(filter_servers(servers, query="hk")), 2)
        self.assertEqual(len(filter_servers(servers, status="online")), 2)
        self.assertEqual(len(filter_servers(servers, group_id=2)), 2)
        self.assertEqual(len(filter_servers(servers, high_load=True)), 1)

    def test_paginate_servers_clamps_page_and_reports_total(self):
        servers = [server(f"s{i}") for i in range(23)]
        page = paginate_servers(servers, page=99, per_page=10)

        self.assertEqual(page["page"], 3)
        self.assertEqual(page["total_pages"], 3)
        self.assertEqual(len(page["items"]), 3)


if __name__ == "__main__":
    unittest.main()
