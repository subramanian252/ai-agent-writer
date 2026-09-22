import base64
import operator
import os
import re
from pathlib import Path
from typing import Annotated, Literal, Optional, TypedDict
from uuid import uuid4

import requests
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langchain_tavily import TavilySearch
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel, Field
from vercel.blob import BlobClient

load_dotenv()

blob_client = BlobClient()

llm = ChatOpenAI(
    model="gpt-4o-mini",
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ.get("OPENROUTER_API_KEY"),
    stream_usage=True,
)
search = TavilySearch()


class Task(BaseModel):
    id: int
    title: str
    goal: str = Field(..., description="One sentence describing the task goal")
    bullets: list[str] = Field(..., min_length=3, max_length=5)
    target_words: int = Field(..., ge=100, le=340)
    section_type: Literal[
        "introduction", "core", "examples", "checklist", "common-mistakes", "conclusion"
    ]
    tags: list[str] = Field(default_factory=list)
    requires_research: bool = False
    requires_citations: bool = False
    requires_code: bool = False


class Plan(BaseModel):
    blog_title: str
    tasks: list[Task]
    audience: str
    tone: str


class EvidenceItem(BaseModel):
    title: str
    url: str
    published_at: Optional[str] = None
    snippet: Optional[str] = None
    source: Optional[str] = None


class EvidencePack(BaseModel):
    evidence: list[EvidenceItem]


class ImageSpec(BaseModel):
    placeholder: str = Field(..., description="For example [[IMAGE_1]]")
    section_title: str
    filename: str
    alt: str
    caption: str
    prompt: str
    size: Literal["1024x1024", "1024x1536", "1536x1024"] = "1024x1024"
    quality: Literal["low", "medium", "high"] = "medium"


class GlobalImagePlan(BaseModel):
    md_with_placeholders: str
    images: list[ImageSpec] = Field(default_factory=list)


class RouterDecision(BaseModel):
    needs_research: bool
    mode: Literal["closed_book", "hybrid", "open_book"]
    queries: list[str] = Field(default_factory=list)


class State(TypedDict, total=False):
    topic: str
    mode: str
    needs_research: bool
    queries: list[str]
    evidence: list[EvidenceItem]
    plan: Plan
    sections: Annotated[list[tuple[int, str]], operator.add]
    merged_md: str
    md_with_placeholders: str
    image_specs: list[dict]
    final: str
    output_url: str
    generation_id: str


ROUTER_SYSTEM = """You route a technical blog-writing request.
Classify it as closed_book, hybrid, or open_book. Use research for current or changing
information. If research is needed, return 3-8 specific search queries. Return only the
structured routing decision."""

PLANNER_SYSTEM = """You are a senior technical writer. Create a practical technical blog plan.
Create 5-7 sections with exactly one introduction and conclusion, at least two core
sections, and at least one examples, checklist, or common-mistakes section. Each task
must have a unique integer id starting at 1, 3-5 concrete bullets, and 100-340 target
words. Return only the structured plan."""

SECTION_SYSTEM = """Write exactly one high-quality Markdown section for a technical blog.
Start with the supplied section title as an H2 heading. Cover every supplied bullet,
match the audience and tone, use code only when useful, and return only the Markdown
section."""

IMAGE_SYSTEM = """You are an expert technical editor.
Choose at most three images that materially improve the article.
For every image, section_title must exactly match one existing Markdown ## heading.
Copy only the heading text verbatim, including capitalization and punctuation.
Do not include the leading ## Markdown prefix in section_title.
Insert each placeholder in the matching section and return strictly GlobalImagePlan.
If no image is useful, return the original Markdown and images=[]."""


def router(state: State) -> dict:
    decision = llm.with_structured_output(RouterDecision).invoke([
        SystemMessage(content=ROUTER_SYSTEM),
        HumanMessage(content=f"Topic: {state['topic']}"),
    ])
    return decision.model_dump()


def route_after_router(state: State) -> str:
    return "research" if state["needs_research"] else "orchestrator"


def tavily_research(query: str) -> list[dict]:
    response = search.invoke(query)
    return [
        {
            "title": result.get("title", ""),
            "url": result.get("url", ""),
            "snippet": result.get("content") or result.get("snippet", ""),
            "published_at": result.get("published_at", ""),
            "source": result.get("source", ""),
        }
        for result in (response.get("results") or [])
    ]


