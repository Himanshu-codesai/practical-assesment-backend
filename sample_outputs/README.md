Outputs from the system, one folder per video:

- `1_video.json` summary and topics
- `2_search.json` retrieval results
- `3_ask.json` a grounded answer with citations
- `4_assessment.json` the generated questions
- `5_result.json` per-question score, correctness and feedback
- `6_report.json` the learning report

`photosynthesis_dr_binocs/` is a real run on the YouTube video
"Photosynthesis | The Dr. Binocs Show" (https://www.youtube.com/watch?v=D1Ymc311XS8).
The summary, topics, questions, grading and report were all produced by the system.
The learner's four answers were typed by hand as a sample: two right, one partly
right and one wrong, so the report has something to analyse (score 62.5%).

`video_8/` to `video_12/` are one run of `python scripts/demo.py` over all five entries in
`sample_data/videos.json`: four YouTube videos and one audio file transcribed by Whisper.
The learner there is simulated (always the first option, and one vague written answer),
so the scores are low on purpose: 0% to 25%.

`python scripts/demo.py` writes new folders here, named `video_<id>`.
