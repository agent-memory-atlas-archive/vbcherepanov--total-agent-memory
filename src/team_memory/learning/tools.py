from team_memory.contracts import Empty
from team_memory.learning.contracts import (
    CurriculumSet,
    GradeRequest,
    LessonRequest,
    ModuleRequest,
    NextRequest,
    ProgressRequest,
    QuizSet,
    QuizSubmit,
    TeamRequest,
)

LEARNING_TOOLS = {
    "onboarding_overview": (Empty, ("List your departments with onboarding status, plus departments whose people "
                                   "you may view or manage. Use it to pick team_id before onboarding_start.")),
    "onboarding_start": (TeamRequest, ("Start or resume onboarding in a department: enrolls you and returns the plan "
                                      "(modules, lessons, quizzes) with your progress and the next step. Use first "
                                      "when a new employee asks to be onboarded.")),
    "onboarding_next": (NextRequest, ("Open your next unfinished lesson (or a given lesson_id) with its source memory "
                                     "records inline. Teach it, answer questions, then call onboarding_complete.")),
    "onboarding_complete": (LessonRequest, ("Mark a lesson as studied after the employee went through it. Idempotent; "
                                           "records the time spent and a note in their personal memory.")),
    "onboarding_quiz": (ModuleRequest, ("Get a module's quiz questions without answers. Available once all lessons "
                                       "of the module are completed. Ask the employee; do not answer for them.")),
    "onboarding_submit": (QuizSubmit, ("Submit the employee's own quiz answers (choices are zero-based option "
                                      "indexes, open answers as text). Returns score, pass/fail and review status.")),
    "onboarding_progress": (ProgressRequest, ("Show onboarding progress: your own by default; another employee's "
                                             "only for their department head, company viewers and superadmins.")),
    "onboarding_team_report": (TeamRequest, ("Department report for heads, company viewers and superadmins: every "
                                            "member x module with status, quiz scores and dates, recent learning log "
                                            "and (for heads) the open-answer grading queue.")),
    "onboarding_curriculum_get": (TeamRequest, ("Department head: read the curriculum with lesson sources, quizzes "
                                               "including answer keys, and revisions needed for editing.")),
    "onboarding_curriculum_set": (CurriculumSet, ("Department head: create or replace the curriculum (ordered modules "
                                                 "-> lessons with text and/or team record IDs). Keep existing ids to "
                                                 "preserve progress; omitted items are archived.")),
    "onboarding_curriculum_draft": (TeamRequest, ("Department head: build a draft curriculum from team memory, grouped "
                                                 "by project/tag and record type (polished by the LLM if configured). "
                                                 "Not saved: review it, then onboarding_curriculum_set.")),
    "onboarding_quiz_set": (QuizSet, ("Department head: create or replace a module quiz (single, multiple or open "
                                     "questions with answer keys/rubrics). Existing attempts keep their snapshot.")),
    "onboarding_quiz_draft": (ModuleRequest, ("Department head: generate draft quiz questions from the module's "
                                             "lessons with the server LLM; each is checked against a lesson quote. "
                                             "Not saved. Without an LLM, author questions via onboarding_quiz_set.")),
    "onboarding_grade": (GradeRequest, ("Department head: grade or regrade an open answer (points 0..question points) "
                                       "from the grading queue in onboarding_team_report.")),
}
