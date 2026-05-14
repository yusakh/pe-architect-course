#!/usr/bin/env python3
"""
Teams CLI - A simple command-line interface for the Teams API
"""

import argparse
import json
import os
import re
import sys
import requests
from typing import Optional

API_BASE_URL = os.environ.get("TEAMS_API_URL", "http://0416-yusakh-arch.coder:3002")


def team_namespace(team_name: str) -> str:
    """Derive the Kubernetes namespace for a team name (mirrors operator logic)."""
    ns = re.sub(r'[^a-z0-9]', '-', team_name.lower())
    ns = re.sub(r'-+', '-', ns).strip('-')
    return f"team-{ns}"[:63]


class TeamsAPI:
    def __init__(self, base_url: str = API_BASE_URL, token: Optional[str] = None):
        self.base_url = base_url
        self.token = token

    def _make_request(self, method: str, endpoint: str, data: Optional[dict] = None) -> Optional[dict]:
        """Make HTTP request to the API"""
        url = f"{self.base_url}{endpoint}"
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            if method == "GET":
                response = requests.get(url, headers=headers)
            elif method == "POST":
                response = requests.post(url, json=data, headers=headers)
            elif method == "DELETE":
                response = requests.delete(url, headers=headers)
            else:
                raise ValueError(f"Unsupported method: {method}")

            response.raise_for_status()
            return response.json() if response.content else None
        except requests.exceptions.ConnectionError:
            print(f"❌ Error: Could not connect to API at {self.base_url}")
            print("   Make sure the Teams API is running")
            sys.exit(1)
        except requests.exceptions.HTTPError as e:
            if response.status_code == 400:
                error_detail = response.json().get("detail", "Bad request")
                print(f"❌ Error: {error_detail}")
            elif response.status_code == 401:
                print("❌ Error: Unauthorized — set TEAMS_API_TOKEN env var or use --token")
            elif response.status_code == 404:
                print(f"❌ Error: Resource not found")
            elif response.status_code == 409:
                print(f"❌ Error: {response.json().get('detail', 'Already exists')}")
            else:
                print(f"❌ HTTP Error {response.status_code}: {e}")
            sys.exit(1)
        except Exception as e:
            print(f"❌ Unexpected error: {e}")
            sys.exit(1)

    # --- Teams ---

    def health_check(self):
        result = self._make_request("GET", "/health")
        print(f"✅ API Status: {result.get('status', 'unknown')}")
        print(f"📊 Teams Count: {result.get('teams_count', 0)}")

    def create_team(self, name: str):
        result = self._make_request("POST", "/teams", {"name": name})
        print(f"✅ Created team: {result['name']}")
        print(f"🆔 Team ID:      {result['id']}")
        print(f"📅 Created:      {result['created_at']}")
        print(f"📦 Namespace:    {team_namespace(result['name'])}")

    def list_teams(self):
        teams = self._make_request("GET", "/teams")
        if not teams:
            print("📭 No teams found")
            return
        print(f"📋 Found {len(teams)} team(s):")
        print("-" * 60)
        for team in teams:
            print(f"🏷️  Name:      {team['name']}")
            print(f"🆔 ID:        {team['id']}")
            print(f"📅 Created:   {team['created_at']}")
            print(f"📦 Namespace: {team_namespace(team['name'])}")
            print("-" * 60)

    def get_team(self, team_id: str):
        team = self._make_request("GET", f"/teams/{team_id}")
        print(f"🏷️  Name:      {team['name']}")
        print(f"🆔 ID:        {team['id']}")
        print(f"📅 Created:   {team['created_at']}")
        print(f"📦 Namespace: {team_namespace(team['name'])}")

    def delete_team(self, team_id: str):
        result = self._make_request("DELETE", f"/teams/{team_id}")
        print(f"✅ {result['message']}")

    # --- Rollout templates ---

    def rollout_templates_list(self):
        templates = self._make_request("GET", "/rollout/templates")
        if not templates:
            print("📭 No templates available")
            return
        print(f"📋 Found {len(templates)} template(s):")
        print("-" * 60)
        for t in templates:
            print(f"📄 Name:      {t['name']}")
            print(f"🐳 Image:     {t['image']}")
            print(f"🔀 Strategy:  {t['strategy']}")
            print(f"🔢 Replicas:  {t['replicas']}")
            print("-" * 60)

    # --- Rollout deployments ---

    def rollout_deployments_list(self, namespace: str):
        deployments = self._make_request("GET", f"/rollout/namespaces/{namespace}/deployments")
        if not deployments:
            print(f"📭 No deployments in namespace '{namespace}'")
            return
        print(f"📋 Found {len(deployments)} deployment(s) in '{namespace}':")
        print("-" * 60)
        for d in deployments:
            status = d.get("status", {})
            print(f"🚀 Name:      {d['name']}")
            print(f"📄 Template:  {d['templateRef']}")
            print(f"📊 Phase:     {status.get('phase', 'Unknown')}")
            if status.get("rolloutName"):
                print(f"🔁 Rollout:   {status['rolloutName']}")
            if status.get("message"):
                print(f"💬 Message:   {status['message']}")
            print("-" * 60)

    def rollout_deployments_create(self, namespace: str, name: str, template_ref: str, replicas: Optional[int] = None):
        body: dict = {"templateRef": template_ref}
        if replicas is not None:
            body["replicas"] = replicas
        result = self._make_request("POST", f"/rollout/namespaces/{namespace}/deployments/{name}", body)
        print(f"✅ Deployment '{result['name']}' created in namespace '{result['namespace']}'")
        print(f"📄 Template:  {result['templateRef']}")
        print(f"📊 Phase:     {result['status'].get('phase', 'Pending')}")

    def rollout_deployments_delete(self, namespace: str, name: str):
        self._make_request("DELETE", f"/rollout/namespaces/{namespace}/deployments/{name}")
        print(f"✅ Deleted deployment '{name}' from namespace '{namespace}'")


