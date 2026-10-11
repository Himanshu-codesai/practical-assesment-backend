# Decisions and trade-offs

The reasoning behind the design, with what was measured. The README has the short version.

## Engineering decisions

**A queue and workers instead of in-process tasks.** Transcription and LLM calls take from seconds to minutes. The API only writes a `jobs` row and puts its id on Redis, so it stays fast and can be scaled or redeployed without touching running work. Celery is configured with `acks_late`, so a job is removed from the queue only when it finishes; a worker that dies mid-job hands it back. Failures marked retryable (rate limits, network errors, a Qdrant outage, too few valid questions) are retried automatically with exponential backoff (10 s, 20 s, 40 s). Permanent failures (a private video, a rejected request) fail at once. Every job is idempotent, which is what makes redelivery and retry safe.

**Two queues.** Ingestion is CPU-bound (Whisper, embeddings); assessment and evaluation are I/O-bound (LLM calls). Separate queues mean a backlog of long videos cannot delay a learner waiting for a grade, and each can later get its own worker pool.

**Captions first, Whisper as the fallback, and the caller can choose.** Captions take seconds and cost nothing, so `auto` uses them when a YouTube video has them. Anything else is transcribed locally with faster-whisper, which gives timestamps and has no per-minute cost. On the development laptop's CPU the `base` model transcribed a 6-minute video in about 2 minutes. Forcing `whisper` on a captioned video is useful for comparing the two; on the same video Whisper misheard a name ("McConee" for "McConaughey") that the captions had right. The transcript is cached on the video row, so it is never produced twice.

**Hybrid retrieval.** Dense embeddings match meaning: a question about "squeezing a value between 0 and 1" finds the sigmoid passage without sharing its words. BM25 matches exact tokens that embeddings blur: names, numbers, acronyms such as "ReLU" or "784". Learner questions come in both forms, so both retrievers run and their ranked lists are merged with Reciprocal Rank Fusion. RRF uses ranks only, so cosine scores and BM25 scores never need to be put on the same scale. Qdrant does this in one request with two named vectors per point.

*Measured, with a caveat:* `scripts/eval_retrieval.py` runs 15 hand-written questions over the three sample videos (35 chunks). Hybrid and keyword put the right chunk in the top 3 every time; dense missed one (hit@1: hybrid 0.80 to 0.87, dense 0.73, keyword 0.87). Hybrid has a range because for one question two chunks tie exactly after fusion (first in one list and second in the other), and the order of a tie can change between runs. The dense miss shows why both retrievers run: "Which kind of learning is compared to training a pet?" is not in the dense top 3, but hybrid ranks it first because the keyword side finds the exact phrase. With only 2 to 21 chunks per video, and test cases defined by a phrase the right chunk contains, this set is too small and too keyword-friendly to rank the modes. It confirms the retrieval works; it does not show that hybrid is better. That would need a larger corpus and paraphrased questions.

**Context is added to each chunk before embedding (contextual retrieval).** A chunk on its own can lose its surroundings: "this is why it works" does not say what "this" is. So the text that is embedded is the chunk with one line in front, `Video: <title>. Topic: <topic>. <topic summary>`. It needs no extra model call, because the title and topics are already known. Only the search copy gets the line; the stored chunk, and every citation a learner sees, stays the speaker's own words (`text_for_search` in `app/retrieval/store.py`). Measured twice by indexing the same chunks with and without the line, and the two results disagree. On an earlier set of videos (48 chunks, 11 questions) hybrid hit@1 rose from 0.91 to 1.00. On the current sample videos (35 chunks, 15 questions) hybrid stayed inside its tie range (0.80 to 0.87) either way, and dense fell from 0.80 to 0.73. Each change is one question moving one rank, so at this size there is no measurable effect in either direction. A likely reason it can hurt: each chunk carries a single topic label, so a chunk at a topic change gets the wrong topic in its line. I kept it because it costs nothing, and I would re-measure on a larger set before relying on it.

**PostgreSQL is the source of truth; Qdrant holds only vectors.** A Qdrant point's id is the chunk's primary key and its payload is just `video_id`. Text, timestamps and topics stay in PostgreSQL, so the vector index can be rebuilt from the database at any time.

**Local embeddings.** `bge-small-en-v1.5` (dense) and `Qdrant/bm25` (sparse) run through fastembed on CPU: no second API key, no per-call cost. A search calls no large language model.

**Validated JSON from every LLM call.** Each call names a Pydantic model. Its JSON schema is written into the prompt, the reply is validated against the model, and if validation fails the error is sent back once for a correction. Rules a schema cannot express (four options, a valid answer index, a real source chunk) are checked in code and bad questions are dropped.

**Understanding over memorisation.** The generation prompt asks for Bloom levels above "remember", plausible wrong options, and scenario questions that apply the idea to a new case, all in short plain language. The number of questions scales with the video's length. Each question stores its Bloom level, so the report also shows performance by skill.

