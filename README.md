# Agent Writer

Agent Writer is an end-to-end technical writing system built with LangGraph and FastAPI. A topic enters the graph, gets classified by research need, becomes a structured article plan, fans out into parallel section-writing workers, and returns as a Markdown article with generated diagrams placed in the relevant sections.

The project includes a responsive web interface inspired by my illustrated portfolio. The UI shows the workflow stages, renders the final Markdown, displays generated images, and provides the saved article as a download.

## Features

- **Research-aware routing** — decides whether a topic can be handled from model knowledge or needs current web research.
- **Focused search queries** — creates targeted Tavily searches for current topics instead of broad, low-signal queries.
- **Structured planning** — produces a validated article plan with section goals, bullet points, word targets, and section types.
- **Parallel section writing** — uses LangGraph `Send` to fan tasks out to multiple workers and merge their output in the original order.
- **Evidence handling** — normalizes, filters, and deduplicates research results before the writing stage.
- **Image planning** — reviews the complete article and chooses up to three diagrams that add real explanatory value.
- **Section-aware placement** — ties each image to an existing Markdown heading and inserts it into the correct section.
- **Image generation** — creates images through OpenRouter's image API and stores them locally.
- **Markdown persistence** — saves completed articles with Windows-safe filenames under `outputs/`.
- **Checkpointing** — uses PostgreSQL when configured and falls back to LangGraph's in-memory saver for local development.
- **Portfolio UI** — includes progress states, article preview, generated images, error handling, responsive layout, and Markdown downloads.

## How It Works

```mermaid
flowchart LR
    A[Topic] --> B[Router]
    B -->|Current topic| C[Researcher]
    B -->|Stable topic| D[Orchestrator]
    C --> D
    D --> E{Fan out tasks}
    E --> W1[Section worker 1]
    E --> W2[Section worker 2]
    E --> W3[Section worker N]
    W1 --> F[Merge sections]
    W2 --> F
    W3 --> F
    F --> G[Image planner]
    G --> H[Generate and place images]
    H --> I[Save Markdown]
    I --> J[Web preview and download]
```

### LangGraph State Flow

```mermaid
stateDiagram-v2
    [*] --> Router
    Router --> Researcher: needs_research = true
    Router --> Orchestrator: needs_research = false
    Researcher --> Orchestrator
    Orchestrator --> Worker: Send one task per section
    Worker --> Reducer: append (task_id, markdown)
    Reducer --> MergeContent
    MergeContent --> DecideImages
    DecideImages --> GenerateAndPlaceImages
    GenerateAndPlaceImages --> [*]
```

The shared state contains the topic, routing decision, research queries, evidence, plan, generated sections, merged Markdown, image specifications, final Markdown, and output path. Section results use an `operator.add` reducer and carry their task IDs so parallel execution does not change article order.

## Technology Stack

| Layer | Technology | Purpose |
|---|---|---|
| Backend | Python 3.13 | Workflow and API implementation |
| API | FastAPI + Uvicorn | HTTP routes, templates, static files, and generation endpoint |
| Agent orchestration | LangGraph | Stateful routing, fan-out workers, reducers, and checkpointing |
| LLM integration | LangChain OpenAI | Structured output and OpenRouter-compatible chat requests |
| Language model | `gpt-4o-mini` through OpenRouter | Routing, planning, evidence extraction, writing, and image planning |
| Web research | Tavily | Current web results for time-sensitive topics |
| Validation | Pydantic | Plans, tasks, evidence, API payloads, and image specifications |
| Image generation | OpenRouter Image API | Generates article diagrams with `bytedance-seed/seedream-4.5` |
| Persistence | PostgreSQL / LangGraph MemorySaver | Durable production checkpoints or local fallback |
| Frontend | Jinja2, HTML, CSS, JavaScript | Responsive workflow UI and Markdown preview |

## Project Structure