def researcher(state: State) -> dict:
    raw_results = [item for query in state.get("queries", []) for item in tavily_research(query)]
    if not raw_results:
        return {"evidence": []}

    pack = llm.with_structured_output(EvidencePack).invoke([
        SystemMessage(content="Extract only relevant, authoritative evidence with valid URLs."),
        HumanMessage(content=f"Raw search results: {raw_results}"),
    ])
    deduplicated = {item.url: item for item in pack.evidence if item.url}
    return {"evidence": list(deduplicated.values())}


def orchestrator(state: State) -> dict:
    evidence = state.get("evidence", [])
    plan = llm.with_structured_output(Plan).invoke([
        SystemMessage(content=PLANNER_SYSTEM),
        HumanMessage(content=(
            f"Topic: {state['topic']}\n"
            f"Mode: {state.get('mode', 'closed_book')}\n"
            f"Evidence: {evidence}"
        )),
    ])
    return {"plan": plan}


def fanout(state: State):
    return [
        Send("worker", {
            "task": task,
            "topic": state["topic"],
            "plan": state["plan"],
            "evidence": state.get("evidence", []),
        })
        for task in state["plan"].tasks
    ]


def worker(payload: dict) -> dict:
    task = payload["task"]
    plan = payload["plan"]
    evidence_text = "\n\n".join(
        f"Title: {item.title}\nURL: {item.url}\nSnippet: {item.snippet or ''}"
        for item in payload.get("evidence", [])
    )
    section = llm.invoke([
        SystemMessage(content=SECTION_SYSTEM),
        HumanMessage(content=(
            f"Blog title: {plan.blog_title}\nAudience: {plan.audience}\nTone: {plan.tone}\n"
            f"Topic: {payload['topic']}\nSection title: {task.title}\nGoal: {task.goal}\n"
            f"Target words: {task.target_words}\nBullets: {task.bullets}\n"
            f"Evidence: {evidence_text}"
        )),
    ]).content.strip()
    return {"sections": [(task.id, section)]}


def merge_content(state: State) -> dict:
    ordered_sections = [
        markdown for _, markdown in sorted(state["sections"], key=lambda item: item[0])
    ]
    return {"merged_md": f"# {state['plan'].blog_title}\n\n{'\n\n'.join(ordered_sections).strip()}\n"}


def normalize_section_title(title: str) -> str:
    return re.sub(r"^\s*#{1,6}\s*", "", title).strip()


def decide_images(state: State) -> dict:
    merged_md = state["merged_md"]
    headings = re.findall(r"^##\s+(.+?)\s*$", merged_md, flags=re.MULTILINE)
    available_headings = "\n".join(f"- {heading}" for heading in headings)
    image_plan = llm.with_structured_output(GlobalImagePlan).invoke([
        SystemMessage(content=IMAGE_SYSTEM),
        HumanMessage(content=(
            f"Available section headings:\n{available_headings}\n\n"
            f"Article Markdown:\n{merged_md}"
        )),
    ])
    valid_headings = set(headings)
    image_specs = []
    for image in image_plan.images:
        section_title = normalize_section_title(image.section_title)
        if section_title not in valid_headings:
            raise ValueError(
                f"Invalid section title: {image.section_title!r}; expected one of {headings!r}"
            )
        image_spec = image.model_dump()
        image_spec["section_title"] = section_title
        image_specs.append(image_spec)

    return {
        "md_with_placeholders": image_plan.md_with_placeholders,
        "image_specs": image_specs,
    }


def insert_missing_placeholders(md: str, image_specs: list[dict]) -> str:
    for spec in image_specs:
        placeholder = spec["placeholder"]
        if placeholder in md:
            continue
        heading = f"## {spec['section_title']}"
        heading_position = md.find(heading)
        if heading_position == -1:
            raise ValueError(f"Section not found: {spec['section_title']}")
        insertion_position = md.find("\n", heading_position) + 1
        md = md[:insertion_position] + f"\n{placeholder}\n" + md[insertion_position:]
    return md


def safe_filename(title: str) -> str:
    filename = re.sub(r"[^a-zA-Z0-9._-]+", "-", title).strip("-._").lower()
    return filename[:100] or "article"


