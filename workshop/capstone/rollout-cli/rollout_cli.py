#!/usr/bin/env python3
"""
rollout-cli — CLI for the teams-api /rollout endpoints
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error

API_URL = os.getenv("TEAMS_API_URL", "http://localhost:4200")


def _request(method: str, path: str, body: dict | None = None) -> dict | list:
    url = f"{API_URL}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"Error {e.code}: {e.read().decode()}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Connection error: {e.reason}", file=sys.stderr)
        sys.exit(1)


def cmd_templates_list(_):
    templates = _request("GET", "/rollout/templates")
    if not templates:
        print("No templates available.")
        return
    for t in templates:
        print(f"  {t['name']:<20} image={t['image']}  strategy={t['strategy']}")


def cmd_deployments_list(args):
    deployments = _request("GET", f"/rollout/namespaces/{args.namespace}/deployments")
    if not deployments:
        print(f"No deployments in namespace '{args.namespace}'.")
        return
    for d in deployments:
        phase = d.get("status", {}).get("phase", "Unknown")
        print(f"  {d['name']:<30} template={d['templateRef']:<20} phase={phase}")


def cmd_deployments_create(args):
    body = {"templateRef": args.template}
    if args.replicas:
        body["replicas"] = args.replicas
    result = _request("POST", f"/rollout/namespaces/{args.namespace}/deployments/{args.name}", body)
    print(f"Created deployment '{result['name']}' in namespace '{args.namespace}' (phase: {result['status']['phase']})")


def cmd_deployments_delete(args):
    _request("DELETE", f"/rollout/namespaces/{args.namespace}/deployments/{args.name}")
    print(f"Deleted deployment '{args.name}' from namespace '{args.namespace}'")


def main():
    parser = argparse.ArgumentParser(prog="rollout-cli")
    sub = parser.add_subparsers(dest="group", required=True)

    # templates
    t = sub.add_parser("templates")
    ts = t.add_subparsers(dest="command", required=True)
    ts.add_parser("list").set_defaults(func=cmd_templates_list)

    # deployments
    d = sub.add_parser("deployments")
    ds = d.add_subparsers(dest="command", required=True)

    dl = ds.add_parser("list")
    dl.add_argument("namespace")
    dl.set_defaults(func=cmd_deployments_list)

    dc = ds.add_parser("create")
    dc.add_argument("namespace")
    dc.add_argument("name")
    dc.add_argument("--template", required=True)
    dc.add_argument("--replicas", type=int)
    dc.set_defaults(func=cmd_deployments_create)

    dd = ds.add_parser("delete")
    dd.add_argument("namespace")
    dd.add_argument("name")
    dd.set_defaults(func=cmd_deployments_delete)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
