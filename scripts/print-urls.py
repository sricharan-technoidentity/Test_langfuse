#!/usr/bin/env python3
"""Print Langfuse URLs for YOUR host and optional project id. Never reads secret keys."""
from __future__ import annotations

import argparse
import os
from urllib.parse import urlparse


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-url",
        default=os.environ.get("LANGFUSE_BASE_URL", "http://127.0.0.1:3000"),
        help="Your Langfuse origin (default loopback). Not a secret.",
    )
    parser.add_argument(
        "--project-id",
        default=os.environ.get("LANGFUSE_PROJECT_ID", ""),
        help="Your Langfuse project id from the UI. Dynamic — this repo has no default project.",
    )
    parser.add_argument(
        "--dashboard-id",
        default="",
        help="Optional dashboard id from provisioner JSON output",
    )
    parser.add_argument(
        "--trace-id",
        default="",
        help="Optional 32-hex step trace id from finalize JSON",
    )
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    parsed = urlparse(base)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        print(f"note host={parsed.hostname} is not loopback; this repo documents local bind only")

    print(f"ui={base}")
    if args.project_id:
        pid = args.project_id
        print(f"project={base}/project/{pid}")
        print(f"dashboards={base}/project/{pid}/dashboards")
        print(f"traces={base}/project/{pid}/traces")
        print(f"scores={base}/project/{pid}/scores")
        if args.dashboard_id:
            print(f"dashboard={base}/project/{pid}/dashboards/{args.dashboard_id}")
        if args.trace_id:
            print(f"trace={base}/project/{pid}/traces/{args.trace_id}")
    else:
        print("note pass --project-id YOUR_PROJECT_ID (from Langfuse project settings) to expand paths")
        print("note this repo does not ship a project id")


if __name__ == "__main__":
    main()
