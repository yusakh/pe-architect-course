#!/usr/bin/env python3
"""
Teams Operator - Creates Kubernetes namespaces when teams are created in the Teams API
"""

import asyncio
import json
import logging
import os
import time
from typing import Any
import aiohttp
from kubernetes import client, config
from kubernetes.client.rest import ApiException

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('teams-operator')

CONSTRAINT_GROUP   = "constraints.gatekeeper.sh"
CONSTRAINT_VERSION = "v1beta1"

# FalcoRootPrevention — blocks containers running as root
FALCO_CONSTRAINT_PLURAL = "falcorootprevention"
FALCO_CONSTRAINT_NAME   = "enforce-falco-root-prevention"

# RequireArgoRollout — blocks plain Deployments, requires Argo Rollouts
ROLLOUT_CONSTRAINT_PLURAL = "requireargorollout"
ROLLOUT_CONSTRAINT_NAME   = "enforce-argo-rollout"

class TeamsOperator:
    def __init__(self):
        self.teams_api_url = os.getenv('TEAMS_API_URL', 'http://teams-api-service:80')
        self.poll_interval = int(os.getenv('POLL_INTERVAL', '30'))  # seconds
        self.api_token = os.getenv('TEAMS_API_TOKEN')
        
        # Initialize Kubernetes client
        try:
            # Try in-cluster config first (when running in pod)
            config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes config")
        except config.ConfigException:
            # Fall back to local kubeconfig (for development)
            config.load_kube_config()
            logger.info("Loaded local kubeconfig")
        
        self.k8s_core_v1 = client.CoreV1Api()
        self.k8s_custom = client.CustomObjectsApi()

    def sanitize_namespace_name(self, team_name: str) -> str:
        """Convert team name to valid Kubernetes namespace name"""
        # Lowercase, replace spaces/special chars with hyphens, remove consecutive hyphens
        namespace = team_name.lower()
        namespace = ''.join(c if c.isalnum() else '-' for c in namespace)
        namespace = '-'.join(filter(None, namespace.split('-')))  # Remove consecutive hyphens
        
        # Ensure it starts and ends with alphanumeric
        namespace = namespace.strip('-')
        
        # Add prefix to avoid conflicts
        namespace = f"team-{namespace}"

        # Kubernetes namespace names must be <= 63 characters
        if len(namespace) > 63:
            namespace = namespace[:63].rstrip('-')
        
        return namespace
    
    async def fetch_teams(self) -> list:
        """Fetch current teams from the Teams API"""
        headers = {}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.teams_api_url}/teams", headers=headers) as response:
                    if response.status == 200:
                        teams = await response.json()
                        logger.debug(f"Fetched {len(teams)} teams from API")
                        return teams
                    else:
                        logger.error(f"Failed to fetch teams: HTTP {response.status}")
                        return []
        except aiohttp.ClientError as e:
            logger.error(f"Error connecting to Teams API: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error fetching teams: {e}")
            return []
    
    def namespace_exists(self, namespace_name: str) -> bool:
        try:
            self.k8s_core_v1.read_namespace(name=namespace_name)
            return True
        except ApiException as e:
            if e.status == 404:
                return False
            raise

    def create_namespace(self, team_id: str, team_name: str, namespace_name: str) -> bool:
        """Create a Kubernetes namespace for the team"""
        try:
            # Define namespace metadata
            namespace_body = client.V1Namespace(
                metadata=client.V1ObjectMeta(
                    name=namespace_name,
                    labels={
                        "app.kubernetes.io/managed-by": "teams-operator",
                        "teams.example.com/team-id": team_id,
                        "teams.example.com/team-name": team_name.replace(" ", "-").lower()
                    },
                    annotations={
                        "teams.example.com/original-team-name": team_name,
                        "teams.example.com/created-by": "teams-operator",
                        "teams.example.com/team-id": team_id
                    }
                )
            )
            
            # Create the namespace
            self.k8s_core_v1.create_namespace(body=namespace_body)
            logger.info(f"✅ Created namespace '{namespace_name}' for team '{team_name}' (ID: {team_id})")
            return True
            
        except ApiException as e:
            if e.status == 409:  # Namespace already exists
                logger.warning(f"⚠️ Namespace '{namespace_name}' already exists")
                return True
            else:
                logger.error(f"❌ Failed to create namespace '{namespace_name}': {e}")
                return False
        except Exception as e:
            logger.error(f"❌ Unexpected error creating namespace: {e}")
            return False
    
    def delete_namespace(self, namespace_name: str, team_name: str) -> bool:
        """Delete a Kubernetes namespace when team is removed"""
        try:
            self.k8s_core_v1.delete_namespace(name=namespace_name)
            logger.info(f"🗑️ Deleted namespace '{namespace_name}' for removed team '{team_name}'")
            return True
        except ApiException as e:
            if e.status == 404:  # Namespace doesn't exist
                logger.warning(f"⚠️ Namespace '{namespace_name}' not found (already deleted?)")
                return True
            else:
                logger.error(f"❌ Failed to delete namespace '{namespace_name}': {e}")
                return False
        except Exception as e:
            logger.error(f"❌ Unexpected error deleting namespace: {e}")
            return False
    
    def _verify_constraint_exists(self, plural: str, name: str):
        """Warn loudly on startup if a required Gatekeeper constraint is missing."""
        try:
            self.k8s_custom.get_cluster_custom_object(
                group=CONSTRAINT_GROUP, version=CONSTRAINT_VERSION,
                plural=plural, name=name,
            )
            logger.info(f"✅ Constraint '{name}' found")
        except ApiException as e:
            if e.status == 404:
                logger.error(f"❌ Constraint '{name}' not found — apply secops/{plural}*.yaml before running the operator")
            else:
                logger.error(f"Failed to check constraint '{name}': {e}")

    def _get_constraint_namespaces(self, plural: str, name: str) -> list:
        try:
            obj = self.k8s_custom.get_cluster_custom_object(
                group=CONSTRAINT_GROUP, version=CONSTRAINT_VERSION,
                plural=plural, name=name,
            )
            return obj.get("spec", {}).get("match", {}).get("namespaces", [])
        except ApiException as e:
            if e.status == 404:
                logger.warning(f"Constraint '{name}' not found — skipping")
                return []
            logger.error(f"Failed to read constraint '{name}': {e}")
            return []

    def _patch_constraint_namespaces(self, plural: str, name: str, namespaces: list) -> bool:
        try:
            self.k8s_custom.patch_cluster_custom_object(
                group=CONSTRAINT_GROUP, version=CONSTRAINT_VERSION,
                plural=plural, name=name,
                body={"spec": {"match": {"namespaces": namespaces}}},
            )
            logger.info(f"Constraint '{name}' namespaces updated: {namespaces}")
            return True
        except ApiException as e:
            logger.error(f"Failed to patch constraint '{name}': {e}")
            return False

    def _add_ns_to_constraint(self, plural: str, name: str, namespace_name: str):
        namespaces = self._get_constraint_namespaces(plural, name)
        if namespace_name not in namespaces:
            self._patch_constraint_namespaces(plural, name, namespaces + [namespace_name])

    def _remove_ns_from_constraint(self, plural: str, name: str, namespace_name: str):
        namespaces = self._get_constraint_namespaces(plural, name)
        if namespace_name in namespaces:
            self._patch_constraint_namespaces(plural, name, [n for n in namespaces if n != namespace_name])

    def add_namespace_to_constraint(self, namespace_name: str):
        self._add_ns_to_constraint(FALCO_CONSTRAINT_PLURAL, FALCO_CONSTRAINT_NAME, namespace_name)
        self._add_ns_to_constraint(ROLLOUT_CONSTRAINT_PLURAL, ROLLOUT_CONSTRAINT_NAME, namespace_name)

    def remove_namespace_from_constraint(self, namespace_name: str):
        self._remove_ns_from_constraint(FALCO_CONSTRAINT_PLURAL, FALCO_CONSTRAINT_NAME, namespace_name)
        self._remove_ns_from_constraint(ROLLOUT_CONSTRAINT_PLURAL, ROLLOUT_CONSTRAINT_NAME, namespace_name)

    async def reconcile_teams(self):
        """Main reconciliation loop - sync teams with namespaces"""
        teams = await self.fetch_teams()
        current_teams = {team['id']: team for team in teams}
        current_team_ids = set(current_teams.keys())

        # Create namespace for new teams or teams whose namespace was deleted externally
        created_namespaces = []
        for team_id in current_team_ids:
            team = current_teams[team_id]
            namespace_name = self.sanitize_namespace_name(team['name'])
            if not self.namespace_exists(namespace_name):
                if self.create_namespace(team_id, team['name'], namespace_name):
                    created_namespaces.append(namespace_name)
                    self.add_namespace_to_constraint(namespace_name)

        # Delete namespaces for teams no longer in API, identified by operator label
        deleted_namespaces = []
        managed = self.k8s_core_v1.list_namespace(
            label_selector="app.kubernetes.io/managed-by=teams-operator"
        )
        for ns in managed.items:
            team_id = ns.metadata.labels.get("teams.example.com/team-id")
            if team_id and team_id not in current_team_ids:
                if self.delete_namespace(ns.metadata.name, ns.metadata.name):
                    deleted_namespaces.append(ns.metadata.name)
                    self.remove_namespace_from_constraint(ns.metadata.name)

        if created_namespaces or deleted_namespaces:
            logger.info(f"📊 Reconciliation complete: {len(current_teams)} teams, created={created_namespaces}, deleted={deleted_namespaces}")
    
    async def run(self):
        """Main operator loop"""
        logger.info(f"🚀 Teams Operator starting...")
        logger.info(f"📡 Teams API URL: {self.teams_api_url}")
        logger.info(f"⏰ Poll interval: {self.poll_interval} seconds")

        # Verify required Gatekeeper constraints exist before reconciling
        self._verify_constraint_exists(FALCO_CONSTRAINT_PLURAL, FALCO_CONSTRAINT_NAME)
        self._verify_constraint_exists(ROLLOUT_CONSTRAINT_PLURAL, ROLLOUT_CONSTRAINT_NAME)

        # Initial reconciliation
        await self.reconcile_teams()
        
        # Main loop
        while True:
            try:
                await asyncio.sleep(self.poll_interval)
                await self.reconcile_teams()
            except KeyboardInterrupt:
                logger.info("👋 Received shutdown signal, exiting...")
                break
            except Exception as e:
                logger.error(f"❌ Error in main loop: {e}")
                await asyncio.sleep(self.poll_interval)

async def main():
    """Entry point"""
    operator = TeamsOperator()
    await operator.run()

if __name__ == "__main__":
    asyncio.run(main())
