# AI LMS Backend

Backend for an AI-powered Learning Management System. It processes educational videos, answers questions from them, generates a quiz when a learner finishes watching, grades the answers and writes a learning report.

**Stack:** FastAPI, Celery + Redis, PostgreSQL, Qdrant, Whisper, Claude.

## Run it

You need Docker and one key for the language model.

```bash
cp .env.example .env          # set LLM_FALLBACK_API_KEY or CLAUDE_CODE_OAUTH_TOKEN
docker compose up --build -d
```

| | |
|---|---|
| Test screen | http://localhost:8001/ui |
| API docs (Swagger) | http://localhost:8001/docs |
| Health | http://localhost:8001/health |

Run the full flow on the sample videos and save the outputs:

```bash
pip install httpx
python scripts/demo.py
```

Run the tests (no network or model needed):

```bash
docker compose run --rm --no-deps api pytest -q
```

## What it does

| Requirement | How | Code |
|---|---|---|
| Video processing | YouTube captions, or Whisper on the audio, or an uploaded file | `app/pipeline/transcript.py` |
| AI pipeline | chunk (about 180 words, with timestamps) → topics by the LLM → embeddings → index | `app/pipeline/` |
| Knowledge retrieval | Hybrid search (meaning + keywords, fused with RRF) and RAG answers with citations | `app/retrieval/store.py`, `app/services/qa.py` |
| Assessment generation | Triggered at a configurable watch threshold. Four question types, tagged by topic and Bloom level | `app/services/assessment.py` |
| Answer evaluation | Choice questions graded in code; written answers graded by the LLM against a rubric | `app/services/evaluation.py` |
| Learning report | Scores computed in code; summary, strengths, weaknesses and suggestions written by the LLM | `app/services/report.py` |
| Background processing | Celery worker, two queues, automatic retry, job status endpoint | `app/worker.py` |
| Logging and errors | JSON logs with a request ID; one error format | `app/main.py` |

## Architecture

![How the system is built](docs/diagrams/architecture.png)

![What happens, step by step](docs/diagrams/how-it-works.png)

More detail: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Database schema: [`docs/schema.sql`](docs/schema.sql).

## API flow

1. `POST /videos` or `/videos/upload` → returns `202` with a job. Poll `GET /jobs/{id}`.
2. `POST /videos/{id}/search` and `/ask` to query the video.
3. `POST /videos/{id}/progress`. At the threshold the reply carries an `assessment_id`.
4. `GET /assessments/{id}` → `POST /assessments/{id}/submit`.
5. `GET /assessments/{id}/result` and `/report`.

All endpoints: [`docs/API.md`](docs/API.md) and [`docs/openapi.json`](docs/openapi.json).

## Key decisions

| Decision | Why | Cost |
|---|---|---|
| Queue and worker, not tasks inside the API | Slow work survives a crash and is retried; the API stays fast | Two more services |
| Captions first, Whisper as fallback | Captions are instant and free; Whisper covers videos without them | Whisper is slow on CPU |
| Hybrid retrieval | Meaning search handles different wording; keyword search handles names and numbers | Two vectors per chunk |
| A context line in front of each chunk before embedding | A chunk keeps its video and topic | No extra LLM call. No measurable gain yet on my small tests |
| PostgreSQL is the source of truth, Qdrant holds only vectors | The index can be rebuilt at any time | Two stores to keep in step |
| Every LLM reply is validated against a schema | Bad output is corrected once or rejected | One extra call when it fails |
| Code grades choice questions, the LLM grades written ones | Exact and free where possible; judgement only where needed | LLM grading is not yet checked against human grading |
| Report numbers from code, words from the LLM | The LLM cannot invent a score | None |
| Plain HTTP LLM client with a fallback server | Changing model or provider is a settings change | No SDK extras such as streaming |

Full reasoning, measurements and the simpler design I compared against: [`docs/DECISIONS.md`](docs/DECISIONS.md).

## Configuration

Settings are environment variables in `.env`. The main ones:

| Variable | Default | Meaning |
|---|---|---|
| `LLM_FALLBACK_API_KEY` | – | Key for the hosted Claude API (APIYI) |
| `CLAUDE_CODE_OAUTH_TOKEN` | – | Sign-in token for the local `llm` gateway (`claude setup-token`) |
| `LLM_MODEL` | `claude-opus-5-5` | Model name |
| `WHISPER_MODEL` | `base` | `tiny` to `large-v3` |
| `RETRIEVAL_MODE` | `hybrid` | `hybrid`, `dense` or `keyword` |
| `COMPLETION_THRESHOLD` | `80` | Percent watched that triggers the quiz |
| `QUESTIONS_PER_ASSESSMENT` | `8` | Maximum questions |

The backend sends every LLM call to the `llm` gateway first and to the fallback server if that fails. Either key alone is enough. Keep `.env` private.

## Deliverables

| Deliverable | Where |
|---|---|
| Source code | `app/`, `llm-proxy/` |
| Setup instructions | this file |
| Architecture diagram | `docs/diagrams/`, `docs/ARCHITECTURE.md` |
| API documentation | `docs/API.md`, `docs/openapi.json`, Swagger at `/docs` |
| Database schema | `docs/schema.sql` |
| Docker configuration | `Dockerfile`, `llm-proxy/Dockerfile`, `docker-compose.yml` |
| Sample data | `sample_data/` |
| Sample outputs | `sample_outputs/` |
| Decisions and assumptions | this file, `docs/DECISIONS.md` |

## Assumptions

- Videos are in English and the spoken words carry the teaching. Slides are not read.
- No login. A `learner_id` is passed in requests; the test screen has a learner picker.
- Watch progress is reported by the client player.
- One assessment per learner and video. Retake replaces it.
- Tables are created at start-up, without a migration tool.

## Limitations

- Whisper on CPU is slow for long videos.
- A video of several hours would not fit the single topics prompt.
- No reranker after fusion.
- The retrieval check is a small smoke test, not a benchmark.
- LLM grading has not been validated against human graders.
