from fastapi import FastAPI, HTTPException, Depends, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html
from fastapi.responses import HTMLResponse, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import List, Optional
import uuid
from datetime import datetime
import sqlite3
import os
import secrets

from kubernetes import client as k8s_client, config as k8s_config
from kubernetes.client.rest import ApiException

# OTel - tracing
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource, SERVICE_NAME as RESOURCE_SERVICE_NAME

# OTel - metrics
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.exporter.prometheus import PrometheusMetricReader

# OTel - auto-instrumentation
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

# Prometheus client for /metrics endpoint
from prometheus_client import REGISTRY, generate_latest, CONTENT_TYPE_LATEST

# Structured logging
import structlog

# --- Configuration ---

DB_PATH = os.getenv("DB_PATH", "/data/teams.db")
ROLLOUT_TEMPLATE_NS = "rollout-system"
PLATFORM_GROUP      = "rollouts.platform.io"
PLATFORM_VERSION    = "v1alpha1"
SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "teams-api")
API_TOKEN = os.getenv("API_TOKEN")
# OTEL_EXPORTER_OTLP_ENDPOINT is read automatically by the OTel SDK from env

# --- OTel setup ---

resource = Resource.create({RESOURCE_SERVICE_NAME: SERVICE_NAME})

# Tracing: BatchSpanProcessor exports spans asynchronously to Jaeger via OTLP HTTP
tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter())
)
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer(SERVICE_NAME)

# Metrics: PrometheusMetricReader registers with prometheus_client default registry
prometheus_reader = PrometheusMetricReader()
meter_provider = MeterProvider(resource=resource, metric_readers=[prometheus_reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter(SERVICE_NAME)

# Custom business metrics
teams_created_counter = meter.create_counter(
    "teams_created_total",
    description="Total number of teams created",
)
teams_deleted_counter = meter.create_counter(
    "teams_deleted_total",
    description="Total number of teams deleted",
)

# --- Structured logging ---

def _add_otel_context(logger, method, event_dict):
    """Inject current trace/span IDs into every log record."""
    span = trace.get_current_span()
    if span.is_recording():
        ctx = span.get_span_context()
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        _add_otel_context,
        structlog.processors.JSONRenderer(),
    ],
    logger_factory=structlog.PrintLoggerFactory(),
)

log = structlog.get_logger()

# --- Auth ---

_bearer_scheme = HTTPBearer(auto_error=False)

def verify_token(credentials: HTTPAuthorizationCredentials = Security(_bearer_scheme)):
    if not API_TOKEN:
        return  # auth disabled — no API_TOKEN configured
    if not credentials or not secrets.compare_digest(credentials.credentials, API_TOKEN):
        log.warning("unauthorized_request")
        raise HTTPException(status_code=401, detail="Invalid or missing token")

# --- App ---

app = FastAPI(
    title="Teams API",
    description="A simple API for team leads to create and manage teams",
    version="1.0.0",
    redoc_url=None  # disabled to allow custom JS URL below
)

