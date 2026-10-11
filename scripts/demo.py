"""End-to-end demo against a running API. Saves every response as a JSON file.

    python scripts/demo.py                     # all videos in sample_data/videos.json
    python scripts/demo.py --interactive       # answer the questions yourself
    python scripts/demo.py --base-url http://localhost:8001   # the default

Without --interactive the learner is simulated: it picks the first option of every
objective question and gives a deliberately vague written answer, so the report has
both right and wrong answers to analyse. Only needs `pip install httpx`.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
VAGUE_ANSWER = ("I think it is about how the parts connect together, "
                "but I am not sure of the details.")


def save(folder: Path, name: str, data) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"    saved {folder.name}/{name}")


def call(client: httpx.Client, method: str, path: str, **kwargs):
    # GETs are safe to repeat, so a dropped connection while polling is retried.
    attempts = 3 if method == "GET" else 1
    for attempt in range(1, attempts + 1):
        try:
            response = client.request(method, path, **kwargs)
            break
        except httpx.TransportError as exc:
            if attempt == attempts:
                sys.exit(f"{method} {path} could not reach the API: {exc!r}")
            time.sleep(2)
    if response.status_code >= 400:
        sys.exit(f"{method} {path} failed with {response.status_code}: {response.text}")
    return response.json()


def register(client: httpx.Client, video: dict, folder: Path) -> dict:
    """A video entry has either a "source_url" or a "file" to upload from the same folder."""
    if "file" not in video:
        return call(client, "POST", "/videos", json=video)
    path = folder / video["file"]
    with path.open("rb") as handle:
        return call(client, "POST", "/videos/upload", data={"title": video["title"]},
                    files={"file": (path.name, handle)})


def wait_for_job(client: httpx.Client, job_id: int, timeout: int = 1800) -> dict:
    deadline = time.time() + timeout
    last_stage = None
    while time.time() < deadline:
        job = call(client, "GET", f"/jobs/{job_id}")
        if job["stage"] != last_stage:
            print(f"    job {job_id} [{job['kind']}]: {job['status']} / {job['stage']}")
            last_stage = job["stage"]
        if job["status"] == "succeeded":
            return job
        if job["status"] == "failed":
            sys.exit(f"Job {job_id} failed: {job['error']}")
        time.sleep(2)
    sys.exit(f"Job {job_id} did not finish within {timeout} seconds.")


def collect_answers(questions: list[dict], interactive: bool) -> list[dict]:
    answers = []
    for q in questions:
        if not interactive:
            answers.append({"question_id": q["id"], "selected_option": 0} if q["options"]
                           else {"question_id": q["id"], "text": VAGUE_ANSWER})
            continue
        print(f"\n  Q{q['position'] + 1} [{q['type']}, {q['topic']}] {q['prompt']}")
        if q["options"]:
            for i, option in enumerate(q["options"]):
                print(f"      {i}) {option}")
            raw = input("  option number (blank to skip): ").strip()
            answers.append({"question_id": q["id"],
                            "selected_option": int(raw) if raw.isdigit() else None})
        else:
            answers.append({"question_id": q["id"], "text": input("  your answer: ").strip()})
    return answers


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:8001")
    parser.add_argument("--videos", default=str(ROOT / "sample_data" / "videos.json"))
    parser.add_argument("--out", default=str(ROOT / "sample_outputs"))
    parser.add_argument("--learner", default="Demo Learner")
    parser.add_argument("--question", default="What is the main idea of this video?")
    parser.add_argument("--interactive", action="store_true")
    args = parser.parse_args()

    videos = json.loads(Path(args.videos).read_text(encoding="utf-8"))
    out = Path(args.out)

    with httpx.Client(base_url=args.base_url, timeout=300) as client:
        health = call(client, "GET", "/health")
        print(f"API is {health['status']} (model: {health['llm_model']})")
        learner = call(client, "POST", "/learners", json={"name": args.learner})

        # Register every video first: they are processed in the background, concurrently.
        print(f"\n1. Registering {len(videos)} videos")
        accepted = [register(client, v, Path(args.videos).parent) for v in videos]

        for item in accepted:
            video_id = item["video"]["id"]
            folder = out / f"video_{video_id}"
            print(f"\n2. Processing video {video_id}: {item['video']['title']}")
            wait_for_job(client, item["job"]["id"])
            video = call(client, "GET", f"/videos/{video_id}")
            save(folder, "1_video.json", video)

            print("3. Retrieval")
            topic = video["topics"][0]["name"]
            save(folder, "2_search.json",
                 call(client, "POST", f"/videos/{video_id}/search",
                      json={"query": topic, "top_k": 3}))
            save(folder, "3_ask.json",
                 call(client, "POST", f"/videos/{video_id}/ask", json={"question": args.question}))

            print("4. Watch progress: 40%, then 90% (crosses the threshold)")
            call(client, "POST", f"/videos/{video_id}/progress",
                 json={"learner_id": learner["id"], "percent_watched": 40})
            progress = call(client, "POST", f"/videos/{video_id}/progress",
                            json={"learner_id": learner["id"], "percent_watched": 90})
            assessment_id = progress["assessment_id"]
            wait_for_job(client, progress["job_id"])
            assessment = call(client, "GET", f"/assessments/{assessment_id}")
            save(folder, "4_assessment.json", assessment)

            print(f"5. Submitting answers to {len(assessment['questions'])} questions")
            answers = collect_answers(assessment["questions"], args.interactive)
            submitted = call(client, "POST", f"/assessments/{assessment_id}/submit",
                             json={"answers": answers})
            wait_for_job(client, submitted["job"]["id"])

            result = call(client, "GET", f"/assessments/{assessment_id}/result")
            save(folder, "5_result.json", result)
            report = call(client, "GET", f"/assessments/{assessment_id}/report")
            save(folder, "6_report.json", report)
            print(f"   Score: {result['score']}/{result['max_score']} ({result['percentage']}%)")

    print(f"\nDone. Outputs are in {out}")


if __name__ == "__main__":
    main()
