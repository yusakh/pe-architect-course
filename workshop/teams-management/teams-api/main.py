from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from typing import List
import uuid
from datetime import datetime
import sqlite3
import os

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
SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "teams-api")
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


# Pydantic models
class TeamCreate(BaseModel):
    name: str

class Team(BaseModel):
    id: str
    name: str
    created_at: datetime


@app.on_event("startup")
def startup():
    init_db()
    log.info("startup_complete", db_path=DB_PATH)


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


@app.post("/teams", response_model=Team)
async def create_team(team: TeamCreate):
    """Create a new team"""
    log.info("create_team", name=team.name)
    team_id = str(uuid.uuid4())
    created_at = datetime.now().isoformat()
    conn = get_db()
    try:
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
        # IntegrityError is raised when UNIQUE constraint on name is violated
        log.warning("create_team_duplicate", name=team.name)
        raise HTTPException(status_code=400, detail="Team name already exists")
    finally:
        conn.close()
    teams_created_counter.add(1, {"team_name": team.name})
    log.info("team_created", team_id=team_id, name=team.name)
    return Team(id=team_id, name=team.name, created_at=created_at)


@app.get("/teams", response_model=List[Team])
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


@app.get("/teams/{team_id}", response_model=Team)
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


@app.delete("/teams/{team_id}")
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
