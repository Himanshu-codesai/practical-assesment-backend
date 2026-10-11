# Architecture

## 1. How the system is built

![How the system is built](diagrams/architecture.png)

| Part | What it does |
|---|---|
| API | Validates requests, reads and writes rows, puts jobs on the queue, serves search and `/ask`. Never runs slow work. |
| Job queue (Redis) | Holds waiting jobs, saved to disk. Carries only job ids. |
| Worker (Celery) | Runs the three job kinds: `ingest`, `generate_assessment`, `evaluate`. Same Docker image as the API, different command. |
| Database (PostgreSQL) | All business data, including chunk text and job state. The source of truth. |
| Search engine (Qdrant) | Two vectors per chunk (meaning and keywords). Can be rebuilt from PostgreSQL. |
| AI model gateway | Answers `POST /v1/messages` by running the Claude command-line tool. Reachable only from the API and the worker. |
| Backup AI server (APIYI) | A hosted Claude API. Asked only when the gateway fails. |
| Speech to text (Whisper) | Runs inside the worker, for videos without captions. |

## 2. What happens, step by step

![What happens, step by step](diagrams/how-it-works.png)

Steps 1 to 5 are one background job (`ingest`). Step 7 is a second job (`generate_assessment`), and steps 9 and 10 are a third (`evaluate`). Each job reports its current stage in the `jobs` table, so a client can poll `GET /jobs/{id}`.

Every job is safe to run twice: the transcript is stored once and reused, and chunks, vectors and questions are deleted and rebuilt. That is what makes retries safe.

## 3. Jobs and retries

A job moves through `queued` → `running` → `succeeded`, or `failed`.

- **Retried automatically** (up to 3 times, waiting 10, 20, then 40 seconds): AI rate limit or server error, network errors, Qdrant unavailable, YouTube unreachable, too few valid questions generated.
- **Not retried:** a request the AI server rejects, an empty transcript, a private video.
- A job is acknowledged only after it finishes (`acks_late`), so a killed worker's job returns to the queue.
- The API commits the `jobs` row before putting the job on the queue, so the worker never looks up a row that is not there yet.
- A failed job can be run again with `POST /jobs/{id}/retry`.
- The HTTP request ID is passed to the job and written on the worker's logs, so one ID traces a request across both programs.

## 4. Hybrid retrieval

A search runs two retrievers and merges their results:

- **Dense** (meaning) finds passages that say the same thing as the query in different words.
- **Sparse, BM25** (keywords) finds passages that contain the query's exact words: names, numbers, acronyms.
- **RRF** (Reciprocal Rank Fusion) merges the two ranked lists. Each chunk scores the sum of `1 / (k + rank)` over the lists. It uses ranks, not raw scores, so the two retrievers need no score calibration.

The method can be chosen per request (`"mode": "hybrid" | "dense" | "keyword"`). `scripts/eval_retrieval.py` compares the three over a small question set.

- **RAG (`/ask`):** the top chunks go into the prompt with `[chunk N | topic | mm:ss-mm:ss]` headers. The model must answer from them only, say so if they do not contain the answer, and list the chunks it used. Those come back as citations with timestamps.
- **Assessment generation** uses the same search per topic, so questions cover the whole video and stay grounded in it.

## 5. Database

| Table | Holds |
|---|---|
| `learners` | A learner's id and name |
| `videos` | Title, source, status, the cached transcript, summary, topics |
| `chunks` | Pieces of the transcript with start and end time and a topic. The chunk id is also the Qdrant point id |
| `jobs` | One row per background job: kind, status, stage, error, attempts |
| `watch_progress` | How much of a video a learner has watched |
| `assessments` | One per learner and video, with its status |
| `questions` | Type, thinking level, topic, prompt, options, answer key, rubric, source chunk |
| `answers` | The learner's answer, plus score, correctness, feedback, areas to improve |
| `reports` | The learning report for an assessment |
| `cached_answers` | Saved replies of `/ask`, so a repeated question is answered once |

Deleting a video removes its chunks, jobs, progress, assessments, questions, answers, reports and cached answers (foreign keys with `ON DELETE CASCADE`).

The exact table definitions are in [`schema.sql`](schema.sql), generated from `app/models.py`.

Qdrant collection `video_chunks`: named vector `dense` (384, cosine), sparse vector `bm25` (IDF modifier), payload `{video_id, chunk_index}` with an index on `video_id`.

## 6. Status values

| Entity | Values |
|---|---|
| Video | `pending` → `processing` → `ready` (or `failed`) |
| Job | `queued` → `running` → `succeeded` (or `failed`) |
| Assessment | `generating` → `ready` → `submitted` → `evaluated` (or `failed`) |
