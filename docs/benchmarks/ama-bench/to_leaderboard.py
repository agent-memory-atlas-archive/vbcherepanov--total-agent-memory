#!/usr/bin/env python3
"""Turn run.py output into the HF leaderboard's submission JSONL.

The harness writes {"episode_id": int, "answer_list", "reasoning_trace"}; the leaderboard
(space AMA-bench/AMA-bench-Leaderboard, submission.py) requires episode_id as a string plus
question_uuid_list and llm_as_judge_score_list (bools), all lists the same length.
Scores come from the harness's results_*.json (the judge run); nothing is invented:
a question without a judge verdict stops the conversion.
"""

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--answers", required=True, help="answers_*.jsonl from src/run.py")
    parser.add_argument("--results", required=True, help="results_*.json from the same run (judge verdicts)")
    parser.add_argument("--test-file", required=True, help="dataset/test/open_end_qa_set.jsonl")
    parser.add_argument("--output", required=True)
    parser.add_argument("--drop-reasoning", action="store_true", help="omit the optional reasoning_trace")
    args = parser.parse_args()

    episodes = {}
    with open(args.test_file, encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            episodes[str(row["episode_id"])] = row

    # Some episodes repeat a question text, so a verdict is keyed by the whole judged triple
    # and consumed once per occurrence.
    verdicts = {}
    for item in json.loads(Path(args.results).read_text(encoding="utf-8"))["results"]:
        key = (str(item["episode_id"]), item["question"], item["golden_answer"], item["predicted_answer"])
        verdicts.setdefault(key, []).append(item["score"] == 1.0)

    written = 0
    with open(args.answers, encoding="utf-8") as source, open(args.output, "w", encoding="utf-8") as sink:
        for line in source:
            answer = json.loads(line)
            episode_id = str(answer["episode_id"])
            qa_pairs = episodes[episode_id]["qa_pairs"]
            if len(answer["answer_list"]) != len(qa_pairs):
                raise SystemExit(f"episode {episode_id}: {len(answer['answer_list'])} answers for "
                                 f"{len(qa_pairs)} questions")
            scores = []
            for qa, predicted in zip(qa_pairs, answer["answer_list"]):
                key = (episode_id, qa["question"], qa["answer"], predicted)
                if not verdicts.get(key):
                    raise SystemExit(f"episode {episode_id}: no judge verdict for question {qa['question_uuid']}")
                scores.append(verdicts[key].pop(0))
            record = {
                "episode_id": episode_id,
                "question_uuid_list": [qa["question_uuid"] for qa in qa_pairs],
                "answer_list": [str(a) for a in answer["answer_list"]],
                "llm_as_judge_score_list": scores,
            }
            if not args.drop_reasoning and answer.get("reasoning_trace"):
                record["reasoning_trace"] = answer["reasoning_trace"]
            sink.write(json.dumps(record, ensure_ascii=False) + "\n")
            written += 1
    print(f"wrote {written} episodes to {args.output}")


if __name__ == "__main__":
    main()
