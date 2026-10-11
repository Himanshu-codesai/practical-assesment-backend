"""Small retrieval check: does the right part of the video come back for a question?

    python scripts/eval_retrieval.py                    # compares hybrid, dense and keyword
    python scripts/eval_retrieval.py --modes hybrid     # one mode only

Each test case names a video (by a word in its title), a question, and terms that the
correct transcript chunk contains. A case is a hit at k if any of the top k chunks
contains one of those terms. Reports hit@1, hit@3 and MRR (mean reciprocal rank of the
first correct chunk). The videos must already be ingested (run scripts/demo.py first).

This is a smoke test on a handful of hand-written questions, not a benchmark.
"""
import argparse
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
TOP_K = 3


def first_hit_rank(results: list[dict], expected: list[str]) -> int | None:
    for rank, result in enumerate(results, 1):
        text = result["text"].lower()
        if any(term.lower() in text for term in expected):
            return rank
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:8001")
    parser.add_argument("--cases", default=str(ROOT / "sample_data" / "retrieval_eval.json"))
    parser.add_argument("--modes", default="hybrid,dense,keyword",
                        help="comma separated: hybrid, dense, keyword")
    args = parser.parse_args()
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    modes = args.modes.split(",")

    with httpx.Client(base_url=args.base_url, timeout=60) as client:
        videos = [v for v in client.get("/videos").json() if v["status"] == "ready"]
        ranks: dict[str, list[int | None]] = {mode: [] for mode in modes}
        used = []
        for case in cases:
            video = next((v for v in videos if case["video"].lower() in v["title"].lower()), None)
            if video is None:
                print(f"skipped (video not ingested): {case['question']}")
                continue
            used.append(case)
            for mode in modes:
                body = {"query": case["question"], "top_k": TOP_K, "mode": mode}
                response = client.post(f"/videos/{video['id']}/search", json=body)
                response.raise_for_status()
                ranks[mode].append(first_hit_rank(response.json()["results"], case["expect_any"]))

    if not used:
        sys.exit("No test case matched an ingested video.")

    print(f"\n{len(used)} questions, top_k={TOP_K}\n")
    print(f"{'mode':<10}{'hit@1':>8}{'hit@3':>8}{'MRR':>8}")
    for mode in modes:
        got = ranks[mode]
        hit1 = sum(r == 1 for r in got) / len(got)
        hit3 = sum(r is not None for r in got) / len(got)
        mrr = sum(1 / r for r in got if r) / len(got)
        print(f"{mode:<10}{hit1:>8.2f}{hit3:>8.2f}{mrr:>8.2f}")

    print("\nRank of the first correct chunk per question (- = not in the top results):")
    for i, case in enumerate(used):
        row = "  ".join(f"{mode}={ranks[mode][i] or '-'}" for mode in modes)
        print(f"  {row}   {case['question']}")


if __name__ == "__main__":
    main()
