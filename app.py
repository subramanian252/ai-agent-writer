from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from backend import IMAGES_DIR, OUTPUT_DIR, generate_blog

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Agent Writer")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
app.mount("/images", StaticFiles(directory=IMAGES_DIR), name="images")
app.mount("/outputs", StaticFiles(directory=OUTPUT_DIR), name="outputs")


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

    output_path = result.get("output_path")
    output_url = f"/outputs/{Path(output_path).name}" if output_path else None

    return {
        "markdown": result.get("final", ""),
        "output_url": output_url,
        "thread_id": thread_id,
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8001, reload=True)
