import json
import uuid
from pathlib import Path

from tam_db.contracts import Backend, CompatConnection, CompatRow, ControlKind
from team_memory.contracts import Conflict, DomainError
from team_memory.database import SwitchableControlPlane, serializable

LEARNING_DB_NAME = "learning.db"
RECENT_LOG_LIMIT = 50
OUTBOX_BATCH = 20

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, """
        CREATE TABLE curricula (
            team_id TEXT PRIMARY KEY, title TEXT NOT NULL, revision INTEGER NOT NULL,
            updated_at TEXT NOT NULL, updated_by TEXT NOT NULL);
        CREATE TABLE modules (
            id TEXT PRIMARY KEY, team_id TEXT NOT NULL REFERENCES curricula(team_id),
            position INTEGER NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL,
            pass_threshold REAL NOT NULL, max_attempts INTEGER NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX modules_team ON modules(team_id, archived, position);
        CREATE TABLE lessons (
            id TEXT PRIMARY KEY, module_id TEXT NOT NULL REFERENCES modules(id),
            position INTEGER NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
            record_ids TEXT NOT NULL, content_hash TEXT NOT NULL DEFAULT '',
            version INTEGER NOT NULL DEFAULT 0, resolved_ids TEXT NOT NULL DEFAULT '[]',
            checked_at TEXT, archived INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX lessons_module ON lessons(module_id, archived, position);
        CREATE TABLE quizzes (
            module_id TEXT PRIMARY KEY REFERENCES modules(id), questions TEXT NOT NULL,
            revision INTEGER NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL);
        CREATE TABLE enrollments (
            team_id TEXT NOT NULL, user_id TEXT NOT NULL, started_at TEXT NOT NULL,
            PRIMARY KEY(team_id, user_id));
        CREATE TABLE lesson_progress (
            user_id TEXT NOT NULL, lesson_id TEXT NOT NULL REFERENCES lessons(id),
            opened_at TEXT NOT NULL, completed_at TEXT, first_completed_at TEXT,
            time_spent_seconds INTEGER, studied_hash TEXT, studied_version INTEGER,
            PRIMARY KEY(user_id, lesson_id));
        CREATE TABLE attempts (
            id INTEGER PRIMARY KEY, team_id TEXT NOT NULL, module_id TEXT NOT NULL REFERENCES modules(id),
            user_id TEXT NOT NULL, quiz_revision INTEGER NOT NULL, submitted_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('graded','pending_review')),
            score REAL NOT NULL, max_score REAL NOT NULL, pass_threshold REAL NOT NULL,
            passed INTEGER, graded_at TEXT);
        CREATE INDEX attempts_user ON attempts(user_id, module_id, id);
        CREATE TABLE attempt_answers (
            attempt_id INTEGER NOT NULL REFERENCES attempts(id), question_id TEXT NOT NULL,
            position INTEGER NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL,
            points REAL NOT NULL, score REAL NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('graded','pending')),
            grader TEXT CHECK(grader IN ('auto','llm','manager')), comment TEXT NOT NULL DEFAULT '',
            graded_by TEXT, graded_at TEXT, PRIMARY KEY(attempt_id, question_id));
        CREATE TABLE learning_log (
            id INTEGER PRIMARY KEY, team_id TEXT NOT NULL, user_id TEXT NOT NULL, at TEXT NOT NULL,
            event TEXT NOT NULL, subject_id TEXT NOT NULL, summary TEXT NOT NULL);
        CREATE INDEX learning_log_team ON learning_log(team_id, id);
        CREATE INDEX learning_log_user ON learning_log(team_id, user_id, id);
        CREATE TABLE personal_outbox (
            id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, team_id TEXT NOT NULL, content TEXT NOT NULL,
            created_at TEXT NOT NULL, delivered_at TEXT, outcome TEXT);
        CREATE INDEX personal_outbox_pending ON personal_outbox(user_id, delivered_at, id);
    """),
)


