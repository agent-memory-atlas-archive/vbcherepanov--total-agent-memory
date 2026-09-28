from typing import Literal

from pydantic import Field, model_validator

from team_memory.contracts import DTO

ID_PATTERN = r"^[a-zA-Z0-9_-]{1,64}$"
DEFAULT_PASS_THRESHOLD = 0.8
DEFAULT_MAX_ATTEMPTS = 3
MAX_ATTEMPTS_LIMIT = 20
MAX_MODULES = 100
MAX_LESSONS_PER_MODULE = 100
MAX_RECORDS_PER_LESSON = 50
MAX_QUESTIONS = 100
MAX_OPTIONS = 10
MAX_POINTS = 100
MAX_TITLE_CHARS = 200
MAX_TEXT_CHARS = 20_000
MAX_COMMENT_CHARS = 2_000

QuestionType = Literal["single", "multiple", "open"]


class TeamRequest(DTO):
    team_id: str = Field(pattern=ID_PATTERN)


class NextRequest(TeamRequest):
    lesson_id: str | None = Field(default=None, pattern=ID_PATTERN,
                                  description="Optional: reopen this lesson instead of the next unfinished one.")


class LessonRequest(DTO):
    lesson_id: str = Field(pattern=ID_PATTERN)


class ModuleRequest(DTO):
    module_id: str = Field(pattern=ID_PATTERN)


class ProgressRequest(TeamRequest):
    user_id: str | None = Field(default=None, pattern=ID_PATTERN,
                                description="Omit for your own progress.")


class LessonSpec(DTO):
    id: str | None = Field(default=None, pattern=ID_PATTERN, description="Existing lesson id; omit for a new lesson.")
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    body: str = Field(default="", max_length=MAX_TEXT_CHARS)
    record_ids: list[int] = Field(default_factory=list, max_length=MAX_RECORDS_PER_LESSON,
                                  description="Team memory record IDs studied in this lesson.")

    @model_validator(mode="after")
    def validate_content(self):
        if not self.body.strip() and not self.record_ids:
            raise ValueError("A lesson needs body text or record_ids")
        if any(record_id <= 0 for record_id in self.record_ids) or len(set(self.record_ids)) != len(self.record_ids):
            raise ValueError("record_ids must be unique positive integers")
        return self


class ModuleSpec(DTO):
    id: str | None = Field(default=None, pattern=ID_PATTERN, description="Existing module id; omit for a new module.")
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    summary: str = Field(default="", max_length=MAX_TEXT_CHARS)
    pass_threshold: float = Field(default=DEFAULT_PASS_THRESHOLD, gt=0, le=1)
    max_attempts: int = Field(default=DEFAULT_MAX_ATTEMPTS, ge=1, le=MAX_ATTEMPTS_LIMIT)
    lessons: list[LessonSpec] = Field(min_length=1, max_length=MAX_LESSONS_PER_MODULE)


class CurriculumSpec(DTO):
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    modules: list[ModuleSpec] = Field(min_length=1, max_length=MAX_MODULES)

    @model_validator(mode="after")
    def validate_ids(self):
        ids = [m.id for m in self.modules if m.id] + [lesson.id for m in self.modules for lesson in m.lessons if lesson.id]
        if len(ids) != len(set(ids)):
            raise ValueError("Module and lesson ids must be unique")
        return self


class CurriculumSet(TeamRequest):
    expected_revision: int = Field(ge=0, description="Revision from onboarding_curriculum_get; 0 when creating.")
    curriculum: CurriculumSpec


class QuestionSpec(DTO):
    id: str | None = Field(default=None, pattern=ID_PATTERN)
    type: QuestionType
    prompt: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    options: list[str] = Field(default_factory=list, max_length=MAX_OPTIONS)
    correct: list[int] = Field(default_factory=list, max_length=MAX_OPTIONS,
                               description="Zero-based indexes of correct options (choice questions).")
    points: int = Field(default=1, ge=1, le=MAX_POINTS)
    rubric: str = Field(default="", max_length=MAX_TEXT_CHARS,
                        description="Reference answer / grading guidance (required for open questions).")
    lesson_id: str | None = Field(default=None, pattern=ID_PATTERN)

    @model_validator(mode="after")
    def validate_shape(self):
        if self.type == "open":
            if self.options or self.correct:
                raise ValueError("Open questions have no options")
            if not self.rubric.strip():
                raise ValueError("Open questions need a rubric")
            return self
        if len(self.options) < 2 or any(not option.strip() for option in self.options):
            raise ValueError("Choice questions need at least two non-empty options")
        if len(set(self.correct)) != len(self.correct) or any(i < 0 or i >= len(self.options) for i in self.correct):
            raise ValueError("correct must reference distinct option indexes")
        if self.type == "single" and len(self.correct) != 1:
            raise ValueError("Single-choice questions have exactly one correct option")
        if self.type == "multiple" and not self.correct:
            raise ValueError("Multiple-choice questions need at least one correct option")
        return self


class QuizSet(ModuleRequest):
    expected_revision: int = Field(ge=0, description="Quiz revision from onboarding_curriculum_get; 0 when creating.")
    questions: list[QuestionSpec] = Field(min_length=1, max_length=MAX_QUESTIONS)

    @model_validator(mode="after")
    def validate_ids(self):
        ids = [q.id for q in self.questions if q.id]
        if len(ids) != len(set(ids)):
            raise ValueError("Question ids must be unique")
        return self


class AnswerSpec(DTO):
    question_id: str = Field(pattern=ID_PATTERN)
    choices: list[int] = Field(default_factory=list, max_length=MAX_OPTIONS,
                               description="Zero-based option indexes for choice questions.")
    text: str = Field(default="", max_length=MAX_TEXT_CHARS, description="Answer text for open questions.")


class QuizSubmit(ModuleRequest):
    answers: list[AnswerSpec] = Field(max_length=MAX_QUESTIONS)

    @model_validator(mode="after")
    def validate_ids(self):
        ids = [a.question_id for a in self.answers]
        if len(ids) != len(set(ids)):
            raise ValueError("Each question may be answered once")
        return self


class GradeRequest(DTO):
    attempt_id: int = Field(gt=0)
    question_id: str = Field(pattern=ID_PATTERN)
    score: float = Field(ge=0, le=MAX_POINTS, description="Points earned, from 0 to the question's points.")
    comment: str = Field(default="", max_length=MAX_COMMENT_CHARS)


class LLMGrade(DTO):
    score: float = Field(ge=0, le=1)
    comment: str = Field(default="", max_length=MAX_COMMENT_CHARS)


class DraftQuestion(QuestionSpec):
    evidence: str = Field(min_length=1, max_length=MAX_TEXT_CHARS,
                          description="Verbatim quote from the lesson that supports the answer.")


class DraftQuiz(DTO):
    questions: list[DraftQuestion] = Field(max_length=MAX_QUESTIONS)


class PolishedLesson(DTO):
    index: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    body: str = Field(default="", max_length=MAX_TEXT_CHARS)


class PolishedModule(DTO):
    index: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    summary: str = Field(default="", max_length=MAX_TEXT_CHARS)
    lessons: list[PolishedLesson] = Field(default_factory=list, max_length=MAX_LESSONS_PER_MODULE)


class PolishedCurriculum(DTO):
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    modules: list[PolishedModule] = Field(max_length=MAX_MODULES)