```text
agent wtiter/
├── app.py                 # FastAPI routes and static file mounts
├── backend.py             # LangGraph workflow and generation logic
├── templates/
│   └── index.html         # Portfolio-themed UI and Markdown renderer
├── notebooks/
│   └── basic.ipynb        # Original workflow experiments
├── images/                # Generated article images
├── outputs/               # Completed Markdown articles
├── requirements.txt
├── pyproject.toml
└── README.md
```

## Setup

### 1. Clone and enter the project

```powershell
git clone <repository-url>
cd "agent wtiter"
```

### 2. Create a virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 3. Install dependencies

Using `uv`:

```powershell
uv sync
```

Or using `pip`:

```powershell
pip install -r requirements.txt
```

For PostgreSQL checkpointing, install the project dependencies from `pyproject.toml` because they include `psycopg` and `langgraph-checkpoint-postgres`.

### 4. Configure environment variables

Create a `.env` file in the project root:

```dotenv
OPENROUTER_API_KEY=your_openrouter_key
TAVILY_API_KEY=your_tavily_key

# Optional: enables durable LangGraph checkpoints
EXTERNAL_DATABASE_URL=postgresql://user:password@host/database
```

If `EXTERNAL_DATABASE_URL` is missing or unavailable, the application automatically uses in-memory checkpointing.

### 5. Run the application

```powershell
.\.venv\Scripts\uvicorn.exe app:app --reload --port 8001
```

Open [http://127.0.0.1:8001](http://127.0.0.1:8001).

## API

### Generate an article

```http
POST /api/generate
Content-Type: application/json
```

Request:

```json
{
  "topic": "How LangGraph coordinates parallel AI agents"
}
```

Response:

```json
{
  "markdown": "# Generated article...",
  "output_url": "/outputs/generated-article.md",
  "thread_id": "unique-checkpoint-thread"
}
```

Each browser request receives a new thread ID so a previous article's reducer state cannot leak into a new run.

### Health check

```http
GET /health
```

## Generated Files

- Completed articles are written to `outputs/`.
- Generated diagrams are written to `images/`.
- Markdown image links use `../images/<filename>`, which is correct relative to files inside `outputs/`.
- FastAPI exposes both directories through `/outputs` and `/images`, allowing the browser preview to render the same assets.

## Workflow Details

### 1. Router

Classifies the topic as `closed_book`, `hybrid`, or `open_book`. Current topics produce focused research queries, while stable topics skip directly to planning.

### 2. Researcher

Calls Tavily for each query, normalizes the results, asks the model to retain useful evidence, and deduplicates sources by URL.

### 3. Orchestrator

Creates five to seven article sections. Every task includes a unique ID, title, goal, section type, bullet list, and target word count.

### 4. Workers

LangGraph sends every task to the same worker node in parallel. Each worker writes one section and returns `(task_id, markdown)` so the reducer can restore the planned order.

### 5. Reducer and Image Planner

The reducer merges the sections into one article. The image planner receives the available H2 headings and must attach every requested image to an exact heading. Missing placeholders are inserted directly under that heading before replacement.

### 6. Image Generation and Save

The image generator calls OpenRouter, writes the returned base64 image bytes to disk, replaces placeholders with Markdown image syntax, and saves the final article. Image failures are converted into readable Markdown notices instead of losing the whole article.

## Notes for Development

- Use a fresh checkpoint thread when changing the shape of `State` during notebook experiments.
- Rebuild the compiled graph after changing any node function.
- Image generation only runs when the image planner returns at least one `ImageSpec`.
- The browser progress indicator represents the workflow stages while the synchronous backend request is running.

## Portfolio Context

This project demonstrates practical experience with:

- agent routing and stateful workflows;
- structured LLM outputs;
- parallel task execution and reducers;
- external research and image APIs;
- checkpoint persistence;
- FastAPI backend development;
- responsive frontend implementation; and
- failure handling across multi-step AI systems.