**Two grading strategies.** Multiple-choice and true/false are graded in code: exact, free and repeatable. Empty answers and replies like "idk" also score zero in code. Written answers go to the LLM in one batched call, with the rubric, a model answer and the source transcript chunk. The LLM returns a score; the correct / partially correct / incorrect label is derived from the score in code so the two cannot disagree. Any credit (one rubric point of four or more) is "partially correct". The learner's text is wrapped in tags and the prompt tells the model to treat it as data, which guards against "give me full marks" injections.

**Numbers from code, words from the LLM.** The report's scores, topic table and skill table are computed from stored grades. The LLM only writes a short narrative on top of them, and is told to claim nothing the data does not show.

**Speed.** Retrieval takes about a tenth of a second. The models are loaded at start-up (`app/warmup.py`) so the first request does not wait, and an answer to a question that was already asked on a video is returned from a cache table.

**One small LLM client, no SDK, with a fallback.** The application calls one method, `generate_structured(...)`, in `app/llm/client.py`: a plain HTTP request, about 100 lines, with its own error mapping (which failures are worth retrying). Changing the model server is a settings change, and a second server can be set as a fallback for when the first fails. Prompts are Markdown files in `app/llm/prompts/`.

**Two things testing found.** A newer PyAV release removed an argument faster-whisper passes, so audio decoding failed; the version is pinned in `requirements.txt`. And Qdrant closes idle HTTP connections with a `408`, so after a long Whisper run the next request failed although Qdrant was healthy; the store now retries once on a fresh connection (`_call` in `app/retrieval/store.py`).

## A simpler alternative I considered

The same domain code can run with far fewer parts, and I built that version first:

| | Simpler alternative | This project |
|---|---|---|
| Background jobs | FastAPI `BackgroundTasks` inside the API process | Celery worker + Redis queue |
| If the process dies mid-job | The job is lost and must be retried by hand | The job returns to the queue and retries itself |
| Vector store | pgvector column in PostgreSQL | Qdrant |
| Search | Dense only | Hybrid (dense + BM25), mode selectable |
| Transcription | Captions or a transcript file | Also Whisper on audio and video |
| Containers | 2 (plus the LLM gateway) | 5 (plus the LLM gateway) |

The simpler version is the right choice for a pilot: one database, nothing to keep in sync, and a chunk and its vector are written in one transaction. I chose the fuller design because the brief asks for background processing of long-running tasks and scalable design, and because it can process videos that have no captions. The cost is three more services to run. The code is arranged so the swap is local: only `app/tasks.py` and `app/worker.py` (jobs), `app/retrieval/store.py` (search) and `app/pipeline/transcript.py` (transcripts) would change.

## How it scales

- **API:** stateless, so add replicas behind a load balancer.
- **Workers:** `--scale worker=N`; split the `ingest` and `assess` queues into separate pools; move Whisper to GPU workers.
- **PostgreSQL / Qdrant / Redis:** each can move to a managed service without code changes (they are URLs in the environment).
- **LLM cost:** one outline call per video, one generation call per assessment, one grading call and one report call per submission. Objective questions cost nothing to grade, and repeated questions are served from the cache.

## The LLM server in detail

All language-model calls go to one address, `LLM_BASE_URL`, as plain HTTP requests (`POST /v1/messages` with `model`, `system` and `messages`; the reply's text is read from `content`). There is no vendor SDK in the backend.

By default that address is the `llm` service in `docker-compose.yml`: a small gateway (`llm-proxy/server.mjs`, about 80 lines) that answers each request by running the Claude command-line tool once. It signs in with a token you create once:

```bash
claude setup-token            # prints a token; put it in .env as CLAUDE_CODE_OAUTH_TOKEN
```

Keep `.env` private. It is excluded from git and from the Docker image; do not include it when sharing the project.

To use a different model server, change three lines in `.env`. Any server with the same request and reply shape works:

```bash
LLM_BASE_URL=http://llm:8787    # the gateway service (the default)
LLM_API_KEY=                    # sent as x-api-key when set
LLM_MODEL=claude-opus-5-5
```

The backend asks only for plain text back: the JSON schema goes into the prompt and the reply is validated in `app/llm/client.py`. That is why the server needs no special structured-output feature.

The gateway starts a process for every request, which adds about 8 seconds to each model call. It is the simplest way to run this without paid API credits, not the fastest.

**Fallback server.** If the primary server fails for any reason (unreachable, an error, no token), the same request is sent to a second server:

```bash
LLM_FALLBACK_BASE_URL=https://api.apiyi.com   # a hosted Claude API (APIYI); empty turns the fallback off
LLM_FALLBACK_API_KEY=...
LLM_FALLBACK_MODEL=claude-opus-5-5
```

Each `llm call` log line says which server answered. I tested this by stopping the gateway container during a question: the primary failed and the fallback answered. With only the fallback key set, the project still runs; every call simply goes to the fallback.
