#!/usr/bin/env python3
"""
Extract and compress Claude Code transcript for knowledge extraction.

Two-level auto-save:
1. INSTANT: Save session summary directly to memory.db (no LLM needed)
2. DETAILED: Save compact transcript to extract-queue for Claude to analyze next session

Usage:
    python3 extract_transcript.py --transcript /path/to/transcript.jsonl \
                                  --session-id <uuid> \
                                  --output-dir ~/.tam/extract-queue \
                                  --db ~/.tam/memory.db
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memory_core.timestamps import utc_now
from paths import memory_dir
from secret_redaction import redact_secrets

MAX_OUTPUT_KB = 200
MAX_USER_TEXT = 2000
MAX_ASSISTANT_TEXT = 500
MAX_TOOL_INPUT_TEXT = 200
HEAD_MESSAGES = 20
TAIL_MESSAGES = 30

# Transcript-only rule on top of the shared list: credential/auth names with a quoted value.
TRANSCRIPT_PATTERNS = [
    re.compile(r'(?:api[_-]?key|password|secret|token|credential|auth)\s*[:=]\s*["\']?[\w\-\.]{8,}', re.IGNORECASE),
]


def sanitize(text: str) -> str:
    """Remove sensitive data from text."""
    text, _ = redact_secrets(text)
    for pattern in TRANSCRIPT_PATTERNS:
        text = pattern.sub('[REDACTED]', text)
    return text


def extract_content_text(content) -> str:
    """Extract text from message content (string or array)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text" and c.get("text", "").strip():
                parts.append(c["text"].strip())
        return "\n".join(parts)
    return ""


