from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List
import uuid
from datetime import datetime
import sqlite3
import os

DB_PATH = os.getenv("DB_PATH", "/data/teams.db")

app = FastAPI(
    title="Teams API",
    description="A simple API for team leads to create and manage teams",
    version="1.0.0",
    redoc_url=None  # disabled to allow custom JS URL below
)

@app.get("/redoc", include_in_schema=False)
async def redoc_html() -> HTMLResponse:
    return get_redoc_html(
        openapi_url="/openapi.json",
        title="Teams API - ReDoc",
        redoc_js_url="https://cdn.jsdelivr.net/npm/redoc@2.1.5/bundles/redoc.standalone.js",
    )


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


@app.get("/")
async def root():
    return {"message": "Teams API is running"}


@app.post("/teams", response_model=Team)
async def create_team(team: TeamCreate):
    """Create a new team"""
    team_id = str(uuid.uuid4())
    created_at = datetime.now().isoformat()
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO teams (id, name, created_at) VALUES (?, ?, ?)",
            (team_id, team.name, created_at)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # IntegrityError is raised when UNIQUE constraint on name is violated
        raise HTTPException(status_code=400, detail="Team name already exists")
    finally:
        conn.close()
    return Team(id=team_id, name=team.name, created_at=created_at)


@app.get("/teams", response_model=List[Team])
async def get_teams():
    """Get all teams"""
    conn = get_db()
    rows = conn.execute("SELECT id, name, created_at FROM teams").fetchall()
    conn.close()
    return [Team(**dict(row)) for row in rows]


@app.get("/teams/{team_id}", response_model=Team)
async def get_team(team_id: str):
    """Get a specific team by ID"""
    conn = get_db()
    row = conn.execute("SELECT id, name, created_at FROM teams WHERE id = ?", (team_id,)).fetchone()
    conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Team not found")
    return Team(**dict(row))


@app.delete("/teams/{team_id}")
async def delete_team(team_id: str):
    """Delete a team"""
    conn = get_db()
    row = conn.execute("SELECT name FROM teams WHERE id = ?", (team_id,)).fetchone()
    if row is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Team not found")
    conn.execute("DELETE FROM teams WHERE id = ?", (team_id,))
    conn.commit()
    conn.close()
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