# Auto-instrument all FastAPI routes (HTTP spans, attributes, status codes)
FastAPIInstrumentor.instrument_app(app)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS teams (
            id TEXT PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def _team_namespace(name: str) -> str:
    """Derive the Kubernetes namespace for a team name (mirrors operator logic)."""
    import re
    ns = re.sub(r'[^a-z0-9]', '-', name.lower())
    ns = re.sub(r'-+', '-', ns).strip('-')
    return f"team-{ns}"[:63]


# Pydantic models
class TeamCreate(BaseModel):
    name: str

class Team(BaseModel):
    id: str
    name: str
    created_at: datetime


_k8s_custom: k8s_client.CustomObjectsApi | None = None


def get_k8s() -> k8s_client.CustomObjectsApi:
    global _k8s_custom
    if _k8s_custom is None:
        raise HTTPException(status_code=503, detail="Kubernetes client not available")
    return _k8s_custom


def _init_k8s():
    global _k8s_custom
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        try:
            k8s_config.load_kube_config()
        except Exception:
            log.warning("k8s_unavailable", detail="rollout endpoints will return 503")
            return
    _k8s_custom = k8s_client.CustomObjectsApi()
    log.info("k8s_client_ready")


@app.on_event("startup")
def startup():
    init_db()
    _init_k8s()
    if API_TOKEN:
        log.info("startup_complete", db_path=DB_PATH, auth="enabled")
    else:
        log.warning("startup_complete", db_path=DB_PATH, auth="disabled — set API_TOKEN to enable")


@app.get("/redoc", include_in_schema=False)
async def redoc_html() -> HTMLResponse:
    return get_redoc_html(
        openapi_url="/openapi.json",
        title="Teams API - ReDoc",
        redoc_js_url="https://cdn.jsdelivr.net/npm/redoc@2.1.5/bundles/redoc.standalone.js",
    )


@app.get("/metrics", include_in_schema=False)
async def metrics_endpoint() -> Response:
    """Prometheus metrics endpoint for OTel and custom business metrics."""
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


@app.get("/")
async def root():
    return {"message": "Teams API is running"}


@app.post("/teams", response_model=Team, dependencies=[Depends(verify_token)])
async def create_team(team: TeamCreate):
    """Create a new team"""
    log.info("create_team", name=team.name)
    new_ns = _team_namespace(team.name)
    team_id = str(uuid.uuid4())
    created_at = datetime.now().isoformat()
    conn = get_db()
    try:
        # Check for namespace collision before inserting
        with tracer.start_as_current_span("sqlite.select.teams") as span:
            span.set_attribute("db.system", "sqlite")
            span.set_attribute("db.operation", "SELECT")
            span.set_attribute("db.sql.table", "teams")
            existing = conn.execute("SELECT name FROM teams").fetchall()

        for row in existing:
            if _team_namespace(row["name"]) == new_ns:
                log.warning("create_team_namespace_collision",
                            name=team.name, conflict=row["name"], namespace=new_ns)
                raise HTTPException(
                    status_code=400,
                    detail=f"Team name '{team.name}' maps to namespace '{new_ns}' which is already used by team '{row['name']}'"
                )

        with tracer.start_as_current_span("sqlite.insert.teams") as span:
            span.set_attribute("db.system", "sqlite")
            span.set_attribute("db.operation", "INSERT")
            span.set_attribute("db.sql.table", "teams")
            conn.execute(
                "INSERT INTO teams (id, name, created_at) VALUES (?, ?, ?)",
                (team_id, team.name, created_at)
            )
            conn.commit()
    except sqlite3.IntegrityError:
        log.warning("create_team_duplicate", name=team.name)
        raise HTTPException(status_code=400, detail="Team name already exists")
    finally:
        conn.close()
    teams_created_counter.add(1, {"team_name": team.name})
    log.info("team_created", team_id=team_id, name=team.name, namespace=new_ns)
    return Team(id=team_id, name=team.name, created_at=created_at)


@app.get("/teams", response_model=List[Team], dependencies=[Depends(verify_token)])
async def get_teams():
    """Get all teams"""
    conn = get_db()
    with tracer.start_as_current_span("sqlite.select.teams") as span:
        span.set_attribute("db.system", "sqlite")
        span.set_attribute("db.operation", "SELECT")
        span.set_attribute("db.sql.table", "teams")
        rows = conn.execute("SELECT id, name, created_at FROM teams").fetchall()
    conn.close()
    log.info("get_teams", count=len(rows))
    return [Team(**dict(row)) for row in rows]


@app.get("/teams/{team_id}", response_model=Team, dependencies=[Depends(verify_token)])
async def get_team(team_id: str):
    """Get a specific team by ID"""
    conn = get_db()
    with tracer.start_as_current_span("sqlite.select.team") as span:
        span.set_attribute("db.system", "sqlite")
        span.set_attribute("db.operation", "SELECT")
        span.set_attribute("db.sql.table", "teams")
        span.set_attribute("team.id", team_id)
        row = conn.execute("SELECT id, name, created_at FROM teams WHERE id = ?", (team_id,)).fetchone()
    conn.close()
    if row is None:
        log.warning("get_team_not_found", team_id=team_id)
        raise HTTPException(status_code=404, detail="Team not found")
    return Team(**dict(row))


@app.delete("/teams/{team_id}", dependencies=[Depends(verify_token)])
async def delete_team(team_id: str):
    """Delete a team"""
    conn = get_db()
    with tracer.start_as_current_span("sqlite.delete.team") as span:
        span.set_attribute("db.system", "sqlite")
        span.set_attribute("db.operation", "DELETE")
        span.set_attribute("db.sql.table", "teams")
        span.set_attribute("team.id", team_id)
        row = conn.execute("SELECT name FROM teams WHERE id = ?", (team_id,)).fetchone()
        if row is None:
            conn.close()
            log.warning("delete_team_not_found", team_id=team_id)
            raise HTTPException(status_code=404, detail="Team not found")
        conn.execute("DELETE FROM teams WHERE id = ?", (team_id,))
        conn.commit()
    conn.close()
    teams_deleted_counter.add(1, {"team_name": row["name"]})
    log.info("team_deleted", team_id=team_id, name=row["name"])
    return {"message": f"Team '{row['name']}' deleted successfully"}


@app.get("/health")
async def health_check():
    """Health check endpoint for Kubernetes"""
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
    conn.close()
    return {"status": "healthy", "teams_count": count}


# ---------------------------------------------------------------------------
# /rollout — template registry + deployment management (no auth required)
# ---------------------------------------------------------------------------

class RolloutTemplateOut(BaseModel):
    name: str
    image: str
    strategy: str
    replicas: int


class RolloutRequestCreate(BaseModel):
    templateRef: str
    replicas: Optional[int] = None


class RolloutRequestOut(BaseModel):
    name: str
    namespace: str
    templateRef: str
    status: dict


@app.get("/rollout/templates", response_model=List[RolloutTemplateOut], tags=["rollout"])
def list_rollout_templates(k8s=Depends(get_k8s)):
    """List available RolloutTemplates from rollout-system."""
    try:
        result = k8s.list_namespaced_custom_object(
            group=PLATFORM_GROUP, version=PLATFORM_VERSION,
            namespace=ROLLOUT_TEMPLATE_NS, plural="rollouttemplates",
        )
    except ApiException as e:
        raise HTTPException(status_code=502, detail=f"Kubernetes error: {e.reason}")

    out = []
    for item in result.get("items", []):
        spec = item.get("spec", {})
        out.append(RolloutTemplateOut(
            name=item["metadata"]["name"],
            image=spec.get("image", ""),
            strategy=spec.get("strategy", {}).get("type", "Canary"),
            replicas=spec.get("replicas", 2),
        ))
    return out


@app.get("/rollout/namespaces/{namespace}/deployments", response_model=List[RolloutRequestOut], tags=["rollout"])
def list_deployments(namespace: str, k8s=Depends(get_k8s)):
    """List RolloutRequests in a team namespace."""
    try:
        result = k8s.list_namespaced_custom_object(
            group=PLATFORM_GROUP, version=PLATFORM_VERSION,
            namespace=namespace, plural="rolloutrequests",
        )
    except ApiException as e:
        if e.status == 404:
            return []
        raise HTTPException(status_code=502, detail=f"Kubernetes error: {e.reason}")

    return [
        RolloutRequestOut(
            name=item["metadata"]["name"],
            namespace=namespace,
            templateRef=item["spec"]["templateRef"],
            status=item.get("status", {"phase": "Pending"}),
        )
        for item in result.get("items", [])
    ]


@app.post("/rollout/namespaces/{namespace}/deployments/{name}", response_model=RolloutRequestOut, status_code=201, tags=["rollout"])
def create_deployment(namespace: str, name: str, body: RolloutRequestCreate, k8s=Depends(get_k8s)):
    """Create a RolloutRequest in a team namespace from an existing template."""
    spec: dict = {"templateRef": body.templateRef}
    if body.replicas is not None:
        spec["replicas"] = body.replicas

    resource = {
        "apiVersion": f"{PLATFORM_GROUP}/{PLATFORM_VERSION}",
        "kind": "RolloutRequest",
        "metadata": {"name": name, "namespace": namespace},
        "spec": spec,
    }
    try:
        k8s.create_namespaced_custom_object(
            group=PLATFORM_GROUP, version=PLATFORM_VERSION,
            namespace=namespace, plural="rolloutrequests", body=resource,
        )
    except ApiException as e:
        if e.status == 409:
            raise HTTPException(status_code=409, detail="Deployment already exists")
        raise HTTPException(status_code=502, detail=f"Kubernetes error: {e.reason}")

    log.info("rollout_request_created", namespace=namespace, name=name, template=body.templateRef)
    return RolloutRequestOut(
        name=name, namespace=namespace,
        templateRef=body.templateRef,
        status={"phase": "Pending"},
    )


@app.delete("/rollout/namespaces/{namespace}/deployments/{name}", status_code=204, tags=["rollout"])
def delete_deployment(namespace: str, name: str, k8s=Depends(get_k8s)):
    """Delete a RolloutRequest (and its associated Rollout via rollout-operator)."""
    try:
        k8s.delete_namespaced_custom_object(
            group=PLATFORM_GROUP, version=PLATFORM_VERSION,
            namespace=namespace, plural="rolloutrequests", name=name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail="Deployment not found")
        raise HTTPException(status_code=502, detail=f"Kubernetes error: {e.reason}")

    log.info("rollout_request_deleted", namespace=namespace, name=name)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