def extract(transcript_path: str, session_id: str, output_dir: str) -> dict:
    """Extract transcript. Returns output dict with conversation and metadata."""
    conversation = []
    memory_saves = []
    user_texts = []
    assistant_texts = []
    tools_used = set()
    files_written = []
    stats = {
        "total_messages": 0,
        "user_messages": 0,
        "assistant_messages": 0,
        "tool_calls": 0,
    }

    project_dir = ""
    project_name = ""
    git_branch = ""
    first_ts = None
    last_ts = None

    # Claude Code v2.1.139+ subagent lineage tracking. We pick up explicit
    # agent_id / parent_agent_id when the transcript carries them (Anthropic
    # may add these fields in a future Claude Code release), and we also
    # fall back to `isSidechain=True` as a proxy signal that a subagent did
    # work in this session.
    agent_ids: set[str] = set()
    parent_agent_ids: set[str] = set()
    sidechain_count = 0

    with open(transcript_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts = obj.get("timestamp", "")

            # Capture metadata from first available record
            if not project_dir and obj.get("cwd"):
                project_dir = obj["cwd"]
                project_name = os.path.basename(project_dir)
            if not git_branch and obj.get("gitBranch"):
                git_branch = obj["gitBranch"]
            if ts:
                if not first_ts:
                    first_ts = ts
                last_ts = ts

            # Agent lineage — accept both snake_case and camelCase, since the
            # exact field name is not yet contracted upstream.
            for key in ("agent_id", "agentId"):
                v = obj.get(key)
                if isinstance(v, str) and v:
                    agent_ids.add(v)
                    break
            for key in ("parent_agent_id", "parentAgentId"):
                v = obj.get(key)
                if isinstance(v, str) and v:
                    parent_agent_ids.add(v)
                    break
            if obj.get("isSidechain") is True:
                sidechain_count += 1

            # Only process user and assistant messages
            msg = obj.get("message")
            if not isinstance(msg, dict):
                continue

            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "user":
                stats["user_messages"] += 1
                stats["total_messages"] += 1

                text = extract_content_text(content)
                if text:
                    clean_text = sanitize(text[:MAX_USER_TEXT])
                    conversation.append({
                        "role": "user",
                        "ts": ts,
                        "text": clean_text,
                    })
                    user_texts.append(clean_text)

            elif role == "assistant":
                stats["assistant_messages"] += 1
                stats["total_messages"] += 1

                if isinstance(content, str) and content.strip():
                    clean_text = sanitize(content[:MAX_ASSISTANT_TEXT])
                    conversation.append({
                        "role": "assistant",
                        "ts": ts,
                        "text": clean_text,
                    })
                    assistant_texts.append(clean_text)
                elif isinstance(content, list):
                    for c in content:
                        if not isinstance(c, dict):
                            continue
                        ctype = c.get("type", "")

                        if ctype == "text" and c.get("text", "").strip():
                            clean_text = sanitize(c["text"][:MAX_ASSISTANT_TEXT])
                            conversation.append({
                                "role": "assistant",
                                "ts": ts,
                                "text": clean_text,
                            })
                            assistant_texts.append(clean_text)

                        elif ctype == "tool_use":
                            stats["tool_calls"] += 1
                            tool_name = c.get("name", "")
                            tool_input = c.get("input", {})
                            tools_used.add(tool_name.split("__")[-1] if "__" in tool_name else tool_name)

                            # Track memory_save calls
                            if "memory_save" in tool_name:
                                content_preview = str(tool_input.get("content", ""))[:200]
                                ktype = tool_input.get("type", "")
                                project = tool_input.get("project", "")
                                memory_saves.append(
                                    f"memory_save(type={ktype}, project={project}, "
                                    f"content={content_preview})"
                                )

                            # Track file writes
                            if tool_name in ("Write", "Edit") or tool_name.endswith("Write") or tool_name.endswith("Edit"):
                                fp = tool_input.get("file_path", "")
                                if fp:
                                    files_written.append(os.path.basename(fp))

                            input_str = json.dumps(tool_input, ensure_ascii=False)
                            conversation.append({
                                "role": "tool_call",
                                "ts": ts,
                                "tool": tool_name,
                                "input_summary": sanitize(input_str[:MAX_TOOL_INPUT_TEXT]),
                            })

    # Skip empty sessions
    if not conversation:
        return {}

    output = {
        "version": 1,
        "session_id": session_id,
        "project_dir": project_dir,
        "project_name": project_name,
        "git_branch": git_branch,
        "started_at": first_ts,
        "ended_at": last_ts,
        "stats": stats,
        "conversation": conversation,
        "memory_saves_in_session": memory_saves,
        "user_texts": user_texts,
        "assistant_texts": assistant_texts,
        "tools_used": sorted(tools_used),
        "files_written": files_written[:20],
        "extracted_at": datetime.now(tz=timezone.utc).isoformat(),
        "status": "pending",
        "agent_ids": sorted(agent_ids),
        "parent_agent_ids": sorted(parent_agent_ids),
        "sidechain_count": sidechain_count,
    }

    # Trim conversation if output too large
    max_bytes = MAX_OUTPUT_KB * 1024
    output_str = json.dumps(output, ensure_ascii=False)

    if len(output_str) > max_bytes and len(conversation) > HEAD_MESSAGES + TAIL_MESSAGES:
        head = conversation[:HEAD_MESSAGES]
        tail = conversation[-TAIL_MESSAGES:]
        dropped = len(conversation) - HEAD_MESSAGES - TAIL_MESSAGES
        output["conversation"] = (
            head
            + [{"role": "system", "text": f"[... {dropped} messages omitted ...]"}]
            + tail
        )
        output["stats"]["trimmed"] = True
        output["stats"]["dropped_messages"] = dropped
        output_str = json.dumps(output, ensure_ascii=False)

    # If still too large, truncate assistant texts further
    if len(output_str) > max_bytes:
        for item in output["conversation"]:
            if item.get("role") == "assistant" and "text" in item:
                item["text"] = item["text"][:200]
            if item.get("role") == "tool_call" and "input_summary" in item:
                item["input_summary"] = item["input_summary"][:100]
        output_str = json.dumps(output, ensure_ascii=False)

    # Write to extract-queue
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    output_path = Path(output_dir) / f"pending-{session_id}.json"

    # Atomic write: temp file then rename
    tmp_path = output_path.with_suffix(".tmp")
    tmp_path.write_text(output_str, encoding="utf-8")
    tmp_path.rename(output_path)

    return output


def auto_save_knowledge(db_path: str, session_id: str, data: dict) -> list:
    """Save meaningful knowledge from session directly to memory.db.

    Creates up to 3 records:
    1. Session task — what the user was working on (first request)
    2. Work log — files changed and tools used
    3. Recovery context — last messages for continuing work
    """
    project = data.get("project_name", "general")
    branch = data.get("git_branch", "")
    user_texts = data.get("user_texts", [])
    assistant_texts = data.get("assistant_texts", [])
    files = data.get("files_written", [])
    tools = data.get("tools_used", [])
    stats = data.get("stats", {})

    # Subagent lineage proxy. If the transcript carries explicit
    # agent_id / parent_agent_id, persist them on the auto-extracted rows.
    # Otherwise, if any sidechain activity was observed, mark the rows with
    # a deterministic "session-<id>" agent_id so recall can filter sessions
    # that involved subagent work even before Claude Code writes the real
    # fields. None means "no subagent activity in this session" — keeps
    # column NULL.
    agent_ids = data.get("agent_ids") or []
    parent_agent_ids = data.get("parent_agent_ids") or []
    sidechain_count = int(data.get("sidechain_count", 0) or 0)

    session_agent_id = agent_ids[0] if agent_ids else (
        f"session-{session_id}" if sidechain_count > 0 else None
    )
    session_parent_agent_id = parent_agent_ids[0] if parent_agent_ids else None
    extra_tags = ["has-subagent-work"] if sidechain_count > 0 else []

    records = []

    # 1. Session task — first user message is the main task
    if user_texts:
        task_text = user_texts[0][:1500]
        branch_info = f" (branch: {branch})" if branch else ""
        records.append({
            "sid_suffix": "task",
            "type": "fact",
            "content": f"[{project}{branch_info}] Session task: {task_text}",
            "context": (
                f"User's main request in this session. "
                f"{stats.get('user_messages', 0)} user msgs, "
                f"{stats.get('tool_calls', 0)} tool calls total."
            ),
            "tags": ["session-task", "auto-extract", project],
        })

    # 2. Work log — files changed and tools used
    if files:
        unique_files = sorted(set(files))[:20]
        file_list = ", ".join(unique_files)
        tool_list = ", ".join(tools[:15]) if tools else "N/A"
        records.append({
            "sid_suffix": "worklog",
            "type": "fact",
            "content": (
                f"[{project}] Files modified: {file_list}. "
                f"Tools used: {tool_list}."
            ),
            "context": (
                f"Auto-extracted work log. "
                f"{len(files)} file operations, "
                f"{len(unique_files)} unique files."
            ),
            "tags": ["work-log", "auto-extract", project],
        })

    # 3. Recovery context — last messages for continuation
    last_user = user_texts[-5:] if len(user_texts) > 1 else user_texts
    last_assistant = assistant_texts[-5:] if assistant_texts else []

    if last_user or last_assistant:
        parts = []
        if last_user:
            parts.append("Last user messages:")
            for msg in last_user:
                parts.append(f"  - {msg[:600]}")
        if last_assistant:
            parts.append("Last assistant responses:")
            for msg in last_assistant:
                parts.append(f"  - {msg[:600]}")

        recovery_text = "\n".join(parts)
        records.append({
            "sid_suffix": "recovery",
            "type": "fact",
            "content": f"[{project}] Session recovery context:\n{recovery_text}",
            "context": (
                f"Recovery data for continuing work on {project}. "
                f"Session ended at {data.get('ended_at', 'unknown')}."
            ),
            "tags": ["recovery", "auto-extract", project],
        })

    if not records:
        return []

    saved_ids = []
    try:
        db = sqlite3.connect(db_path)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")

        # Detect whether the agent-lineage columns exist (migration 028).
        # Older installs without the migration fall back to the legacy INSERT
        # so this script keeps working across upgrades.
        cols = {r[1] for r in db.execute("PRAGMA table_info(knowledge)").fetchall()}
        has_lineage = "agent_id" in cols and "parent_agent_id" in cols

        now = utc_now()

        for record in records:
            dedup_key = f"auto_{session_id}_{record['sid_suffix']}"

            existing = db.execute(
                "SELECT id FROM knowledge WHERE session_id=? AND source='auto-extract'",
                (dedup_key,)
            ).fetchone()
            if existing:
                saved_ids.append(existing[0])
                continue

            row_tags = list(record["tags"]) + extra_tags

            if has_lineage:
                cur = db.execute("""
                    INSERT INTO knowledge (session_id, type, content, context, project, tags,
                                           source, confidence, created_at, last_confirmed, recall_count,
                                           agent_id, parent_agent_id)
                    VALUES (?, ?, ?, ?, ?, ?, 'auto-extract', 0.7, ?, ?, 0, ?, ?)
                """, (
                    dedup_key,
                    record["type"],
                    sanitize(record["content"]),
                    record["context"],
                    project,
                    json.dumps(row_tags),
                    now,
                    now,
                    session_agent_id,
                    session_parent_agent_id,
                ))
            else:
                cur = db.execute("""
                    INSERT INTO knowledge (session_id, type, content, context, project, tags,
                                           source, confidence, created_at, last_confirmed, recall_count)
                    VALUES (?, ?, ?, ?, ?, ?, 'auto-extract', 0.7, ?, ?, 0)
                """, (
                    dedup_key,
                    record["type"],
                    sanitize(record["content"]),
                    record["context"],
                    project,
                    json.dumps(row_tags),
                    now,
                    now,
                ))
            saved_ids.append(cur.lastrowid)

        db.commit()
        db.close()
    except Exception as e:
        print(f"Auto-save to DB failed: {e}", file=sys.stderr)

    return saved_ids


def main():
    parser = argparse.ArgumentParser(description="Extract Claude Code transcript for knowledge extraction")
    parser.add_argument("--transcript", required=True, help="Path to JSONL transcript file")
    parser.add_argument("--session-id", required=True, help="Session UUID")
    parser.add_argument("--output-dir", required=True, help="Output directory for extraction archive")
    parser.add_argument("--db", default=str(memory_dir() / "memory.db"),
                        help="Path to memory.db for auto-save")
    args = parser.parse_args()

    if not os.path.isfile(args.transcript):
        print(f"Transcript not found: {args.transcript}", file=sys.stderr)
        sys.exit(1)

    data = extract(args.transcript, args.session_id, args.output_dir)
    if not data:
        print("Empty session, nothing to extract", file=sys.stderr)
        return

    # Rename output from pending-* to done-* (auto-processed)
    pending_path = Path(args.output_dir) / f"pending-{args.session_id}.json"
    done_path = Path(args.output_dir) / f"done-{args.session_id}.json"
    if pending_path.exists():
        pending_path.rename(done_path)

    size = done_path.stat().st_size if done_path.exists() else 0
    print(f"Extracted {size} bytes to {done_path}", file=sys.stderr)

    # Auto-save meaningful knowledge to memory.db (3 records)
    if os.path.isfile(args.db):
        saved_ids = auto_save_knowledge(args.db, args.session_id, data)
        if saved_ids:
            print(f"Auto-saved {len(saved_ids)} knowledge records to memory.db (ids={saved_ids})", file=sys.stderr)


if __name__ == "__main__":
    main()
