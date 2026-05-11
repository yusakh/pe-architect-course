#!/usr/bin/env python3
"""
Rollout Operator - Watches RolloutRequest CRDs and creates Argo Rollout resources
"""

import asyncio
import logging
import os
from kubernetes import client, config
from kubernetes.client.rest import ApiException

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('rollout-operator')

PLATFORM_GROUP   = "rollouts.platform.io"
PLATFORM_VERSION = "v1alpha1"
ARGO_GROUP       = "argoproj.io"
ARGO_VERSION     = "v1alpha1"
TEMPLATE_NS      = "rollout-system"


class RolloutOperator:
    def __init__(self):
        self.poll_interval = int(os.getenv('POLL_INTERVAL', '15'))
        try:
            config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes config")
        except config.ConfigException:
            config.load_kube_config()
            logger.info("Loaded local kubeconfig")

        self.custom = client.CustomObjectsApi()

    def _get_template(self, name: str) -> dict | None:
        try:
            return self.custom.get_namespaced_custom_object(
                group=PLATFORM_GROUP, version=PLATFORM_VERSION,
                namespace=TEMPLATE_NS, plural="rollouttemplates", name=name,
            )
        except ApiException as e:
            if e.status == 404:
                logger.warning(f"RolloutTemplate '{name}' not found in {TEMPLATE_NS}")
                return None
            raise

    def _list_requests(self) -> list:
        try:
            result = self.custom.list_cluster_custom_object(
                group=PLATFORM_GROUP, version=PLATFORM_VERSION, plural="rolloutrequests",
            )
            return result.get("items", [])
        except ApiException as e:
            logger.error(f"Failed to list RolloutRequests: {e}")
            return []

    def _patch_request_status(self, namespace: str, name: str, phase: str, message: str = "", rollout_name: str = ""):
        try:
            self.custom.patch_namespaced_custom_object_status(
                group=PLATFORM_GROUP, version=PLATFORM_VERSION,
                namespace=namespace, plural="rolloutrequests", name=name,
                body={"status": {"phase": phase, "message": message, "rolloutName": rollout_name}},
            )
        except ApiException as e:
            logger.error(f"Failed to patch status for {namespace}/{name}: {e}")

    def _rollout_exists(self, namespace: str, name: str) -> bool:
        try:
            self.custom.get_namespaced_custom_object(
                group=ARGO_GROUP, version=ARGO_VERSION,
                namespace=namespace, plural="rollouts", name=name,
            )
            return True
        except ApiException as e:
            return e.status != 404

    def _create_rollout(self, namespace: str, request: dict, template: dict) -> str:
        spec = template.get("spec", {})
        req_spec = request.get("spec", {})
        name = request["metadata"]["name"]
        replicas = req_spec.get("replicas", spec.get("replicas", 2))

        canary_steps = spec.get("strategy", {}).get("canary", {}).get("steps", [
            {"setWeight": 50},
            {"pause": {"duration": 30}},
            {"setWeight": 100},
        ])

        rollout_body = {
            "apiVersion": f"{ARGO_GROUP}/{ARGO_VERSION}",
            "kind": "Rollout",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": {
                    "app": name,
                    "app.kubernetes.io/managed-by": "rollout-operator",
                    "rollouts.platform.io/template": req_spec["templateRef"],
                },
            },
            "spec": {
                "replicas": replicas,
                "revisionHistoryLimit": 2,
                "strategy": {
                    "canary": {"steps": canary_steps}
                },
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {"labels": {"app": name}},
                    "spec": {
                        "securityContext": {
                            "runAsUser": 1000,
                            "runAsNonRoot": True,
                            "runAsGroup": 1000,
                        },
                        "containers": [{
                            "name": name,
                            "image": spec["image"],
                            "ports": [{"name": "http", "containerPort": spec.get("port", 8080), "protocol": "TCP"}],
                            "resources": spec.get("resources", {
                                "requests": {"memory": "64Mi", "cpu": "50m"},
                                "limits":   {"memory": "128Mi", "cpu": "100m"},
                            }),
                            "securityContext": {"allowPrivilegeEscalation": False},
                        }],
                    },
                },
            },
        }

        self.custom.create_namespaced_custom_object(
            group=ARGO_GROUP, version=ARGO_VERSION,
            namespace=namespace, plural="rollouts", body=rollout_body,
        )
        return name

    def _list_managed_rollouts(self) -> list:
        try:
            result = self.custom.list_cluster_custom_object(
                group=ARGO_GROUP, version=ARGO_VERSION, plural="rollouts",
                label_selector="app.kubernetes.io/managed-by=rollout-operator",
            )
            return result.get("items", [])
        except ApiException as e:
            logger.error(f"Failed to list managed Rollouts: {e}")
            return []

    def _delete_rollout(self, namespace: str, name: str):
        try:
            self.custom.delete_namespaced_custom_object(
                group=ARGO_GROUP, version=ARGO_VERSION,
                namespace=namespace, plural="rollouts", name=name,
            )
            logger.info(f"🗑️ Deleted orphaned Rollout '{name}' in namespace '{namespace}'")
        except ApiException as e:
            if e.status != 404:
                logger.error(f"Failed to delete Rollout '{name}': {e}")

    def reconcile(self):
        # Build index of existing RolloutRequests for cascade-delete check
        existing_requests = {
            (r["metadata"]["namespace"], r["metadata"]["name"])
            for r in self._list_requests()
        }

        # Delete Rollouts whose RolloutRequest was removed
        for rollout in self._list_managed_rollouts():
            ns   = rollout["metadata"]["namespace"]
            name = rollout["metadata"]["name"]
            if (ns, name) not in existing_requests:
                self._delete_rollout(ns, name)

        # Create Rollouts for pending requests
        for req in self._list_requests():
            ns   = req["metadata"]["namespace"]
            name = req["metadata"]["name"]
            phase = req.get("status", {}).get("phase", "Pending")

            if phase in ("Running", "Failed"):
                continue

            self._patch_request_status(ns, name, "Creating")
            template_ref = req.get("spec", {}).get("templateRef")
            template = self._get_template(template_ref)

            if not template:
                self._patch_request_status(ns, name, "Failed", f"Template '{template_ref}' not found")
                continue

            if self._rollout_exists(ns, name):
                self._patch_request_status(ns, name, "Running", rollout_name=name)
                continue

            try:
                rollout_name = self._create_rollout(ns, req, template)
                self._patch_request_status(ns, name, "Running", rollout_name=rollout_name)
                logger.info(f"✅ Created Rollout '{rollout_name}' in namespace '{ns}'")
            except ApiException as e:
                msg = f"Failed to create Rollout: {e.reason}"
                self._patch_request_status(ns, name, "Failed", msg)
                logger.error(f"❌ {msg}")

    async def run(self):
        logger.info(f"🚀 Rollout Operator starting (poll interval: {self.poll_interval}s)")
        self.reconcile()
        while True:
            try:
                await asyncio.sleep(self.poll_interval)
                self.reconcile()
            except KeyboardInterrupt:
                logger.info("👋 Shutting down")
                break
            except Exception as e:
                logger.error(f"❌ Error in main loop: {e}")


async def main():
    await RolloutOperator().run()


if __name__ == "__main__":
    asyncio.run(main())