class LearningRepository:
    """Onboarding data (learning.db / tam_learning). Pass the Registry's plane so both share one
    switchable control plane; without it the configured one is opened."""

    def __init__(self, root: Path, plane: SwitchableControlPlane | None = None):
        self.path = root.resolve() / LEARNING_DB_NAME
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if plane is None:
            from team_memory.database_config import open_control_plane
            plane = open_control_plane(root)
        self.plane = plane
        if plane.backend is Backend.POSTGRES:
            # Schema tam_learning is provisioned by pg_provision (migrations/postgres/control).
            with self.connect() as db:
                self.instance_id = db.execute("SELECT value FROM meta WHERE key='instance_id'").fetchone()[0]
            return
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES ('instance_id',?)", (uuid.uuid4().hex,))
            applied = {row[0] for row in db.execute("SELECT version FROM schema_migrations")}
            for version, script in MIGRATIONS:
                if version in applied:
                    continue
                for statement in filter(str.strip, script.split(";")):
                    db.execute(statement)
                db.execute("INSERT INTO schema_migrations(version,applied_at) VALUES (?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                           (version,))
            self.instance_id = db.execute("SELECT value FROM meta WHERE key='instance_id'").fetchone()[0]
        self.path.chmod(0o600)

    def connect(self):
        """One learning transaction: BEGIN IMMEDIATE on SQLite, SERIALIZABLE on PostgreSQL."""
        return self.plane.connect(ControlKind.LEARNING, write=True)

    # Curriculum

    @serializable
    def curriculum(self, team_id: str) -> dict | None:
        with self.connect() as db:
            head = db.execute("SELECT * FROM curricula WHERE team_id=?", (team_id,)).fetchone()
            if head is None:
                return None
            modules = []
            for module in db.execute("SELECT * FROM modules WHERE team_id=? AND archived=0 ORDER BY position", (team_id,)):
                lessons = [self._lesson(row) for row in db.execute(
                    "SELECT * FROM lessons WHERE module_id=? AND archived=0 ORDER BY position", (module["id"],))]
                quiz = db.execute("SELECT * FROM quizzes WHERE module_id=?", (module["id"],)).fetchone()
                modules.append({**dict(module), "lessons": lessons,
                                "quiz": None if quiz is None else {**dict(quiz), "questions": json.loads(quiz["questions"])}})
        return {**dict(head), "modules": modules}

    @staticmethod
    def _lesson(row: CompatRow) -> dict:
        return {**dict(row), "record_ids": json.loads(row["record_ids"]), "resolved_ids": json.loads(row["resolved_ids"])}

    @serializable
    def replace_curriculum(self, team_id: str, spec: dict, expected_revision: int, actor_id: str,
                           hashes: dict[int, str], now: str) -> int:
        """Replace the active curriculum; `hashes` maps flat lesson index to a verified content hash."""
        with self.connect() as db:
            head = db.execute("SELECT revision FROM curricula WHERE team_id=?", (team_id,)).fetchone()
            current = 0 if head is None else head["revision"]
            if current != expected_revision:
                raise Conflict(f"Curriculum revision is {current}; read it again before saving")
            revision = current + 1
            if head is None:
                db.execute("INSERT INTO curricula VALUES (?,?,?,?,?)", (team_id, spec["title"], revision, now, actor_id))
            else:
                db.execute("UPDATE curricula SET title=?,revision=?,updated_at=?,updated_by=? WHERE team_id=?",
                           (spec["title"], revision, now, actor_id, team_id))
            own_modules = {row[0] for row in db.execute("SELECT id FROM modules WHERE team_id=?", (team_id,))}
            own_lessons = {row["id"]: row for row in db.execute(
                "SELECT l.* FROM lessons l JOIN modules m ON m.id=l.module_id WHERE m.team_id=?", (team_id,))}
            db.execute("UPDATE modules SET archived=1 WHERE team_id=?", (team_id,))
            db.execute("UPDATE lessons SET archived=1 WHERE module_id IN (SELECT id FROM modules WHERE team_id=?)", (team_id,))
            flat = 0
            for position, module in enumerate(spec["modules"]):
                module_id = module.get("id")
                if module_id is None:
                    module_id = "m_" + uuid.uuid4().hex[:12]
                    db.execute("INSERT INTO modules(id,team_id,position,title,summary,pass_threshold,max_attempts) "
                               "VALUES (?,?,?,?,?,?,?)", (module_id, team_id, position, module["title"], module["summary"],
                                                          module["pass_threshold"], module["max_attempts"]))
                elif module_id in own_modules:
                    db.execute("UPDATE modules SET position=?,title=?,summary=?,pass_threshold=?,max_attempts=?,archived=0 "
                               "WHERE id=?", (position, module["title"], module["summary"], module["pass_threshold"],
                                              module["max_attempts"], module_id))
                else:
                    raise DomainError(f"Unknown module id {module_id}; omit id to create a module")
                for lesson_position, lesson in enumerate(module["lessons"]):
                    lesson_id = lesson.get("id")
                    content_hash = hashes.get(flat, "")
                    flat += 1
                    record_ids = json.dumps(lesson["record_ids"])
                    if lesson_id is None:
                        lesson_id = "l_" + uuid.uuid4().hex[:12]
                        db.execute("INSERT INTO lessons(id,module_id,position,title,body,record_ids,content_hash,version,checked_at) "
                                   "VALUES (?,?,?,?,?,?,?,1,?)", (lesson_id, module_id, lesson_position, lesson["title"],
                                                                  lesson["body"], record_ids, content_hash,
                                                                  now if content_hash else None))
                    elif lesson_id in own_lessons:
                        old = own_lessons[lesson_id]
                        changed = (content_hash != old["content_hash"] if content_hash else
                                   (old["title"], old["body"], old["record_ids"]) != (lesson["title"], lesson["body"], record_ids))
                        db.execute("UPDATE lessons SET module_id=?,position=?,title=?,body=?,record_ids=?,content_hash=?,"
                                   "version=version+?,checked_at=?,archived=0 WHERE id=?",
                                   (module_id, lesson_position, lesson["title"], lesson["body"], record_ids,
                                    content_hash or ("" if changed else old["content_hash"]), int(changed),
                                    now if content_hash else (None if changed else old["checked_at"]), lesson_id))
                    else:
                        raise DomainError(f"Unknown lesson id {lesson_id}; omit id to create a lesson")
            return revision

    @serializable
    def module(self, module_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM modules WHERE id=? AND archived=0", (module_id,)).fetchone()
            if row is None:
                return None
            lessons = [self._lesson(r) for r in db.execute(
                "SELECT * FROM lessons WHERE module_id=? AND archived=0 ORDER BY position", (module_id,))]
            quiz = db.execute("SELECT * FROM quizzes WHERE module_id=?", (module_id,)).fetchone()
        return {**dict(row), "lessons": lessons,
                "quiz": None if quiz is None else {**dict(quiz), "questions": json.loads(quiz["questions"])}}

    @serializable
    def lesson(self, lesson_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT l.*, m.team_id, m.title AS module_title FROM lessons l JOIN modules m ON m.id=l.module_id "
                             "WHERE l.id=? AND l.archived=0 AND m.archived=0", (lesson_id,)).fetchone()
        return None if row is None else self._lesson(row)

    @serializable
    def update_lesson_source(self, lesson_id: str, content_hash: str, resolved_ids: list[int], now: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE lessons SET version=version+(CASE WHEN content_hash<>? THEN 1 ELSE 0 END),content_hash=?,resolved_ids=?,checked_at=? "
                       "WHERE id=?", (content_hash, content_hash, json.dumps(resolved_ids), now, lesson_id))

    @serializable
    def replace_quiz(self, module_id: str, questions: list[dict], expected_revision: int, actor_id: str, now: str) -> int:
        with self.connect() as db:
            row = db.execute("SELECT revision FROM quizzes WHERE module_id=?", (module_id,)).fetchone()
            current = 0 if row is None else row["revision"]
            if current != expected_revision:
                raise Conflict(f"Quiz revision is {current}; read it again before saving")
            db.execute("INSERT INTO quizzes(module_id,questions,revision,updated_at,updated_by) VALUES (?,?,?,?,?) "
                       "ON CONFLICT(module_id) DO UPDATE SET questions=excluded.questions,revision=excluded.revision,"
                       "updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                       (module_id, json.dumps(questions, ensure_ascii=False), current + 1, now, actor_id))
            return current + 1

    # Progress

    @serializable
    def enroll(self, team_id: str, user_id: str, now: str) -> tuple[str, bool]:
        with self.connect() as db:
            row = db.execute("SELECT started_at FROM enrollments WHERE team_id=? AND user_id=?", (team_id, user_id)).fetchone()
            if row is not None:
                return row["started_at"], False
            db.execute("INSERT INTO enrollments VALUES (?,?,?)", (team_id, user_id, now))
            return now, True

    @serializable
    def enrolled_users(self, team_id: str) -> set[str]:
        with self.connect() as db:
            return {row[0] for row in db.execute("SELECT user_id FROM enrollments WHERE team_id=?", (team_id,))}

    @serializable
    def pending_count(self, team_id: str) -> int:
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM attempt_answers a JOIN attempts t ON t.id=a.attempt_id "
                              "WHERE t.team_id=? AND a.status='pending'", (team_id,)).fetchone()[0]

    @serializable
    def enrollment(self, team_id: str, user_id: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT started_at FROM enrollments WHERE team_id=? AND user_id=?", (team_id, user_id)).fetchone()
        return None if row is None else row["started_at"]

    @serializable
    def progress(self, team_id: str, user_ids: list[str] | None = None) -> dict[tuple[str, str], dict]:
        query = ("SELECT p.* FROM lesson_progress p JOIN lessons l ON l.id=p.lesson_id JOIN modules m ON m.id=l.module_id "
                 "WHERE m.team_id=?")
        params: list = [team_id]
        if user_ids is not None:
            query += " AND p.user_id IN ({})".format(",".join("?" * len(user_ids)))
            params.extend(user_ids)
        with self.connect() as db:
            return {(row["user_id"], row["lesson_id"]): dict(row) for row in db.execute(query, params)}

    @serializable
    def open_lesson(self, user_id: str, lesson_id: str, now: str) -> dict:
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO lesson_progress(user_id,lesson_id,opened_at) VALUES (?,?,?)",
                       (user_id, lesson_id, now))
            return dict(db.execute("SELECT * FROM lesson_progress WHERE user_id=? AND lesson_id=?",
                                   (user_id, lesson_id)).fetchone())

    @serializable
    def complete_lesson(self, user_id: str, lesson: dict, now: str, elapsed: int, log: dict, note: str) -> tuple[dict, str]:
        """Returns (progress, outcome) with outcome in completed|restudied|unchanged."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM lesson_progress WHERE user_id=? AND lesson_id=?",
                             (user_id, lesson["id"])).fetchone()
            if row is None:
                raise Conflict("Open the lesson with onboarding_next before completing it")
            if row["completed_at"] is not None and row["studied_hash"] == lesson["content_hash"]:
                return dict(row), "unchanged"
            outcome = "completed" if row["completed_at"] is None else "restudied"
            db.execute("UPDATE lesson_progress SET completed_at=?,first_completed_at=COALESCE(first_completed_at,?),"
                       "time_spent_seconds=COALESCE(time_spent_seconds,?),studied_hash=?,studied_version=? WHERE user_id=? AND lesson_id=?",
                       (now, now, elapsed, lesson["content_hash"], lesson["version"], user_id, lesson["id"]))
            self._log(db, {**log, "event": "lesson_" + outcome})
            self._outbox(db, user_id, log["team_id"], note, now)
            return dict(db.execute("SELECT * FROM lesson_progress WHERE user_id=? AND lesson_id=?",
                                   (user_id, lesson["id"])).fetchone()), outcome

    # Attempts

    @serializable
    def attempts(self, user_id: str, module_id: str) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM attempts WHERE user_id=? AND module_id=? ORDER BY id", (user_id, module_id))]

    @serializable
    def attempts_for_team_user(self, team_id: str, user_id: str) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM attempts WHERE team_id=? AND user_id=? ORDER BY id", (team_id, user_id))]

    @serializable
    def team_attempts(self, team_id: str) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM attempts WHERE team_id=? ORDER BY id", (team_id,))]

    @serializable
    def insert_attempt(self, attempt: dict, answers: list[dict], max_attempts: int, log: dict, note: str | None) -> int:
        with self.connect() as db:
            rows = db.execute("SELECT status, passed FROM attempts WHERE user_id=? AND module_id=?",
                              (attempt["user_id"], attempt["module_id"])).fetchall()
            self.check_attempt_allowed(rows, max_attempts)
            cursor = db.execute(
                "INSERT INTO attempts(team_id,module_id,user_id,quiz_revision,submitted_at,status,score,max_score,"
                "pass_threshold,passed,graded_at) VALUES (:team_id,:module_id,:user_id,:quiz_revision,:submitted_at,"
                ":status,:score,:max_score,:pass_threshold,:passed,:graded_at)", attempt)
            attempt_id = cursor.lastrowid
            for answer in answers:
                db.execute("INSERT INTO attempt_answers(attempt_id,question_id,position,question,answer,points,score,status,"
                           "grader,comment,graded_by,graded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (attempt_id, answer["question_id"], answer["position"], json.dumps(answer["question"], ensure_ascii=False),
                            json.dumps(answer["answer"], ensure_ascii=False), answer["points"], answer["score"],
                            answer["status"], answer["grader"], answer["comment"], answer["graded_by"], answer["graded_at"]))
            self._log(db, {**log, "subject_id": str(attempt_id)})
            if note is not None:
                self._outbox(db, attempt["user_id"], attempt["team_id"], note, attempt["submitted_at"])
            return attempt_id

    @staticmethod
    def check_attempt_allowed(rows, max_attempts: int) -> None:
        if any(row["status"] == "pending_review" for row in rows):
            raise Conflict("The previous attempt is awaiting manager grading")
        if any(row["passed"] for row in rows):
            raise Conflict("This module quiz is already passed")
        if len(rows) >= max_attempts:
            raise Conflict(f"No attempts left ({max_attempts} allowed)")

    @serializable
    def attempt(self, attempt_id: int) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if row is None:
                return None
            answers = [self._answer(a) for a in db.execute(
                "SELECT * FROM attempt_answers WHERE attempt_id=? ORDER BY position", (attempt_id,))]
        return {**dict(row), "answers": answers}

    @staticmethod
    def _answer(row: CompatRow) -> dict:
        return {**dict(row), "question": json.loads(row["question"]), "answer": json.loads(row["answer"])}

    @serializable
    def grade_answer(self, attempt_id: int, question_id: str, score: float, comment: str, grader_id: str,
                     now: str, finalize) -> dict:
        """Grade one answer; `finalize(answers)` returns attempt updates plus optional log/note once nothing is pending."""
        with self.connect() as db:
            updated = db.execute("UPDATE attempt_answers SET score=?,status='graded',grader='manager',comment=?,graded_by=?,"
                                 "graded_at=? WHERE attempt_id=? AND question_id=?",
                                 (score, comment, grader_id, now, attempt_id, question_id)).rowcount
            if updated != 1:
                raise Conflict("Answer not found")
            answers = [self._answer(a) for a in db.execute(
                "SELECT * FROM attempt_answers WHERE attempt_id=? ORDER BY position", (attempt_id,))]
            attempt = dict(db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone())
            result = finalize(attempt, answers)
            if result is not None:
                changes, log, note = result
                db.execute("UPDATE attempts SET status=:status,score=:score,passed=:passed,graded_at=:graded_at WHERE id=:id",
                           {**changes, "id": attempt_id})
                self._log(db, log)
                self._outbox(db, attempt["user_id"], attempt["team_id"], note, now)
                attempt.update(changes)
            return {**attempt, "answers": answers}

    @serializable
    def grading_queue(self, team_id: str) -> list[dict]:
        with self.connect() as db:
            return [self._answer(row) for row in db.execute(
                        "SELECT a.*, t.user_id, t.module_id, t.submitted_at FROM attempt_answers a "
                        "JOIN attempts t ON t.id=a.attempt_id WHERE t.team_id=? AND a.status='pending' "
                        "ORDER BY t.id, a.position", (team_id,))]

    # Log and personal outbox

    @serializable
    def log(self, entry: dict) -> None:
        with self.connect() as db:
            self._log(db, entry)

    @serializable
    def recent_log(self, team_id: str, user_id: str | None = None, limit: int = RECENT_LOG_LIMIT) -> list[dict]:
        query = "SELECT * FROM learning_log WHERE team_id=?"
        params: list = [team_id]
        if user_id is not None:
            query += " AND user_id=?"
            params.append(user_id)
        with self.connect() as db:
            return [dict(row) for row in db.execute(query + " ORDER BY id DESC LIMIT ?", (*params, limit))]

    @staticmethod
    def _log(db: CompatConnection, entry: dict) -> None:
        db.execute("INSERT INTO learning_log(team_id,user_id,at,event,subject_id,summary) "
                   "VALUES (:team_id,:user_id,:at,:event,:subject_id,:summary)", entry)

    @staticmethod
    def _outbox(db: CompatConnection, user_id: str, team_id: str, content: str, now: str) -> None:
        db.execute("INSERT INTO personal_outbox(user_id,team_id,content,created_at) VALUES (?,?,?,?)",
                   (user_id, team_id, content, now))

    @serializable
    def pending_notes(self, user_id: str) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM personal_outbox WHERE user_id=? AND delivered_at IS NULL ORDER BY id LIMIT ?",
                (user_id, OUTBOX_BATCH))]

    @serializable
    def mark_delivered(self, note_id: int, outcome: str, now: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE personal_outbox SET delivered_at=?,outcome=? WHERE id=?", (now, outcome, note_id))
