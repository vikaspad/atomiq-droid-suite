# main.py

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import (
    FastAPI,
    Form,
    File,
    UploadFile,
    BackgroundTasks,
    HTTPException,
)
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from agent.models import BuildOptions
from agent.orchestrator import run_pipeline

# Creates the web API application (title/version shown in docs).
app = FastAPI(title="Atomiq Suite Backend", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],           # tighten if you don't use the Angular proxy
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

JOBS: Dict[str, Dict[str, Any]] = {}

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
# Safely converts form values into True/False
def to_bool(v) -> bool:
    """Parse truthy/falsey values coming from multipart forms reliably."""
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    # Some frameworks send lists like ['true']
    if isinstance(v, (list, tuple)) and v:
        v = v[0]
    return str(v).strip().lower() in {"1", "true", "yes", "on", "y", "t"}

def _now_iso() -> str:
    """UTC timestamp for logs & responses."""
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"

def _job_dir(base: Path, job_id: str) -> Path:
    """Create & return the working directory for a job."""
    p = base / "work" / job_id
    p.mkdir(parents=True, exist_ok=True)
    return p

# -----------------------------------------------------------------------------
# Health
# -----------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"ok": True, "time": _now_iso()}

# -----------------------------------------------------------------------------
# Build: start a generation job
# -----------------------------------------------------------------------------
# Kicks off the whole pipeline in the background and immediately returns a jobId.
@app.post("/api/build")
async def build(
    background: BackgroundTasks,

    # Canonical snake_case fields (Angular service should send these)
    github_url: str = Form(...),
    prompt: str = Form(""),
    llm_provider: str = Form("openai"),
    llm_model: str = Form("gpt-4o-mini"),
    api_key: Optional[str] = Form(None),
    generate_unit: Optional[str] = Form(None),
    generate_bdd: Optional[str] = Form(None),

    # Legacy camelCase tolerated for older UIs (optional)
    generateUnitTests: Optional[str] = Form(None),
    createBDDFramework: Optional[str] = Form(None),

    # Optional uploaded requirements/spec file (used to enrich context)
    file: UploadFile | None = File(None),
):
    # --- Normalize & enforce radio semantics (Unit vs BDD) ---
    gen_unit = to_bool(generate_unit) or to_bool(generateUnitTests)
    gen_bdd = to_bool(generate_bdd) or to_bool(createBDDFramework)

    # Exactly one should be true; default to Unit when ambiguous
    if gen_unit and gen_bdd:
        gen_bdd = False
    elif not gen_unit and not gen_bdd:
        gen_unit = True

    # --- Make OpenAI key available to CrewAI/LangChain (never log the key) ---
    if api_key:
        os.environ["OPENAI_API_KEY"] = api_key

    # --- Create job entry & persist upload (if any) ---
    job_id = str(uuid.uuid4())
    base_dir = Path(os.getcwd())
    work_dir = _job_dir(base_dir, job_id)

    uploaded_path: Optional[str] = None
    if file is not None and file.filename:
        dst = work_dir / "upload" / file.filename
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(await file.read())
        uploaded_path = str(dst)

    JOBS[job_id] = {
        "status": "pending",
        "progress": 0,
        "message": "Queued",
        "logs": [],
        "artifact": None,
        "work_dir": str(work_dir),
        "created_at": _now_iso(),
        "flags": {"generate_unit": gen_unit, "generate_bdd": gen_bdd},
    }

    # --- Options object passed to orchestrator ---
    opts = BuildOptions(
        job_id=job_id,
        github_url=github_url,
        prompt=prompt,
        llm_provider=llm_provider,
        llm_model=llm_model,
        api_key=api_key,
        requirement_path=uploaded_path,   # <— CrewAI agents can read this
        generate_unit=gen_unit,
        generate_bdd=gen_bdd,
    )

    # --- Pipeline progress callback -> updates the JOBS store ---
    def _progress(pct: int, status: str, message: str = ""):
        pct = max(0, min(100, int(pct)))
        job = JOBS[job_id]
        job["progress"] = pct
        if job["status"] not in {"succeeded", "failed"}:
            job["status"] = "running" if pct < 100 else job["status"]
        job["message"] = message or status
        job["logs"].append({
            "progress": pct, "status": status, "message": message, "ts": _now_iso()
        })

    # --- Background worker that runs the pipeline and finalizes the job ---
    def _run_job():
        try:
            JOBS[job_id]["status"] = "running"
            zip_path = run_pipeline(opts, job_progress_cb=_progress, base_dir=str(base_dir))
            if zip_path and Path(zip_path).is_file():
                JOBS[job_id]["artifact"] = zip_path
                JOBS[job_id]["progress"] = 100
                JOBS[job_id]["status"] = "succeeded"
                JOBS[job_id]["message"] = "Complete"
                JOBS[job_id]["logs"].append({
                    "progress": 100, "status": "Complete", "message": "Artifact ready", "ts": _now_iso()
                })
            else:
                raise FileNotFoundError("Artifact ZIP not found after pipeline.")
        except Exception as e:
            JOBS[job_id]["status"] = "failed"
            JOBS[job_id]["message"] = f"Error: {e}"
            JOBS[job_id]["logs"].append({
                "progress": JOBS[job_id]["progress"], "status": "Error", "message": str(e), "ts": _now_iso()
            })

    background.add_task(_run_job)
    return JSONResponse({"jobId": job_id})

# -----------------------------------------------------------------------------
# Job status (polling)
# -----------------------------------------------------------------------------
@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    artifact_url = f"/api/jobs/{job_id}/artifact" if job.get("artifact") else None
    return {
        "jobId": job_id,
        "status": job["status"],
        "progress": job["progress"],
        "message": job["message"],
        "artifactUrl": artifact_url,
        "flags": job.get("flags", {}),
        "createdAt": job["created_at"],
        "logs": job["logs"][-200:],  # keep payload lean
    }

# -----------------------------------------------------------------------------
# Server-Sent Events: stream progress until job completes
# -----------------------------------------------------------------------------
@app.get("/api/jobs/{job_id}/events")
async def stream_events(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_gen():
        last_len = 0
        while True:
            job = JOBS.get(job_id)
            if not job:
                break

            # Stream only new log entries
            logs = job["logs"]
            if len(logs) > last_len:
                for entry in logs[last_len:]:
                    payload = {
                        "jobId": job_id,
                        "progress": job["progress"],
                        "status": job["status"],
                        "message": entry.get("message") or entry.get("status"),
                        "ts": entry.get("ts"),
                        "artifactUrl": f"/api/jobs/{job_id}/artifact" if job.get("artifact") else None,
                    }
                    # Proper SSE frame
                    yield "data: " + json.dumps(payload) + "\n\n"
                last_len = len(logs)

            # Stop streaming after a terminal state
            if job["status"] in {"succeeded", "failed"}:
                break

            await asyncio.sleep(0.5)

    headers = {"Cache-Control": "no-cache", "Connection": "keep-alive"}
    return StreamingResponse(event_gen(), media_type="text/event-stream", headers=headers)

# -----------------------------------------------------------------------------
# Download artifact ZIP
# -----------------------------------------------------------------------------
@app.get("/api/jobs/{job_id}/artifact")
async def download_artifact(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    artifact = job.get("artifact")
    if not artifact or not Path(artifact).is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(
        path=artifact,
        media_type="application/zip",
        filename=Path(artifact).name,
        headers={"Cache-Control": "no-cache"},
    )

# -----------------------------------------------------------------------------
# Local dev entrypoint
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    # Run locally with:
    #   uvicorn main:app --reload --port 8000
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
