from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from backend import generate_blog

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Agent Writer")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


class GenerateRequest(BaseModel):
    topic: str = Field(min_length=3, max_length=300)


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.post("/api/generate")
async def generate(request: GenerateRequest):
    thread_id = uuid4().hex

    try:
        result = await run_in_threadpool(generate_blog, request.topic.strip(), thread_id)
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error

    return {
        "markdown": result.get("final", ""),
        "output_url": result.get("output_url"),
        "thread_id": thread_id,
    }