def main():
    script_name=os.path.basename(__file__)
    parser = argparse.ArgumentParser(
        description="Teams CLI - Manage teams and rollout deployments via the Teams API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Examples:
  ./{script_name} health
  ./{script_name} create "Backend Team"
  ./{script_name} list
  ./{script_name} get <team-id>
  ./{script_name} delete <team-id>
  ./{script_name} namespace "Backend Team"          # show k8s namespace for a team name

  ./{script_name} rollout templates list
  ./{script_name} rollout deployments list team-backend-team
  ./{script_name} rollout deployments create team-backend-team my-app --template argo-demo
  ./{script_name} rollout deployments delete team-backend-team my-app
        """
    )

    parser.add_argument("--url", default=API_BASE_URL,
                        help=f"API base URL (default: TEAMS_API_URL env or {API_BASE_URL})")
    parser.add_argument("--token", default=os.environ.get("TEAMS_API_TOKEN"),
                        help="Bearer token for API auth (default: TEAMS_API_TOKEN env var)")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    subparsers.add_parser("health", help="Check API health")

    create_p = subparsers.add_parser("create", help="Create a new team")
    create_p.add_argument("name", help="Team name")

    subparsers.add_parser("list", help="List all teams")

    get_p = subparsers.add_parser("get", help="Get a specific team")
    get_p.add_argument("team_id", help="Team ID")

    delete_p = subparsers.add_parser("delete", help="Delete a team")
    delete_p.add_argument("team_id", help="Team ID")

    ns_p = subparsers.add_parser("namespace", help="Show the k8s namespace for a team name")
    ns_p.add_argument("team_name", help="Team name")

    # rollout subcommand group
    rollout_p = subparsers.add_parser("rollout", help="Manage rollout templates and deployments")
    rollout_sub = rollout_p.add_subparsers(dest="rollout_resource", help="Resource type")

    # rollout templates
    tpl_p = rollout_sub.add_parser("templates", help="Manage rollout templates")
    tpl_sub = tpl_p.add_subparsers(dest="rollout_action", help="Action")
    tpl_sub.add_parser("list", help="List available templates")

    # rollout deployments
    dep_p = rollout_sub.add_parser("deployments", help="Manage rollout deployments")
    dep_sub = dep_p.add_subparsers(dest="rollout_action", help="Action")

    dep_list_p = dep_sub.add_parser("list", help="List deployments in a namespace")
    dep_list_p.add_argument("namespace", help="Kubernetes namespace (e.g. team-backend-team)")

    dep_create_p = dep_sub.add_parser("create", help="Create a deployment from a template")
    dep_create_p.add_argument("namespace", help="Kubernetes namespace")
    dep_create_p.add_argument("name", help="Deployment name")
    dep_create_p.add_argument("--template", required=True, help="RolloutTemplate name")
    dep_create_p.add_argument("--replicas", type=int, help="Override replica count")

    dep_delete_p = dep_sub.add_parser("delete", help="Delete a deployment")
    dep_delete_p.add_argument("namespace", help="Kubernetes namespace")
    dep_delete_p.add_argument("name", help="Deployment name")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    api = TeamsAPI(args.url, token=args.token)

    try:
        if args.command == "health":
            api.health_check()
        elif args.command == "create":
            api.create_team(args.name)
        elif args.command == "list":
            api.list_teams()
        elif args.command == "get":
            api.get_team(args.team_id)
        elif args.command == "delete":
            api.delete_team(args.team_id)
        elif args.command == "namespace":
            print(team_namespace(args.team_name))
        elif args.command == "rollout":
            if not args.rollout_resource:
                rollout_p.print_help()
            elif args.rollout_resource == "templates":
                if args.rollout_action == "list":
                    api.rollout_templates_list()
                else:
                    tpl_p.print_help()
            elif args.rollout_resource == "deployments":
                if args.rollout_action == "list":
                    api.rollout_deployments_list(args.namespace)
                elif args.rollout_action == "create":
                    api.rollout_deployments_create(args.namespace, args.name, args.template, args.replicas)
                elif args.rollout_action == "delete":
                    api.rollout_deployments_delete(args.namespace, args.name)
                else:
                    dep_p.print_help()
    except KeyboardInterrupt:
        print("\n👋 Goodbye!")
        sys.exit(0)


if __name__ == "__main__":
    main()
