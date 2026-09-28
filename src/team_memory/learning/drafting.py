import json
import re
from collections import defaultdict

from team_memory.learning.contracts import (
    MAX_LESSONS_PER_MODULE,
    MAX_MODULES,
    MAX_TEXT_CHARS,
    MAX_TITLE_CHARS,
    CurriculumSpec,
    DraftQuestion,
    PolishedCurriculum,
    QuestionSpec,
)

DRAFT_RECORDS_PER_LESSON = 8
DRAFT_MAX_RECORDS = 2000
SUMMARY_LINE_CHARS = 140
GENERAL_PROJECT = "general"
TYPE_ORDER = ("convention", "decision", "solution", "lesson", "fact")
TYPE_TITLES = {"convention": "Conventions", "decision": "Decisions", "solution": "Solutions",
               "lesson": "Lessons learned", "fact": "Key facts"}
IMPORTANCE_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
MIN_EVIDENCE_WORDS = 3


def record_tags(record: dict) -> list[str]:
    tags = record.get("tags") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except ValueError:
            tags = []
    return sorted(str(tag) for tag in tags if isinstance(tag, str) and ":" not in tag)


def topic_of(record: dict) -> str:
    project = (record.get("project") or GENERAL_PROJECT).strip() or GENERAL_PROJECT
    if project != GENERAL_PROJECT:
        return project
    tags = record_tags(record)
    return tags[0] if tags else GENERAL_PROJECT


def first_line(text: str) -> str:
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    return line if len(line) <= SUMMARY_LINE_CHARS else line[:SUMMARY_LINE_CHARS - 1].rstrip() + "…"


def humanize(topic: str) -> str:
    text = re.sub(r"[_-]+", " ", topic).strip()
    return (text[:1].upper() + text[1:])[:MAX_TITLE_CHARS] if text else "General knowledge"


def deterministic_draft(team_name: str, records: list[dict]) -> dict:
    """Group active records by topic (project, else first tag) then by type; stable for the same input."""
    groups: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for record in records:
        if record.get("status", "active") != "active":
            continue
        kind = record.get("type") if record.get("type") in TYPE_ORDER else "fact"
        groups[topic_of(record)][kind].append(record)
    topics = sorted(groups, key=lambda topic: (topic == GENERAL_PROJECT, topic.lower()))[:MAX_MODULES]
    modules = []
    for topic in topics:
        lessons = []
        for kind in TYPE_ORDER:
            items = sorted(groups[topic].get(kind, []),
                           key=lambda r: (IMPORTANCE_RANK.get(r.get("importance", "medium"), 2), r["id"]))
            chunks = [items[i:i + DRAFT_RECORDS_PER_LESSON] for i in range(0, len(items), DRAFT_RECORDS_PER_LESSON)]
            for number, chunk in enumerate(chunks, 1):
                suffix = f" ({number}/{len(chunks)})" if len(chunks) > 1 else ""
                body = (f"{len(chunk)} {TYPE_TITLES[kind].lower()} about {humanize(topic)}:\n"
                        + "\n".join("- " + first_line(r.get("content", "")) for r in chunk))
                lessons.append({"title": f"{TYPE_TITLES[kind]}{suffix}", "body": body[:MAX_TEXT_CHARS],
                                "record_ids": [r["id"] for r in chunk]})
        if lessons:
            count = sum(len(lesson["record_ids"]) for lesson in lessons)
            modules.append({"title": humanize(topic), "summary": f"{count} team records about {humanize(topic)}.",
                            "lessons": lessons[:MAX_LESSONS_PER_MODULE]})
    return {"title": f"{team_name} onboarding"[:MAX_TITLE_CHARS], "modules": modules}


def apply_polish(draft: dict, polished: PolishedCurriculum) -> dict:
    """Take only titles and texts from the model; structure and record references stay deterministic."""
    result = json.loads(json.dumps(draft))
    result["title"] = polished.title
    for module in polished.modules:
        if module.index >= len(result["modules"]):
            continue
        target = result["modules"][module.index]
        target["title"], target["summary"] = module.title, module.summary or target["summary"]
        for lesson in module.lessons:
            if lesson.index < len(target["lessons"]):
                target["lessons"][lesson.index]["title"] = lesson.title
                if lesson.body.strip():
                    target["lessons"][lesson.index]["body"] = lesson.body
    CurriculumSpec.model_validate(result)
    return result


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def validate_draft_questions(questions: list[DraftQuestion],
                             lesson_texts: dict[str, str]) -> tuple[list[dict], list[dict], list[dict]]:
    """Keep questions whose evidence is a verbatim quote from the referenced lesson of this module."""
    accepted, evidence_list, rejected = [], [], []
    normalized = {lesson_id: normalize(text) for lesson_id, text in lesson_texts.items()}
    for number, question in enumerate(questions):
        evidence = normalize(question.evidence)
        if question.lesson_id not in normalized:
            rejected.append({"index": number, "reason": "lesson_id is not a lesson of this module"})
        elif len(evidence.split()) < MIN_EVIDENCE_WORDS or evidence not in normalized[question.lesson_id]:
            rejected.append({"index": number, "reason": "evidence is not quoted from the lesson"})
        else:
            spec = QuestionSpec.model_validate(question.model_dump(exclude={"evidence", "id"}))
            accepted.append(spec.model_dump(exclude={"id"}))
            evidence_list.append({"index": len(accepted) - 1, "lesson_id": question.lesson_id, "quote": question.evidence})
    return accepted, evidence_list, rejected
