from typing import Literal

from pydantic import Field, model_validator

from memory_reports.contracts import OutputFormat, ReportRequest
from team_memory.contracts import IDENTIFIER

ReportScope = Literal["personal", "team", "company"]


class TeamReportRequest(ReportRequest):
    scope: ReportScope = Field(default="personal", description=(
        "personal = your own memory; team = one department (head, company viewer, superadmin); "
        "company = every department plus shared memory (company viewer, superadmin)"))
    team_id: str | None = Field(default=None, pattern=IDENTIFIER, description="Department ID for scope=team")
    format: OutputFormat = Field(default="markdown", description="markdown (readable) or json (structured)")
    include_llm_summary: bool = Field(default=False, description="Add a paragraph from the server's LLM")

    @model_validator(mode="after")
    def valid_scope(self):
        if (self.scope == "team") != (self.team_id is not None):
            raise ValueError("team_id is required for scope=team and only allowed there")
        return self

    def report_request(self) -> ReportRequest:
        return ReportRequest.model_validate(self.model_dump(exclude={"scope", "team_id", "format",
                                                                     "include_llm_summary"}))
