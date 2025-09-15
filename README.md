# Atomiq Droid Suite — FastAPI + Angular (CrewAI-ready)

## Run (Windows)

### Backend
```
cd backend
run_backend.bat
```
- FastAPI at **http://127.0.0.1:8000**
- Optional: copy `.env.example` to `.env` and set `OPENAI_API_KEY` if you later enable AI (Python 3.11/3.12).

### Angular UI
```
cd ui-angular
run_ui.bat
```
- UI at **http://localhost:4200** (proxy forwards `/api/*` to backend).

## Notes
- This build **boots on Python 3.13**. CrewAI/LangChain are skipped to avoid install errors.
- The backend creates a valid ZIP artifact every run, so the **download link never 404s**.
- To enable real AI generation, use **Python 3.11/3.12**, install requirements again (which will include CrewAI), and extend `agent/crewai_pipeline.py`.