# Sample outputs

Three real runs of the system, one folder each. Everything in them was produced by the system: transcript, topics, search results, answers, questions, grading and report. Only the learner's answers were typed by hand, chosen to give three different results.

| Folder | Video | Length | Transcript | Questions | Score |
|---|---|---|---|---|---|
| `heart_and_circulatory_system/` | [The Heart and Circulatory System - How They Work](https://www.youtube.com/watch?v=CWFyxn0qDEU) (Mayo Clinic) | 3 min | YouTube captions | 4 | 100% |
| `machine_learning_concepts/` | [All Machine Learning Concepts Explained in 22 Minutes](https://www.youtube.com/watch?v=Fa_V9fP2tpU) (Infinite Codes) | 22 min | YouTube captions | 8 | 62.5% |
| `psychology_of_not_posting/` | [The Psychology of People Who Don't Post on Social Media](https://www.youtube.com/watch?v=5pcw25mQxCg) (Dr. Sahar Yousef) | 13 min | Whisper (speech to text) | 8 | 34.4% |

What each one shows:

- **Heart:** a short video. Two topics, a short quiz, and a report with strengths and no weaknesses.
- **Machine learning:** a long video. Six topics, the full eight questions, and a report with strong and weak topics and rewatch timestamps.
- **Psychology:** a non-science talk transcribed from the audio by Whisper. A low score, so the report lists what to rewatch.

## Files in each folder

| File | Endpoint | What it is |
|---|---|---|
| `1_video.json` | `GET /videos/{id}` | The processed video: summary and topics |
| `2_search.json` | `POST /videos/{id}/search` | Retrieval results for one query |
| `3_ask.json` | `POST /videos/{id}/ask` | A grounded answer with citations |
| `4_assessment.json` | `GET /assessments/{id}` | The generated questions |
| `5_result.json` | `GET /assessments/{id}/result` | Score, correctness, feedback and areas of improvement per question |
| `6_report.json` | `GET /assessments/{id}/report` | The learning report |

The inputs (the learner's answers) are in `5_result.json`, next to the correct answers.

`python scripts/demo.py` writes new folders here, named `video_<id>`, with a simulated learner.
