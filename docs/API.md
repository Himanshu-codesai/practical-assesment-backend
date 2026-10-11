# API reference

Base URL: `http://localhost:8001`. Interactive docs: `/docs` (Swagger UI) and `/redoc`. Machine-readable spec: [`openapi.json`](openapi.json).

All bodies are JSON unless noted. Long-running operations return `202 Accepted` with a `job` object; poll `GET /jobs/{id}` until `status` is `succeeded` or `failed`.

## Endpoints

| Method | Path | Purpose | Success |
|---|---|---|---|
| POST | `/learners` | Create a learner | 201 |
| GET | `/learners` | List learners | 200 |
| DELETE | `/learners/{id}` | Delete the learner with their progress, assessments and reports. Videos stay | 204 |
| GET | `/learners/{id}/progress` | That learner's watch progress, assessment status and score per video | 200 |
| POST | `/videos` | Register a video by URL (YouTube or any site yt-dlp supports) and start processing. Optional `transcript_method`: `auto`, `captions` or `whisper` | 202 |
| POST | `/videos/upload` | Upload a media file (`.mp4`, `.mp3`, `.wav`, ...) or a transcript (`.srt`, `.vtt`, `.txt`) as `multipart/form-data`: `title`, `file` | 202 |
| GET | `/videos` | List videos | 200 |
| GET | `/videos/{id}` | Video status, summary and topics | 200 |
| GET | `/videos/{id}/transcript` | The extracted transcript line by line, with its source (captions, whisper, file) | 200 |
| DELETE | `/videos/{id}` | Delete the video with its chunks, vectors, progress, assessments and reports | 204 |
| POST | `/videos/{id}/search` | Retrieve the most relevant transcript chunks (`mode`: `hybrid`, `dense` or `keyword`) | 200 |
| POST | `/videos/{id}/ask` | Answer a question from the video, with citations. A repeated question returns the saved answer (`cached: true`) | 200 |
| POST | `/videos/{id}/progress` | Report watch progress; triggers the assessment at the threshold | 200 |
| GET | `/assessments` | List assessments (filters: `learner_id`, `video_id`) | 200 |
| GET | `/assessments/{id}` | Assessment and questions, without the answer key | 200 |
| POST | `/assessments/{id}/submit` | Submit answers for evaluation | 202 |
| POST | `/assessments/{id}/retake` | Discard the attempt and generate a new, different set of questions | 202 |
| GET | `/assessments/{id}/result` | Per-question score, correctness, feedback | 200 |
| GET | `/assessments/{id}/report` | The learning report | 200 |
| GET | `/jobs/{id}` | Status and stage of a background job | 200 |
| POST | `/jobs/{id}/retry` | Re-run a job that failed after its automatic retries | 202 |
| GET | `/health` | Liveness and dependency check (database, Redis, Qdrant) | 200 / 503 |

## Errors

Every error has the same shape. The `request_id` also appears in the `X-Request-ID` response header and on every log line for that request.

```json
{"error": {"code": "not_found", "message": "Video 99 not found.", "request_id": "72aa69f561fd"}}
```

| Status | Code | When |
|---|---|---|
| 404 | `not_found` | Unknown video, learner, assessment or job |
| 409 | `conflict` | Wrong state: video not ready, assessment already submitted, job not failed |
| 422 | `bad_request` | Invalid input (also FastAPI's own validation errors) |
| 502 | `llm_error` | The LLM call failed on a synchronous endpoint (`/ask`) |
| 500 | `internal_error` | Anything unexpected |

## Walkthrough

```bash
# 1. Create a learner
curl -X POST localhost:8001/learners -H 'content-type: application/json' \
  -d '{"name": "Asha Rao"}'

# 2. Register a video (returns video + job)
curl -X POST localhost:8001/videos -H 'content-type: application/json' \
  -d '{"source_url": "https://www.youtube.com/watch?v=aircAruvnKk", "title": "But what is a neural network?"}'

#    ...or upload an audio/video file (transcribed with Whisper) or a transcript
curl -X POST localhost:8001/videos/upload -F title="Photosynthesis" \
  -F file=@sample_data/photosynthesis_lecture.mp3

# 3. Poll the job
curl localhost:8001/jobs/1

# 4. Search and ask
curl -X POST localhost:8001/videos/1/search -H 'content-type: application/json' \
  -d '{"query": "what does the sigmoid function do", "top_k": 3, "mode": "hybrid"}'
curl -X POST localhost:8001/videos/1/ask -H 'content-type: application/json' \
  -d '{"question": "Why does the network need hidden layers?"}'

# 5. Report progress. At or above the threshold this creates the assessment.
curl -X POST localhost:8001/videos/1/progress -H 'content-type: application/json' \
  -d '{"learner_id": 1, "percent_watched": 90}'
#    -> {"assessment_id": 1, "assessment_status": "generating", "job_id": 2, ...}

# 6. Fetch the questions, then submit answers
curl localhost:8001/assessments/1
curl -X POST localhost:8001/assessments/1/submit -H 'content-type: application/json' \
  -d '{"answers": [
        {"question_id": 1, "selected_option": 2},
        {"question_id": 2, "text": "Because each layer combines the patterns found by the previous one."}
      ]}'

# 7. Results and report
curl localhost:8001/assessments/1/result
curl localhost:8001/assessments/1/report
```

## Key response shapes

`POST /videos/{id}/search`

```json
{
  "query": "what does the sigmoid function do",
  "mode": "hybrid",
  "results": [
    {"chunk_id": 12, "video_id": 1, "chunk_index": 11, "topic": "...",
     "start": "10:24", "end": "11:17",
     "url": "https://www.youtube.com/watch?v=aircAruvnKk&t=624s", "score": 0.5, "text": "..."}
  ]
}
```

`score` is the RRF score in hybrid mode, cosine similarity in dense mode, and the BM25 score in keyword mode.

A job that fails with a retryable error goes back to `queued`, and its `error` field says when it will be retried.

`GET /assessments/{id}` (a question)

```json
{"id": 3, "position": 2, "type": "mcq", "bloom_level": "apply", "topic": "...",
 "prompt": "...", "options": ["...", "...", "...", "..."]}
```

Question types: `mcq`, `true_false`, `short_answer`, `scenario`. Answer objective questions with `selected_option` (index into `options`) and written ones with `text`. Unanswered questions score zero.

`GET /assessments/{id}/result` (one entry)

```json
{"question_id": 3, "type": "mcq", "score": 1.0, "max_score": 1.0,
 "correctness": "correct", "feedback": "Correct. ...", "improvement_areas": []}
```

`correctness` is `correct`, `partially_correct` or `incorrect`.

`GET /assessments/{id}/report` (top-level keys)

```
assessment_id, learner, video, generated_at,
overall_performance   {score, max_score, percentage, level, questions, correct, partially_correct, incorrect}
topic_analysis        [{topic, questions, score, max_score, percentage, level}]
skill_analysis        [{bloom_level, ...same fields}]
strengths, weaknesses, improvement_suggestions, ai_summary
recommended_rewatch   [{topic, from, to, url}]
question_breakdown    [{prompt, learner_answer, correct_answer, correctness, score, feedback, ...}]
```

`level` is `strong` (80% and above), `developing` (50% and above) or `needs_work`.
