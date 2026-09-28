"""Pure builders for progress views; no I/O."""

PERCENT = 100


def lesson_status(lesson: dict, row: dict | None) -> dict:
    if row is None:
        status = "not_started"
    elif row["completed_at"] is None:
        status = "opened"
    else:
        status = "completed"
    stale = status == "completed" and row["studied_hash"] != lesson["content_hash"]
    return {"lesson_id": lesson["id"], "title": lesson["title"], "status": status,
            "opened_at": row["opened_at"] if row else None,
            "completed_at": row["completed_at"] if row else None,
            "first_completed_at": row["first_completed_at"] if row else None,
            "time_spent_seconds": row["time_spent_seconds"] if row else None,
            "updated_since_studied": stale}


def quiz_status(module: dict, attempts: list[dict]) -> dict | None:
    if module["quiz"] is None:
        return None
    graded = [a for a in attempts if a["status"] == "graded"]
    best = max(graded, key=lambda a: (a["score"] / a["max_score"] if a["max_score"] else 0, -a["id"]), default=None)
    passed = next((a for a in graded if a["passed"]), None)
    pending = any(a["status"] == "pending_review" for a in attempts)
    return {"questions": len(module["quiz"]["questions"]), "pass_threshold": module["pass_threshold"],
            "max_attempts": module["max_attempts"], "attempts_used": len(attempts),
            "attempts_left": max(0, module["max_attempts"] - len(attempts)),
            "best_score": None if best is None else best["score"],
            "max_score": None if best is None else best["max_score"],
            "best_percent": None if best is None or not best["max_score"] else round(PERCENT * best["score"] / best["max_score"]),
            "passed": passed is not None, "passed_at": passed["graded_at"] if passed else None,
            "pending_review": pending,
            "last_attempt_at": attempts[-1]["submitted_at"] if attempts else None}


def module_progress(module: dict, rows: dict[str, dict], attempts: list[dict]) -> dict:
    lessons = [lesson_status(lesson, rows.get(lesson["id"])) for lesson in module["lessons"]]
    done = sum(1 for lesson in lessons if lesson["status"] == "completed")
    quiz = quiz_status(module, attempts)
    all_done = done == len(lessons)
    if quiz is not None and quiz["passed"]:
        status = "passed"
    elif quiz is not None and quiz["pending_review"]:
        status = "pending_review"
    elif all_done and quiz is None:
        status = "completed"
    elif all_done and quiz["attempts_left"] == 0:
        status = "failed"
    elif all_done:
        status = "quiz_available"
    elif done or any(lesson["status"] == "opened" for lesson in lessons):
        status = "in_progress"
    else:
        status = "not_started"
    completed_dates = [lesson["completed_at"] for lesson in lessons if lesson["completed_at"]]
    opened_dates = [lesson["opened_at"] for lesson in lessons if lesson["opened_at"]]
    return {"module_id": module["id"], "title": module["title"], "status": status,
            "lessons_completed": done, "lessons_total": len(lessons),
            "started_at": min(opened_dates, default=None),
            "lessons_finished_at": max(completed_dates) if all_done and completed_dates else None,
            "lessons": lessons, "quiz": quiz}


def user_progress(curriculum: dict, user_id: str, rows: dict[tuple[str, str], dict], attempts: list[dict]) -> dict:
    own = {lesson_id: row for (uid, lesson_id), row in rows.items() if uid == user_id}
    modules = [module_progress(module, own, [a for a in attempts if a["module_id"] == module["id"] and a["user_id"] == user_id])
               for module in curriculum["modules"]]
    lessons_total = sum(m["lessons_total"] for m in modules)
    lessons_done = sum(m["lessons_completed"] for m in modules)
    finished = sum(1 for m in modules if m["status"] in ("passed", "completed"))
    return {"modules": modules, "summary": {
        "lessons_completed": lessons_done, "lessons_total": lessons_total,
        "modules_finished": finished, "modules_total": len(modules),
        "percent": round(PERCENT * lessons_done / lessons_total) if lessons_total else 0,
        "updated_lessons": sum(1 for m in modules for lesson in m["lessons"] if lesson["updated_since_studied"])}}


def next_step(progress: dict) -> dict:
    for module in progress["modules"]:
        for lesson in module["lessons"]:
            if lesson["status"] != "completed":
                return {"action": "lesson", "module_id": module["module_id"], "lesson_id": lesson["lesson_id"],
                        "title": lesson["title"], "hint": "Call onboarding_next to open it."}
        quiz = module["quiz"]
        if module["status"] == "quiz_available":
            return {"action": "quiz", "module_id": module["module_id"], "title": module["title"],
                    "attempts_left": quiz["attempts_left"], "hint": "Call onboarding_quiz, then onboarding_submit."}
    for module in progress["modules"]:
        for lesson in module["lessons"]:
            if lesson["updated_since_studied"]:
                return {"action": "review", "module_id": module["module_id"], "lesson_id": lesson["lesson_id"],
                        "title": lesson["title"], "hint": "Source records changed since you studied it; reopen it."}
    waiting = [m["module_id"] for m in progress["modules"] if m["status"] == "pending_review"]
    if waiting:
        return {"action": "wait_review", "module_ids": waiting, "hint": "Open answers await your department head's grading."}
    failed = [m["module_id"] for m in progress["modules"] if m["status"] == "failed"]
    if failed:
        return {"action": "contact_manager", "module_ids": failed, "hint": "No quiz attempts left; ask your department head."}
    return {"action": "done", "hint": "Onboarding complete."}


def empty_kpis(members: int) -> dict:
    return {"has_curriculum": False, "modules": 0, "members": members, "enrolled": 0, "finished": 0,
            "completion_percent": 0, "average_score_percent": None, "pending_grading": 0}


def team_kpis(curriculum: dict, member_ids: list[str], rows: dict, attempts: list[dict],
              enrolled: set[str], pending: int) -> dict:
    progress = [user_progress(curriculum, user_id, rows, attempts) for user_id in member_ids]
    scores = [module["quiz"]["best_percent"] for p in progress for module in p["modules"]
              if module["quiz"] is not None and module["quiz"]["best_percent"] is not None]
    finished = sum(1 for p in progress if p["summary"]["modules_finished"] == p["summary"]["modules_total"])
    return {"has_curriculum": True, "modules": len(curriculum["modules"]), "members": len(member_ids),
            "enrolled": len(enrolled & set(member_ids)), "finished": finished,
            "completion_percent": round(sum(p["summary"]["percent"] for p in progress) / len(progress)) if progress else 0,
            "average_score_percent": round(sum(scores) / len(scores)) if scores else None,
            "pending_grading": pending}


def company_totals(departments: list[dict]) -> dict:
    members = sum(d["members"] for d in departments if d["has_curriculum"])
    weighted = sum(d["completion_percent"] * d["members"] for d in departments if d["has_curriculum"])
    return {"with_curriculum": sum(1 for d in departments if d["has_curriculum"]),
            "enrolled": sum(d["enrolled"] for d in departments),
            "finished": sum(d["finished"] for d in departments),
            "completion_percent": round(weighted / members) if members else 0,
            "pending_grading": sum(d["pending_grading"] for d in departments)}