def _openrouter_generate_image_bytes(prompt: str) -> bytes:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    response = requests.post(
        "https://openrouter.ai/api/v1/images",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": "bytedance-seed/seedream-4.5",
            "prompt": prompt,
            "resolution": "2K",
        },
        timeout=120,
    )
    if not response.ok:
        raise RuntimeError(f"OpenRouter image error {response.status_code}: {response.text}")
    return base64.b64decode(response.json()["data"][0]["b64_json"])

CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def detect_image_type(image_bytes: bytes, filename: str) -> tuple[str, str]:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return ".webp", "image/webp"

    suffix = Path(filename).suffix.lower()
    suffix = suffix if suffix in CONTENT_TYPES else ".png"
    return suffix, CONTENT_TYPES[suffix]


def upload_image(image_bytes: bytes, filename: str, generation_id: str) -> str:
    suffix, content_type = detect_image_type(image_bytes, filename)

    blob = blob_client.put(
        f"articles/{generation_id}/{uuid4().hex}{suffix}",
        image_bytes,
        access="public",
        content_type=content_type,
        cache_control_max_age=31536000,
    )

    return blob.url


def generate_and_place_images(state: State) -> dict:
    plan = state["plan"]
    markdown = state.get("md_with_placeholders") or state["merged_md"]
    image_specs = state.get("image_specs", []) or []
    markdown = insert_missing_placeholders(markdown, image_specs)

    for spec in image_specs:
        try:
            image_bytes = _openrouter_generate_image_bytes(spec["prompt"])
            image_url = upload_image(image_bytes, spec["filename"], state["generation_id"])
        except Exception as error:
            failure = (
                f"> **[IMAGE GENERATION FAILED]** {spec.get('caption', '')}\n>\n"
                f"> **Error:** {error}\n"
            )
            markdown = markdown.replace(spec["placeholder"], failure)
            continue

        image_markdown = (
            f"![{spec['alt']}]({image_url})\n"
            f"*{spec['caption']}*"
        )
        markdown = markdown.replace(spec["placeholder"], image_markdown)

    if not markdown.strip():
        raise ValueError("Markdown content is empty before upload")

    article_filename = f"{safe_filename(plan.blog_title)}.md"
    article_blob = blob_client.put(
        f"articles/{state['generation_id']}/{article_filename}",
        markdown.encode("utf-8"),
        access="public",
        content_type="text/markdown; charset=utf-8",
        cache_control_max_age=3600,
    )

    return {"final": markdown, "output_url": article_blob.download_url}


def build_checkpointer():
    database_url = os.environ.get("EXTERNAL_DATABASE_URL")
    if not database_url:
        return MemorySaver()

    try:
        import psycopg
        from psycopg.rows import dict_row
        from langgraph.checkpoint.postgres import PostgresSaver

        connection = psycopg.connect(
            database_url,
            autocommit=True,
            row_factory=dict_row,
        )
        checkpointer = PostgresSaver(connection)
        checkpointer.setup()
        return checkpointer
    except Exception as error:
        print(f"Postgres checkpointing unavailable; using memory: {error}")
        return MemorySaver()


def build_workflow():
    reducer_graph = StateGraph(State)
    reducer_graph.add_node("merge_content", merge_content)
    reducer_graph.add_node("decide_images", decide_images)
    reducer_graph.add_node("generate_and_place_images", generate_and_place_images)
    reducer_graph.add_edge(START, "merge_content")
    reducer_graph.add_edge("merge_content", "decide_images")
    reducer_graph.add_edge("decide_images", "generate_and_place_images")
    reducer_graph.add_edge("generate_and_place_images", END)
    reducer_subgraph = reducer_graph.compile()

    graph = StateGraph(State)
    graph.add_node("router", router)
    graph.add_node("research", researcher)
    graph.add_node("orchestrator", orchestrator)
    graph.add_node("worker", worker)
    graph.add_node("reducer", reducer_subgraph)
    graph.add_edge(START, "router")
    graph.add_conditional_edges("router", route_after_router, ["research", "orchestrator"])
    graph.add_edge("research", "orchestrator")
    graph.add_conditional_edges("orchestrator", fanout, ["worker"])
    graph.add_edge("worker", "reducer")
    graph.add_edge("reducer", END)
    return graph.compile(checkpointer=build_checkpointer())


workflow = build_workflow()


def generate_blog(topic: str, thread_id: str) -> dict:
    return workflow.invoke(
        {"topic": topic, "sections": [], "generation_id": thread_id},
        config={"configurable": {"thread_id": thread_id}},
    )
