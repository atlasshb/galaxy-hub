#!/usr/bin/env python3
"""
STARDRIVE - indexer + HTTP server over the Claude Code session store.

Read-only over ~/.claude/projects/<projdir>/*.jsonl (top-level files only,
never recurses into subagents/workflows subdirectories). Builds data.json
(TF-IDF clustering + similarity graph over sessions) and serves it plus
index.html on the tailnet. Python 3 stdlib only. See BUILD-SPEC.md.
"""

import argparse
import glob
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CLAUDE_HOME = os.path.join(os.path.expanduser("~"), ".claude")
STORE_DIR = os.path.join(CLAUDE_HOME, "projects")
SKILLS_DIR = os.path.join(CLAUDE_HOME, "skills")
COMMANDS_DIR = os.path.join(CLAUDE_HOME, "commands")
DATA_JSON = os.path.join(APP_DIR, "data.json")
LOG_FILE = os.path.join(APP_DIR, "stardrive.log")
TITLES_OVERRIDE = os.path.join(APP_DIR, "titles_override.json")
INDEX_HTML = os.path.join(APP_DIR, "index.html")

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8877
BIND_FALLBACK = "127.0.0.1"

MAX_LINES = 4000
MAX_BYTES = 5 * 1024 * 1024
MAX_LINE_BYTES = 4 * 1024 * 1024   # M1: per-logical-line read ceiling (bounds memory on newline-free files)
MAX_DOC_MSGS = 20
MAX_DOC_CHARS = 6000
MAX_SEARCH_TERMS = 5000
MAX_SEARCH_POSTINGS = 200
MAX_SEARCH_RESULTS = 100
TITLE_MAX_LEN = 70
STALE_SECONDS = 6 * 3600
LOG_TRUNCATE_BYTES = 1024 * 1024

# Dashboard: /api/usage cache window (day buckets, ascending, only active days)
USAGE_LOOKBACK_DAYS = 120

# v2: chat/transcript/skills/memory
TRANSCRIPT_MAX_MSGS = 500
TRANSCRIPT_MAX_CHARS = 2 * 1024 * 1024
TRANSCRIPT_MAX_TOOLS = 20          # v3: max tool_use blocks surfaced per message
TRANSCRIPT_TOOL_INPUT_CAP = 200    # v3: cap on a tool_use input summary
TRANSCRIPT_TOOL_RESULT_CAP = 300   # v3: cap on a tool_result snippet
TRANSCRIPT_THINK_CAP = 1500        # v3: cap on concatenated thinking text
MAX_CHAT_PROCS = 3
DEFAULT_RUN_TIMEOUT = 900
MAX_CHAT_BODY_BYTES = 1 * 1024 * 1024

TOKEN_RE = re.compile(r"[a-z][a-z0-9_-]{2,}")
HEX_RE = re.compile(r"^[0-9a-f-]{8,}$")
WS_RE = re.compile(r"\s+")
# L4: full-string anchors (\A..\Z, not ^..$ which admits a trailing newline); use
# via is_safe_name() which additionally rejects "." / "..".
NAME_RE = re.compile(r"\A[A-Za-z0-9._-]+\Z")
FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
# Phase 1: redacts a ?token=<value> query param out of logged request lines
# (see StardriveHandler.log_message) — the token is never written to disk.
_TOKEN_QS_RE = re.compile(r"([?&]token=)[^&\s\"]*")


def is_safe_name(s):
    """A path component safe to join under the store root: non-empty, only
    [A-Za-z0-9._-], and never "." or ".." (defense-in-depth; the realpath-prefix
    check in resolve_under_store is the actual traversal barrier)."""
    return isinstance(s, str) and s not in (".", "..") and NAME_RE.fullmatch(s) is not None

# ---------------------------------------------------------------------------
# v2 mutable server state (guarded by locks; ThreadingHTTPServer = one thread
# per connection)
# ---------------------------------------------------------------------------

_STATE_LOCK = threading.Lock()
PROJECTS_CACHE = []  # list of {dir,label,sessions,lastActivity}, rebuilt by run_index()
USAGE_CACHE = {}  # Dashboard /api/usage aggregate, rebuilt by run_index() (see compute_usage)

ACTIVE_LOCK = threading.Lock()
ACTIVE_PROCS = {}  # pid -> subprocess.Popen, chat children currently streaming
# Orchestration: parallel run registry (pid -> live metadata), guarded by the
# SAME ACTIVE_LOCK as ACTIVE_PROCS. Populated alongside pid registration and
# popped in the same cleanup path; live state only, never persisted.
RUNS = {}  # pid -> {"pid","prompt"(cap 120),"cwd","resume","started","status"}
RESERVED_SLOTS = 0  # L3: chat slots reserved between the cap check and pid registration

ENABLE_RUN = False
RUN_TIMEOUT = DEFAULT_RUN_TIMEOUT

# Phase 1: optional bearer-token auth. None = no auth (loopback-only mode,
# unchanged behavior). Set by --token; never logged.
TOKEN = None

# H1: Host-header allowlist (DNS-rebinding defense). Populated by start_server()
# from the actual bound host + port; only these Host values are served.
ALLOWED_HOSTS = frozenset()

STOPWORDS = frozenset(
    """
    the and for with that this you not are but can all use from have has had
    was were will would could should its into onto about over under again
    then than when where what which who whom whose why how there here they
    them their our your his her she him out off per via etc also just more
    most some such only own same too very a an in on at to of by it as or be
    we he i my me so do did does now don t s re ve ll been being were if no
    yes up down once further ok okay let lets ll
    de het een en van ik je jij u dat dit die deze is ben bent zijn was
    waren niet geen met voor naar ook maar als dan wat kan kunnen kon
    konden moet moeten mocht mochten heb hebt heeft hebben had hadden er
    aan op in te zo om bij uit wel al nog toch dus want omdat hoe waar wie
    welke wordt worden werd werden zal zullen zou zouden mag mogen gaat ga
    gaan doen doet deed ja nee of nu even graag deze die dat daar hier
    """.split()
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("stardrive")


def configure_logging():
    try:
        if os.path.isfile(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_TRUNCATE_BYTES:
            open(LOG_FILE, "w", encoding="utf-8").close()
    except OSError:
        pass
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    try:
        handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    except OSError:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def clean_text(text, max_len=TITLE_MAX_LEN):
    t = text.strip()
    if t.lower().startswith("instruction:"):
        t = t[len("instruction:"):].strip()
    t = WS_RE.sub(" ", t)
    if len(t) > max_len:
        t = t[:max_len].rstrip()
    return t


def extract_user_text(d):
    """Text content of a user-type jsonl record, per BUILD-SPEC rules, or None."""
    msg = d.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    text = None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                bt = block.get("text")
                if isinstance(bt, str):
                    parts.append(bt)
        if parts:
            text = "\n".join(parts)
    if not text:
        return None
    if text.lstrip().startswith("<system-reminder>"):
        return None
    return text


def tokenize(text):
    return [
        t
        for t in TOKEN_RE.findall(text.lower())
        if len(t) <= 30 and t not in STOPWORDS and not HEX_RE.match(t)
    ]


def build_search_index(search_docs):
    """Build a bounded term -> session-index inverted index."""
    index = defaultdict(set)
    for session_index, text in search_docs:
        for term in set(tokenize(text)):
            if len(index) >= MAX_SEARCH_TERMS and term not in index:
                continue
            postings = index[term]
            if len(postings) < MAX_SEARCH_POSTINGS:
                postings.add(session_index)
    return {term: sorted(postings) for term, postings in index.items()}


# ---------------------------------------------------------------------------
# Scan: one jsonl session file -> parsed record
# ---------------------------------------------------------------------------


def parse_session_file(path, session_id, override_title=None):
    cwd = None
    msgs = 0
    summary_title = None
    first_user_text = None
    doc_parts = []
    doc_chars = 0
    collected = 0
    bad_lines = 0
    total_lines = 0
    bytes_read = 0
    # Dashboard /api/usage: cost/tokens from stored type=="result" lines, if any
    # (total_cost_usd, usage.input_tokens/output_tokens). Most stores never
    # write these — stay None so the caller can omit them. Later result lines
    # overwrite earlier ones per-field, so a session with several result
    # events (e.g. multiple turns) ends up with the last-seen value of each.
    cost_usd = None
    tokens_in = None
    tokens_out = None

    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        while True:
            raw_line = f.readline(MAX_LINE_BYTES)
            if raw_line == "":
                break
            total_lines += 1
            if total_lines > MAX_LINES:
                break
            bytes_read += len(raw_line.encode("utf-8", "replace"))
            if bytes_read > MAX_BYTES:
                break
            # M1: a logical line that filled MAX_LINE_BYTES without a newline is
            # oversized — skip it, draining the remainder without buffering (bytes
            # still counted so MAX_BYTES bounds total I/O).
            if not raw_line.endswith("\n") and len(raw_line) >= MAX_LINE_BYTES:
                over = False
                while True:
                    cont = f.readline(MAX_LINE_BYTES)
                    if cont == "":
                        break
                    bytes_read += len(cont.encode("utf-8", "replace"))
                    if bytes_read > MAX_BYTES:
                        over = True
                        break
                    if cont.endswith("\n"):
                        break
                bad_lines += 1
                if over:
                    break
                continue
            line = raw_line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                bad_lines += 1
                continue
            if not isinstance(d, dict):
                continue

            t = d.get("type")

            if cwd is None:
                c = d.get("cwd")
                if isinstance(c, str) and c:
                    cwd = c

            if t == "summary" and summary_title is None:
                s = d.get("summary")
                if isinstance(s, str) and s.strip():
                    summary_title = clean_text(s)

            if t == "result":
                c = d.get("total_cost_usd")
                if isinstance(c, (int, float)) and not isinstance(c, bool):
                    cost_usd = float(c)
                usage_blk = d.get("usage")
                if isinstance(usage_blk, dict):
                    ti = usage_blk.get("input_tokens")
                    if isinstance(ti, int) and not isinstance(ti, bool):
                        tokens_in = ti
                    to = usage_blk.get("output_tokens")
                    if isinstance(to, int) and not isinstance(to, bool):
                        tokens_out = to

            if t in ("user", "assistant"):
                msgs += 1

            if t == "user" and not d.get("isSidechain"):
                text = extract_user_text(d)
                if text:
                    if first_user_text is None:
                        first_user_text = text
                    if collected < MAX_DOC_MSGS and doc_chars < MAX_DOC_CHARS:
                        remaining = MAX_DOC_CHARS - doc_chars
                        piece = text[:remaining]
                        if piece:
                            doc_parts.append(piece)
                            doc_chars += len(piece)
                            collected += 1

    if override_title:
        title = clean_text(override_title)
    elif summary_title:
        title = summary_title
    elif first_user_text:
        title = clean_text(first_user_text)
    else:
        title = session_id[:8]

    doc_body = " ".join(doc_parts)
    doc = ((title + " ") * 3) + doc_body

    size_bytes = os.path.getsize(path)
    kb = int(round(size_bytes / 1024.0))
    mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    return {
        "title": title,
        "cwd": cwd,
        "msgs": msgs,
        "kb": kb,
        "mtime": mtime,
        "doc": doc,
        "search_text": doc_body,
        "bad_lines": bad_lines,
        "cost_usd": cost_usd,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
    }


def load_titles_override():
    if not os.path.isfile(TITLES_OVERRIDE):
        return {}
    try:
        with open(TITLES_OVERRIDE, "r", encoding="utf-8-sig", errors="replace") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): v for k, v in data.items() if isinstance(v, str)}
    except Exception as e:
        logger.warning("titles_override.json unreadable: %s", e)
    return {}


# ---------------------------------------------------------------------------
# v2: path safety (every file-serving endpoint below routes through this)
# ---------------------------------------------------------------------------


def resolve_under_store(*parts):
    """Validate each path component with is_safe_name (no slashes, no '.'/'..',
    no absolute paths possible), join under STORE_DIR, then verify the resolved
    realpath is actually inside STORE_DIR (realpath prefix check catches
    symlink/junction escapes). Returns the resolved absolute path, or None if
    any check fails."""
    if not parts or any(not is_safe_name(p) for p in parts):
        return None
    joined = os.path.join(STORE_DIR, *parts)
    real_root = os.path.realpath(STORE_DIR)
    real_path = os.path.realpath(joined)
    if real_path == real_root or real_path.startswith(real_root + os.sep):
        return real_path
    return None


# ---------------------------------------------------------------------------
# v2: minimal frontmatter parser (stdlib only, no PyYAML)
# ---------------------------------------------------------------------------


def parse_frontmatter(text):
    """Parse a leading '---\\n...\\n---' block into a flat {key: value} dict.
    Only top-level (non-indented) 'key: value' lines are captured; nested
    blocks (e.g. 'metadata:' sub-keys) are skipped. YAML block scalars
    ('key: >' folded / 'key: |' literal, with optional chomping indicator)
    are supported since several skills use 'description: >' for long text.
    Good enough for the SKILL.md / command / memory frontmatter this project
    actually writes — not a general YAML parser."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    fm = {}
    lines = m.group(1).splitlines()
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]
        if not line or line[0] in (" ", "\t"):
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        i += 1
        if val in (">", ">-", ">+", "|", "|-", "|+"):
            block = []
            while i < n and lines[i] and lines[i][0] in (" ", "\t"):
                block.append(lines[i].strip())
                i += 1
            val = (" " if val[0] == ">" else "\n").join(block).strip()
        elif len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key and key not in fm:
            fm[key] = val
    return fm


def _read_text(path, limit_bytes=None):
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            return f.read(limit_bytes) if limit_bytes else f.read()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# v2: /api/skills — ~/.claude/skills/*/SKILL.md + ~/.claude/commands/*.md
# ---------------------------------------------------------------------------


def scan_skills():
    skills = []
    seen = set()

    if os.path.isdir(SKILLS_DIR):
        try:
            entries = sorted(os.listdir(SKILLS_DIR))
        except OSError as e:
            logger.warning("cannot list %s: %s", SKILLS_DIR, e)
            entries = []
        for entry in entries:
            skill_md = os.path.join(SKILLS_DIR, entry, "SKILL.md")
            if not os.path.isfile(skill_md):
                continue
            text = _read_text(skill_md)
            if text is None:
                continue
            fm = parse_frontmatter(text)
            name = fm.get("name") or entry
            if name in seen:
                continue
            seen.add(name)
            skills.append({"name": name, "description": fm.get("description", "")})

    if os.path.isdir(COMMANDS_DIR):
        try:
            entries = sorted(os.listdir(COMMANDS_DIR))
        except OSError as e:
            logger.warning("cannot list %s: %s", COMMANDS_DIR, e)
            entries = []
        for entry in entries:
            if not entry.endswith(".md"):
                continue
            fp = os.path.join(COMMANDS_DIR, entry)
            if not os.path.isfile(fp):
                continue
            text = _read_text(fp)
            if text is None:
                continue
            fm = parse_frontmatter(text)
            name = fm.get("name") or os.path.splitext(entry)[0]
            if name in seen:
                continue
            seen.add(name)
            skills.append({"name": name, "description": fm.get("description", "")})

    return skills


# ---------------------------------------------------------------------------
# v2: /api/memory — <STORE_DIR>/*/memory/*.md
# ---------------------------------------------------------------------------


def scan_memory():
    items = []
    if not os.path.isdir(STORE_DIR):
        return items
    try:
        proj_entries = sorted(os.listdir(STORE_DIR))
    except OSError as e:
        logger.warning("cannot list store dir %s: %s", STORE_DIR, e)
        return items

    for proj in proj_entries:
        mem_dir = os.path.join(STORE_DIR, proj, "memory")
        if not os.path.isdir(mem_dir):
            continue
        try:
            files = sorted(glob.glob(os.path.join(mem_dir, "*.md")))
        except OSError as e:
            logger.warning("cannot list %s: %s", mem_dir, e)
            continue
        for fp in files:
            fname = os.path.basename(fp)
            text = _read_text(fp)
            if text is None:
                continue
            fm = parse_frontmatter(text)
            name = fm.get("name") or os.path.splitext(fname)[0]
            items.append(
                {
                    "name": name,
                    "file": proj + "/" + fname,
                    "description": fm.get("description", ""),
                }
            )
    return items


# ---------------------------------------------------------------------------
# v3: transcript block helpers — tool_use input summary, tool_result snippet,
# thinking text. Each is individually capped and does NOT count toward the
# transcript char budget (per CONSOLE-SPEC).
# ---------------------------------------------------------------------------


def _collapse_cap(text, cap):
    """Collapse whitespace runs to single spaces, strip, then hard-cap length."""
    text = WS_RE.sub(" ", text).strip()
    if len(text) > cap:
        text = text[:cap]
    return text


def summarize_tool_input(inp):
    """Compact one-line summary of a tool_use block's input: the first present
    key of the priority list (its string value) wins, else json.dumps(input).
    Whitespace collapsed, capped at TRANSCRIPT_TOOL_INPUT_CAP."""
    raw = None
    if isinstance(inp, dict):
        for k in ("command", "file_path", "path", "pattern", "url", "prompt", "description", "query"):
            v = inp.get(k)
            if isinstance(v, str):
                raw = v
                break
    if raw is None:
        try:
            raw = json.dumps(inp, ensure_ascii=False)
        except (TypeError, ValueError):
            raw = str(inp)
    return _collapse_cap(raw, TRANSCRIPT_TOOL_INPUT_CAP)


def tool_result_snippet(content):
    """Snippet of a tool_result block's content: a string as-is, or a list ->
    concatenated {"type":"text"} block texts. Collapsed + capped."""
    if isinstance(content, str):
        raw = content
    elif isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                bt = b.get("text")
                if isinstance(bt, str):
                    parts.append(bt)
        raw = "\n".join(parts)
    else:
        raw = ""
    return _collapse_cap(raw, TRANSCRIPT_TOOL_RESULT_CAP)


def attach_tool_results(d, tool_map):
    """Attach tool_result blocks (which live in user-type jsonl lines and may
    appear AFTER the assistant line that emitted the tool_use) onto the matching
    tool entry via tool_use_id. Entries are the same mutable dicts already stored
    on emitted assistant messages, so this single-pass late attach shows up
    there without a second scan."""
    msg = d.get("message")
    if not isinstance(msg, dict):
        return
    content = msg.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        tuid = block.get("tool_use_id")
        if not isinstance(tuid, str):
            continue
        entry = tool_map.get(tuid)
        if entry is None:
            continue
        entry["result"] = tool_result_snippet(block.get("content"))
        entry["isError"] = bool(block.get("is_error", False))


# ---------------------------------------------------------------------------
# v2/v3: /api/transcript — parse one session jsonl into chat-bubble messages
# (v3: structured tool_use / tool_result / thinking surfaced on assistant msgs)
# ---------------------------------------------------------------------------


def build_transcript(path, session_id, override_title=None):
    messages = []
    total_chars = 0
    truncated = False
    total_lines = 0
    bytes_read = 0
    summary_title = None
    first_user_text = None
    tool_map = {}  # tool_use_id -> tool entry dict (mutable; late result attach)

    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        while True:
            raw_line = f.readline(MAX_LINE_BYTES)
            if raw_line == "":
                break
            total_lines += 1
            if total_lines > MAX_LINES:
                truncated = True
                break
            bytes_read += len(raw_line.encode("utf-8", "replace"))
            if bytes_read > MAX_BYTES:
                truncated = True
                break
            # M1: a logical line that filled MAX_LINE_BYTES without a newline is
            # oversized — skip it, draining the remainder without buffering (bytes
            # still counted so MAX_BYTES bounds total I/O).
            if not raw_line.endswith("\n") and len(raw_line) >= MAX_LINE_BYTES:
                truncated = True
                over = False
                while True:
                    cont = f.readline(MAX_LINE_BYTES)
                    if cont == "":
                        break
                    bytes_read += len(cont.encode("utf-8", "replace"))
                    if bytes_read > MAX_BYTES:
                        over = True
                        break
                    if cont.endswith("\n"):
                        break
                if over:
                    break
                continue
            line = raw_line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if not isinstance(d, dict):
                continue

            t = d.get("type")

            if t == "summary" and summary_title is None:
                s = d.get("summary")
                if isinstance(s, str) and s.strip():
                    summary_title = clean_text(s)
                continue

            if t not in ("user", "assistant"):
                continue
            if d.get("isSidechain"):
                continue

            ts = d.get("timestamp")
            ts = ts if isinstance(ts, str) else None

            tools = None
            thinking = None

            if t == "user":
                # tool_result blocks arrive in user-type lines (possibly after
                # the assistant line that emitted the tool_use). Attach them
                # before deciding whether this line is a displayable user message
                # — lines that are ONLY tool_results become no message at all
                # (extract_user_text returns None), matching existing behavior.
                attach_tool_results(d, tool_map)
                text = extract_user_text(d)
                if text is None:
                    continue
                if first_user_text is None:
                    first_user_text = text
                entry_text = text
            else:
                msg = d.get("message")
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                text_parts = []
                tool_entries = []
                think_parts = []
                if isinstance(content, str):
                    if content:
                        text_parts.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text":
                            bt = block.get("text")
                            if isinstance(bt, str) and bt:
                                text_parts.append(bt)
                        elif btype == "tool_use":
                            if len(tool_entries) >= TRANSCRIPT_MAX_TOOLS:
                                continue
                            name = block.get("name")
                            tool_entry = {
                                "name": name if isinstance(name, str) else "",
                                "input": summarize_tool_input(block.get("input")),
                                "result": None,
                                "isError": False,
                            }
                            tool_entries.append(tool_entry)
                            tuid = block.get("id")
                            if isinstance(tuid, str) and tuid:
                                tool_map[tuid] = tool_entry
                        elif btype == "thinking":
                            tk = block.get("thinking")
                            if isinstance(tk, str) and tk:
                                think_parts.append(tk)
                # Emit if the line carries any text, tools, or thinking. A
                # tool_use-only assistant line (no text) is now emitted with
                # text "" rather than skipped.
                if not text_parts and not tool_entries and not think_parts:
                    continue
                entry_text = "\n".join(text_parts)
                if tool_entries:
                    tools = tool_entries
                if think_parts:
                    thinking = _collapse_cap("\n".join(think_parts), TRANSCRIPT_THINK_CAP)

            if len(messages) >= TRANSCRIPT_MAX_MSGS or total_chars >= TRANSCRIPT_MAX_CHARS:
                truncated = True
                break

            remaining = TRANSCRIPT_MAX_CHARS - total_chars
            if len(entry_text) > remaining:
                entry_text = entry_text[:remaining]
                truncated = True

            record = {"role": t, "text": entry_text, "ts": ts}
            if tools:
                record["tools"] = tools
            if thinking:
                record["thinking"] = thinking
            messages.append(record)
            total_chars += len(entry_text)

    if override_title:
        title = clean_text(override_title)
    elif summary_title:
        title = summary_title
    elif first_user_text:
        title = clean_text(first_user_text)
    else:
        title = session_id[:8]

    return {"id": session_id, "title": title, "messages": messages, "truncated": truncated}


# ---------------------------------------------------------------------------
# v2: /api/projects cache — computed once per index, not per-request
# ---------------------------------------------------------------------------


def compute_projects(ordered_sessions):
    proj_map = {}
    for s in ordered_sessions:
        d = s["project"]
        info = proj_map.setdefault(d, {"labels": Counter(), "sessions": 0, "lastActivity": ""})
        info["labels"][s["projectLabel"]] += 1
        info["sessions"] += 1
        if s["mtime"] > info["lastActivity"]:
            info["lastActivity"] = s["mtime"]

    projects = []
    for d, info in proj_map.items():
        label = info["labels"].most_common(1)[0][0] if info["labels"] else d
        projects.append(
            {
                "dir": d,
                "label": label,
                "sessions": info["sessions"],
                "lastActivity": info["lastActivity"],
            }
        )
    projects.sort(key=lambda p: p["lastActivity"], reverse=True)
    return projects


def get_projects_cache():
    with _STATE_LOCK:
        return list(PROJECTS_CACHE)


# ---------------------------------------------------------------------------
# Dashboard: /api/usage cache — computed once per index, not per-request
# (same rationale as compute_projects/PROJECTS_CACHE above).
# ---------------------------------------------------------------------------


def compute_usage(ordered_sessions, clusters, cost_rows=None):
    """Aggregate the Dashboard's /api/usage payload from already-parsed session
    records (the same objects that back data.json's "sessions" list — mtime,
    msgs, kb, project, projectLabel, cluster) plus `clusters` (id/label/size).
    No extra store reads: totals/byDay/byProject/byCluster are pure reductions
    over data already in memory.

    cost_rows, when given, is a list aligned by index to ordered_sessions of
    (cost_usd, tokens_in, tokens_out) tuples captured by parse_session_file
    from stored type=="result" lines; any element may be None. When cost_rows
    is None (e.g. warming the cache from an existing data.json at startup,
    which does not persist per-session cost/tokens), or when no session in
    the store ever carried cost/token data, the "cost"/"tokens" keys are
    omitted entirely rather than emitted as null/zero."""
    total_msgs = 0
    total_kb = 0
    by_day = {}  # date -> {"sessions":N,"messages":N}
    proj_map = {}  # dir -> {"labels":Counter,"sessions":N,"messages":N,"kb":N}

    cutoff = (datetime.now(timezone.utc) - timedelta(days=USAGE_LOOKBACK_DAYS)).strftime(
        "%Y-%m-%d"
    )

    for s in ordered_sessions:
        total_msgs += s["msgs"]
        total_kb += s["kb"]

        date = s["mtime"][:10]
        if date >= cutoff:
            dinfo = by_day.setdefault(date, {"sessions": 0, "messages": 0})
            dinfo["sessions"] += 1
            dinfo["messages"] += s["msgs"]

        pinfo = proj_map.setdefault(
            s["project"], {"labels": Counter(), "sessions": 0, "messages": 0, "kb": 0}
        )
        pinfo["labels"][s["projectLabel"]] += 1
        pinfo["sessions"] += 1
        pinfo["messages"] += s["msgs"]
        pinfo["kb"] += s["kb"]

    by_day_list = [
        {"date": d, "sessions": v["sessions"], "messages": v["messages"]}
        for d, v in sorted(by_day.items())
    ]

    by_project_list = []
    for d, info in proj_map.items():
        label = info["labels"].most_common(1)[0][0] if info["labels"] else d
        by_project_list.append(
            {
                "dir": d,
                "label": label,
                "sessions": info["sessions"],
                "messages": info["messages"],
                "kb": info["kb"],
            }
        )
    by_project_list.sort(key=lambda p: p["sessions"], reverse=True)

    by_cluster_list = [
        {"cluster": c["id"], "label": c["label"], "size": c["size"]} for c in clusters
    ]

    usage = {
        "totals": {
            "sessions": len(ordered_sessions),
            "messages": total_msgs,
            "kb": total_kb,
            "clusters": len(clusters),
            "projects": len(proj_map),
        },
        "byDay": by_day_list,
        "byProject": by_project_list,
        "byCluster": by_cluster_list,
    }

    if cost_rows:
        cost_by_day = {}
        total_cost = 0.0
        have_cost = False
        total_in = 0
        total_out = 0
        have_tokens = False
        for s, row in zip(ordered_sessions, cost_rows):
            cost_val, tin, tout = row
            if cost_val is not None:
                have_cost = True
                total_cost += cost_val
                date = s["mtime"][:10]
                if date >= cutoff:
                    cost_by_day[date] = cost_by_day.get(date, 0.0) + cost_val
            if tin is not None:
                have_tokens = True
                total_in += tin
            if tout is not None:
                have_tokens = True
                total_out += tout

        if have_cost:
            usage["cost"] = {
                "totalUsd": round(total_cost, 4),
                "byDay": [
                    {"date": d, "usd": round(v, 4)} for d, v in sorted(cost_by_day.items())
                ],
            }
        if have_tokens:
            usage["tokens"] = {"input": total_in, "output": total_out}

    return usage


def get_usage_cache():
    with _STATE_LOCK:
        return dict(USAGE_CACHE)


# ---------------------------------------------------------------------------
# v2: locate the Claude Code CLI binary for headless spawning
#
# POSIX (Linux/macOS): `claude` is a node shebang script that execve handles
# directly, so we ALWAYS spawn it without a shell (needs_shell=False). Shell-
# wrapping on POSIX was doubly wrong: /bin/sh word-splits/expands the prompt
# (command injection via $(...)/backticks) and terminate() would only kill the
# /bin/sh wrapper, orphaning the CLI's grandchildren that hold the SSE pipe.
#
# Windows: the npm shim (claude.cmd) is a batch wrapper around a real native
# executable at <npm-root>\node_modules\@anthropic-ai\claude-code\bin\
# claude.exe. Spawning that exe directly with an argv array (no shell) avoids
# all .cmd/cmd.exe quoting hazards. Only fall back to shell-wrapping the shim
# itself if the real exe can't be located.
# ---------------------------------------------------------------------------


def resolve_claude_binary():
    """Returns (path, needs_shell) or (None, False) if no usable binary found.
    needs_shell is NEVER True on POSIX."""
    if os.name == "posix":
        w = shutil.which("claude")
        return (w, False) if w else (None, False)

    # Windows (os.name == "nt") — npm-shim / .exe / .cmd resolution, unchanged.
    shim_candidates = []
    for name in ("claude.cmd", "claude.exe", "claude", "claude.ps1"):
        w = shutil.which(name)
        if w and w not in shim_candidates:
            shim_candidates.append(w)

    # Prefer the real exe behind an npm shim (claude.cmd / claude.ps1 / claude).
    for shim in shim_candidates:
        shim_dir = os.path.dirname(shim)
        exe = os.path.join(
            shim_dir, "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe"
        )
        if os.path.isfile(exe):
            return exe, False

    # A claude.exe directly on PATH.
    for shim in shim_candidates:
        if shim.lower().endswith(".exe"):
            return shim, False

    # Last resort: shell-wrap whatever shim we found (.cmd/.ps1 need a shell;
    # CreateProcess cannot exec them directly).
    if shim_candidates:
        return shim_candidates[0], True

    return None, False


# ---------------------------------------------------------------------------
# v3.1: process-tree termination for chat children
#
# On POSIX the CLI is spawned as its own session / process-group leader
# (start_new_session=True), so signalling the GROUP reaps the node process AND
# every child it spawned — plain proc.terminate() would kill only the leader and
# orphan grandchildren that keep the SSE stdout pipe open (so the stream never
# EOFs / stardrive_done never fires). On Windows we keep proc.terminate()/kill().
# Shared by every kill path: /api/chat/stop, the --run-timeout timer, the
# client-disconnect cleanup, and the finally-block terminate.
# ---------------------------------------------------------------------------


def _term_group(proc):
    """SIGTERM the child's process group (POSIX) or proc.terminate() (Windows /
    fallback when the group is gone or unsignalable)."""
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.terminate()
    except Exception:
        pass


def _kill_group(proc):
    """Hard-kill counterpart of _term_group: SIGKILL the group (POSIX) or
    proc.kill() (Windows / fallback)."""
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.kill()
    except Exception:
        pass


def terminate_proc_tree(proc, grace=5):
    """Graceful stop of a chat child and everything it spawned: group-SIGTERM,
    wait up to `grace`s, then group-SIGKILL. Blocking; safe to call on an
    already-exited proc (no-op). Callers that must not block (the HTTP request
    thread) run it in a daemon thread."""
    if proc.poll() is not None:
        return
    _term_group(proc)
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        return
    _kill_group(proc)
    try:
        proc.wait(timeout=grace)
    except Exception:
        pass


def stop_chat_proc(proc):
    """POST /api/chat/stop action: group-terminate the child immediately and, if
    still alive after 3s, group-kill — all in a daemon thread so the HTTP
    response returns at once. Only ever called with a proc pulled from
    ACTIVE_PROCS — we never signal a pid that isn't in the registry. The
    /api/chat streaming loop then finishes naturally (stdout EOF -> stardrive_done)."""
    threading.Thread(target=terminate_proc_tree, args=(proc, 3), daemon=True).start()


# ---------------------------------------------------------------------------
# Vectorize: TF-IDF + cosine edges + union-find clustering
# ---------------------------------------------------------------------------


def build_vocab(docs):
    doc_tokens = []
    df = Counter()
    for doc in docs:
        toks = tokenize(doc)
        doc_tokens.append(toks)
        df.update(set(toks))
    n = len(docs)
    idf = {}
    for term, dfc in df.items():
        if dfc > 0:
            idf[term] = math.log(n / dfc)
    return doc_tokens, idf


def build_vectors(doc_tokens, idf):
    vectors = []
    for toks in doc_tokens:
        if not toks:
            vectors.append({})
            continue
        tf = Counter(toks)
        n = len(toks)
        vec = {}
        for term, c in tf.items():
            w = (c / n) * idf.get(term, 0.0)
            if w > 0:
                vec[term] = w
        norm = math.sqrt(sum(v * v for v in vec.values()))
        if norm > 0:
            vec = {k: v / norm for k, v in vec.items()}
        vectors.append(vec)
    return vectors


def build_edges(vectors):
    n = len(vectors)
    edges = []
    for i in range(n):
        vi = vectors[i]
        if not vi:
            continue
        for j in range(i + 1, n):
            vj = vectors[j]
            if not vj:
                continue
            small, big = (vi, vj) if len(vi) <= len(vj) else (vj, vi)
            sim = 0.0
            for term, w in small.items():
                bw = big.get(term)
                if bw:
                    sim += w * bw
            if sim >= 0.15:
                edges.append([i, j, round(sim, 3)])
    return edges


def cluster_label(group, doc_tokens, idf):
    counts = Counter()
    for idx in group:
        counts.update(doc_tokens[idx])
    total = sum(counts.values())
    if total == 0:
        return "(untitled)"
    scored = []
    for term, c in counts.items():
        w = (c / total) * idf.get(term, 0.0)
        if w > 0:
            scored.append((w, term))
    if not scored:
        return "(misc)"
    scored.sort(key=lambda x: (-x[0], x[1]))
    return " / ".join(t for _, t in scored[:3])


def cluster_sessions(n, edges, doc_tokens, idf):
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i, j, w in edges:
        if w >= 0.30:
            union(i, j)

    groups = defaultdict(list)
    for idx in range(n):
        groups[find(idx)].append(idx)

    group_list = sorted(groups.values(), key=lambda g: (-len(g), min(g) if g else 0))

    clusters = []
    cluster_of = {}
    for new_id, group in enumerate(group_list):
        clusters.append(
            {"id": new_id, "label": cluster_label(group, doc_tokens, idf), "size": len(group)}
        )
        for idx in group:
            cluster_of[idx] = new_id
    return clusters, cluster_of


def top_terms(vec, k=5):
    if not vec:
        return []
    ordered = sorted(vec.items(), key=lambda kv: (-kv[1], kv[0]))
    return [t for t, _ in ordered[:k]]


def top_terms_weighted(vec, k=25):
    """v3.1: top-k TF-IDF terms as [term, weight] pairs — weight is the
    L2-normalized vector value (same vec top_terms reads), rounded to 3
    decimals. Same sort key as top_terms (weight desc, alphabetical tie-break)
    so the first 5 pairs are consistent with the `terms` field."""
    if not vec:
        return []
    ordered = sorted(vec.items(), key=lambda kv: (-kv[1], kv[0]))
    return [[t, round(w, 3)] for t, w in ordered[:k]]


# ---------------------------------------------------------------------------
# Full index build -> data.json
# ---------------------------------------------------------------------------


def run_index():
    global PROJECTS_CACHE, USAGE_CACHE
    t0 = time.time()
    overrides = load_titles_override()

    sessions = []
    docs = []
<<<<<<< HEAD:session_atlas.py
    search_docs = []
=======
    cost_rows = []  # (cost_usd, tokens_in, tokens_out) aligned by index to `sessions`
>>>>>>> upstream/main:stardrive.py
    n_files = 0

    if os.path.isdir(STORE_DIR):
        try:
            proj_entries = sorted(os.listdir(STORE_DIR))
        except OSError as e:
            logger.error("cannot list store dir %s: %s", STORE_DIR, e)
            proj_entries = []
        for entry in proj_entries:
            proj_path = os.path.join(STORE_DIR, entry)
            if not os.path.isdir(proj_path):
                continue
            try:
                jsonl_files = sorted(glob.glob(os.path.join(proj_path, "*.jsonl")))
            except OSError as e:
                logger.warning("cannot list %s: %s", proj_path, e)
                continue
            for fp in jsonl_files:
                n_files += 1
                session_id = os.path.splitext(os.path.basename(fp))[0]
                try:
                    parsed = parse_session_file(fp, session_id, overrides.get(session_id))
                except Exception as e:
                    logger.error("failed parsing %s: %s", fp, e)
                    continue
                if parsed["bad_lines"]:
                    logger.warning(
                        "%s: skipped %d unparsable json line(s)", fp, parsed["bad_lines"]
                    )
                sessions.append(
                    {
                        "id": session_id,
                        "title": parsed["title"],
                        "project": entry,
                        "projectLabel": parsed["cwd"] or entry,
                        "mtime": parsed["mtime"],
                        "msgs": parsed["msgs"],
                        "kb": parsed["kb"],
                    }
                )
                docs.append(parsed["doc"])
<<<<<<< HEAD:session_atlas.py
                search_docs.append((len(sessions) - 1, parsed["search_text"]))
=======
                cost_rows.append(
                    (parsed["cost_usd"], parsed["tokens_in"], parsed["tokens_out"])
                )
>>>>>>> upstream/main:stardrive.py
    else:
        logger.warning("store dir not found: %s", STORE_DIR)

    n = len(sessions)
    doc_tokens, idf = build_vocab(docs)
    vectors = build_vectors(doc_tokens, idf)
    edges = build_edges(vectors)
    clusters, cluster_of = cluster_sessions(n, edges, doc_tokens, idf)

    ordered_sessions = []
    for idx, sess in enumerate(sessions):
        ordered_sessions.append(
            {
                "i": idx,
                "id": sess["id"],
                "title": sess["title"],
                "project": sess["project"],
                "projectLabel": sess["projectLabel"],
                "mtime": sess["mtime"],
                "msgs": sess["msgs"],
                "kb": sess["kb"],
                "cluster": cluster_of.get(idx, 0),
                "terms": top_terms(vectors[idx], 5),
                "tw": top_terms_weighted(vectors[idx], 25),
            }
        )

    data = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "nFiles": n_files,
        "sessions": ordered_sessions,
        "clusters": clusters,
        "edges": edges,
        "searchIndex": build_search_index(search_docs),
        "searchSnippets": {
            str(idx): re.sub(r"\s+", " ", text)[:240]
            for idx, text in search_docs
        },
    }

    tmp_path = DATA_JSON + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp_path, DATA_JSON)

    projects = compute_projects(ordered_sessions)
    usage = compute_usage(ordered_sessions, clusters, cost_rows)
    with _STATE_LOCK:
        PROJECTS_CACHE = projects
        USAGE_CACHE = usage

    secs = round(time.time() - t0, 2)
    logger.info(
        "indexed: files=%d sessions=%d clusters=%d edges=%d secs=%.2f",
        n_files,
        n,
        len(clusters),
        len(edges),
        secs,
    )
    return {
        "nFiles": n_files,
        "nSessions": n,
        "nClusters": len(clusters),
        "nEdges": len(edges),
        "secs": secs,
    }


def data_json_is_fresh():
    if not os.path.isfile(DATA_JSON):
        return False
    age = time.time() - os.path.getmtime(DATA_JSON)
    return age < STALE_SECONDS


def search_data(query):
    """Search the persisted inverted index and return matching sessions."""
    query_terms = list(dict.fromkeys(tokenize(query)))
    if not query_terms or not os.path.isfile(DATA_JSON):
        return {"query": query, "matches": []}
    with open(DATA_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)
    index = data.get("searchIndex") or {}
    snippets = data.get("searchSnippets") or {}
    sessions = data.get("sessions") or []
    candidates = None
    for term in query_terms:
        ids = set(index.get(term, []))
        candidates = ids if candidates is None else candidates & ids
    results = []
    for idx in sorted(candidates or []):
        if 0 <= idx < len(sessions):
            session = sessions[idx]
            results.append({"i": session["i"], "id": session["id"], "title": session["title"],
                            "snippet": snippets.get(str(idx), "")})
            if len(results) >= MAX_SEARCH_RESULTS:
                break
    return {"query": query, "matches": results}


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class StardriveHandler(BaseHTTPRequestHandler):
    server_version = "Stardrive/3.0"

    def log_message(self, fmt, *args):
        # Phase 1: the access log otherwise echoes the raw request line
        # (BaseHTTPRequestHandler default), and ?token=<value> is now a valid
        # auth channel — redact it here so a token never lands in the log
        # file, matching the "never log the token" rule for the startup line.
        msg = _TOKEN_QS_RE.sub(r"\1***", fmt % args)
        logger.info("%s - %s", self.address_string(), msg)

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type, extra_headers=None):
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        for name, value in extra_headers or []:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    # -- v2: SSE (chunked transfer-encoding, manual framing) ----------------

    def _sse_start(self):
        self.protocol_version = "HTTP/1.1"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()

    def _sse_send(self, text):
        chunk = text.encode("utf-8")
        self.wfile.write(("%x\r\n" % len(chunk)).encode("ascii"))
        self.wfile.write(chunk)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _sse_end(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _read_json_body(self, max_bytes=MAX_CHAT_BODY_BYTES):
        length = self.headers.get("Content-Length")
        if not length:
            return None, "missing request body"
        try:
            n = int(length)
        except ValueError:
            return None, "invalid Content-Length"
        if n <= 0 or n > max_bytes:
            return None, "invalid request body size"
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return None, "invalid JSON body"
        if not isinstance(data, dict):
            return None, "invalid JSON body"
        return data, None

    # -- H1: DNS-rebinding + CSRF defenses ----------------------------------

    def _reject_bad_host(self):
        """Host-header allowlist (primary DNS-rebinding defense) on ALL requests.
        Returns True (and sends 403) when the Host is not an allowed loopback /
        configured-bind value. A rebound DNS name resolving to 127.0.0.1 still
        carries the attacker's Host, so this blocks the store read."""
        host = (self.headers.get("Host") or "").strip().lower()
        if host in ALLOWED_HOSTS:
            return False
        self._send_json({"error": "forbidden host"}, status=403)
        return True

    def _reject_cross_origin(self):
        """Reject cross-origin state-changing POSTs. Sec-Fetch-Site is sent
        automatically by modern browsers for the real same-origin UI (no frontend
        change needed); an explicit cross-origin Origin is also refused. Returns
        True (and sends 403) when the request looks cross-origin."""
        sfs = self.headers.get("Sec-Fetch-Site")
        if sfs is not None and sfs.strip().lower() not in ("same-origin", "none"):
            self._send_json({"error": "cross-origin request rejected"}, status=403)
            return True
        origin = self.headers.get("Origin")
        if origin and origin.strip().lower() != "null":
            onetloc = urlsplit(origin.strip()).netloc.lower()
            host = (self.headers.get("Host") or "").strip().lower()
            if onetloc != host:
                self._send_json({"error": "cross-origin request rejected"}, status=403)
                return True
        elif origin:  # Origin: null (sandboxed/file: context) is not same-origin
            self._send_json({"error": "cross-origin request rejected"}, status=403)
            return True
        return False

    # -- Phase 1: optional bearer-token auth --------------------------------

    def _extract_token_with_source(self, qs):
        """Pull a candidate token from, in priority order: Authorization:
        Bearer header, ?token= query param, gh_token cookie. Returns
        (token_or_None, source) where source is "header"/"query"/"cookie"/None.
        Does not itself validate against TOKEN — callers compare_digest it."""
        auth = self.headers.get("Authorization")
        if auth:
            scheme, _, value = auth.partition(" ")
            if scheme.strip().lower() == "bearer" and value.strip():
                return value.strip(), "header"
        if qs:
            qtok = (qs.get("token") or [None])[0]
            if qtok:
                return qtok, "query"
        cookie_header = self.headers.get("Cookie")
        if cookie_header:
            for part in cookie_header.split(";"):
                name, sep, val = part.strip().partition("=")
                if sep and name == "gh_token" and val:
                    return val, "cookie"
        return None, None

    def _reject_bad_token(self, qs):
        """Auth gate: when --token is configured, every request (GET and POST
        alike) must carry a matching token via Authorization: Bearer, ?token=,
        or the gh_token cookie (constant-time compare). No-op — returns False
        immediately — when no --token was configured (loopback-only mode,
        unchanged behavior). Returns True (and sends 401) on missing/invalid
        token."""
        if not TOKEN:
            return False
        supplied, _source = self._extract_token_with_source(qs)
        if not supplied or not hmac.compare_digest(supplied, TOKEN):
            self._send_json({"error": "unauthorized"}, status=401)
            return True
        return False

    def _reject_non_json_ct(self):
        """Enforce Content-Type: application/json on POST bodies (kills the
        text/plain CORS 'simple request' bypass). Returns True (and sends 415)
        when the media type is not application/json."""
        ct = self.headers.get("Content-Type", "")
        media = ct.split(";", 1)[0].strip().lower()
        if media == "application/json":
            return False
        self._send_json({"error": "Content-Type must be application/json"}, status=415)
        return True

    # -- v2: POST /api/chat — headless CLI spawn, streamed as SSE -----------

    def _handle_chat(self, body):
        global RESERVED_SLOTS
        prompt = body.get("prompt")
        resume = body.get("resume")
        cwd_req = body.get("cwd")
        perm_mode = body.get("permissionMode") or "default"

        if not isinstance(prompt, str) or not prompt.strip():
            self._send_json({"error": "prompt is required"}, status=400)
            return
        if resume is not None and not is_safe_name(resume):
            self._send_json({"error": "invalid resume id"}, status=400)
            return
        if perm_mode not in ("default", "acceptEdits", "bypassPermissions"):
            self._send_json({"error": "invalid permissionMode"}, status=400)
            return

        if cwd_req:
            if not isinstance(cwd_req, str) or not os.path.isdir(cwd_req):
                self._send_json({"error": "cwd does not exist"}, status=400)
                return
            run_cwd = cwd_req
        else:
            run_cwd = os.path.expanduser("~")

        exe, needs_shell = resolve_claude_binary()
        if not exe:
            self._send_json({"error": "claude binary not found"}, status=500)
            return

        # L3: atomically reserve a slot under the lock so concurrent requests
        # can't slip past the cap between this check and pid registration below.
        with ACTIVE_LOCK:
            if len(ACTIVE_PROCS) + RESERVED_SLOTS >= MAX_CHAT_PROCS:
                self._send_json({"error": "too many concurrent chat processes"}, status=429)
                return
            RESERVED_SLOTS += 1

        # L1: pass resume as a single --resume=<value> token so a dash-leading
        # value can never be re-read as a separate flag.
        argv = [exe, "-p", prompt, "--output-format", "stream-json", "--verbose"]
        if resume:
            argv += ["--resume=%s" % resume]
        if perm_mode != "default":
            argv += ["--permission-mode", perm_mode]

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        popen_kwargs = dict(
            cwd=run_cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        # POSIX: make the child its own session/process-group leader so every kill
        # path can signal the whole tree (see terminate_proc_tree). No effect on
        # Windows spawning, which is left byte-identical.
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        try:
            if needs_shell:
                # Windows-only path: .cmd/.ps1 shims can't be exec'd directly.
                cmd_str = subprocess.list2cmdline(argv)
                proc = subprocess.Popen(cmd_str, shell=True, **popen_kwargs)
            else:
                proc = subprocess.Popen(argv, shell=False, **popen_kwargs)
        except OSError as e:
            with ACTIVE_LOCK:
                RESERVED_SLOTS -= 1
            logger.exception("failed to spawn claude binary")
            self._send_json({"error": "failed to spawn claude: %s" % e}, status=500)
            return

        # L3: convert the reservation into a registered pid atomically. The
        # Orchestration RUNS registry is populated in the SAME locked step, so a
        # client enumerating live runs never sees a pid in ACTIVE_PROCS but not
        # RUNS (or vice versa). Timestamp comes from the request-handler clock.
        started = datetime.now(timezone.utc).isoformat()
        with ACTIVE_LOCK:
            RESERVED_SLOTS -= 1
            ACTIVE_PROCS[proc.pid] = proc
            RUNS[proc.pid] = {
                "pid": proc.pid,
                "prompt": prompt[:120],  # never store the prompt beyond the cap
                "cwd": run_cwd,
                "resume": resume,
                "started": started,
                "status": "running",
            }
        logger.info("chat spawn pid=%s resume=%s cwd=%s", proc.pid, resume, run_cwd)

        timed_out = {"v": False}

        def _on_timeout():
            timed_out["v"] = True
            logger.warning("chat pid=%s hit --run-timeout, killing", proc.pid)
            terminate_proc_tree(proc, grace=3)

        timer = threading.Timer(RUN_TIMEOUT, _on_timeout)
        timer.daemon = True
        timer.start()

        self._sse_start()
        client_gone = False
        try:
            # v3: announce the child pid as the very first SSE event so the
            # client can target POST /api/chat/stop.
            try:
                self._sse_send('data: {"type":"stardrive_started","pid":%d}\n\n' % proc.pid)
            except OSError:
                client_gone = True
                logger.info("client disconnected from /api/chat pid=%s", proc.pid)
            if not client_gone:
                for line in proc.stdout:
                    line = line.rstrip("\r\n")
                    if not line:
                        continue
                    try:
                        self._sse_send("data: %s\n\n" % line)
                    except OSError:
                        client_gone = True
                        logger.info("client disconnected from /api/chat pid=%s", proc.pid)
                        break
        finally:
            timer.cancel()
            if client_gone or timed_out["v"] or proc.poll() is None:
                terminate_proc_tree(proc, grace=5)
            code = proc.poll()
            with ACTIVE_LOCK:
                ACTIVE_PROCS.pop(proc.pid, None)
                RUNS.pop(proc.pid, None)
            logger.info("chat pid=%s exited code=%s", proc.pid, code)
            if not client_gone:
                try:
                    self._sse_send(
                        'data: {"type":"stardrive_done","code":%s}\n\n'
                        % (json.dumps(code) if code is not None else "null")
                    )
                    self._sse_end()
                except OSError:
                    pass

    def do_GET(self):
        try:
            if self._reject_bad_host():
                return
            parsed = urlsplit(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            if self._reject_bad_token(qs):
                return

            if path == "/":
                # Browser bootstrap (Phase 1): a valid token supplied via the
                # header or ?token= (NOT already via cookie) sets gh_token as
                # an HttpOnly cookie before serving index.html, so a one-time
                # visit to /?token=XXX is enough for the SPA's later
                # same-origin fetches to carry auth with no frontend change.
                # When TOKEN is unset this block is skipped entirely and the
                # response is byte-for-byte identical to before.
                extra_headers = None
                if TOKEN:
                    supplied, source = self._extract_token_with_source(qs)
                    if source in ("header", "query"):
                        extra_headers = [
                            ("Set-Cookie", "gh_token=%s; HttpOnly; SameSite=Strict; Path=/" % supplied)
                        ]
                if os.path.isfile(INDEX_HTML):
                    self._send_file(INDEX_HTML, "text/html; charset=utf-8", extra_headers=extra_headers)
                else:
                    self._send_json({"error": "index.html not found"}, status=404)

            elif path == "/data.json":
                if os.path.isfile(DATA_JSON):
                    self._send_file(DATA_JSON, "application/json; charset=utf-8")
                else:
                    self._send_json({"error": "no data.json yet"}, status=404)

            elif path == "/search":
                query = (qs.get("q") or [""])[0].strip()
                if len(query) > 200:
                    self._send_json({"error": "query too long"}, status=400)
                    return
                try:
                    self._send_json(search_data(query))
                except (OSError, ValueError, json.JSONDecodeError) as e:
                    logger.exception("search failed")
                    self._send_json({"error": str(e)}, status=500)

            elif path == "/api/projects":
                self._send_json(get_projects_cache())

            elif path == "/api/usage":
                self._send_json(get_usage_cache())

            elif path == "/api/transcript":
                project = (qs.get("project") or [""])[0]
                sid = (qs.get("id") or [""])[0]
                if not is_safe_name(project) or not is_safe_name(sid):
                    self._send_json({"error": "invalid project or id"}, status=400)
                    return
                resolved = resolve_under_store(project, sid + ".jsonl")
                if not resolved:
                    self._send_json({"error": "invalid project or id"}, status=400)
                    return
                if not os.path.isfile(resolved):
                    self._send_json({"error": "session not found"}, status=404)
                    return
                overrides = load_titles_override()
                try:
                    result = build_transcript(resolved, sid, overrides.get(sid))
                except Exception as e:
                    logger.exception("transcript parse failed for %s", resolved)
                    self._send_json({"error": str(e)}, status=500)
                    return
                self._send_json(result)

            elif path == "/api/skills":
                self._send_json({"skills": scan_skills()})

            elif path == "/api/memory":
                self._send_json(scan_memory())

            elif path == "/api/memory/read":
                file_param = (qs.get("file") or [""])[0]
                project, sep, fname = file_param.partition("/")
                if not sep or not is_safe_name(project) or not is_safe_name(fname):
                    self._send_json({"error": "invalid file"}, status=400)
                    return
                resolved = resolve_under_store(project, "memory", fname)
                if not resolved or not os.path.isfile(resolved):
                    self._send_json({"error": "invalid file"}, status=400)
                    return
                content = _read_text(resolved)
                if content is None:
                    self._send_json({"error": "unreadable file"}, status=500)
                    return
                self._send_json({"file": file_param, "content": content})

            elif path == "/api/chat/active":
                # Snapshot count + the RUNS registry under the lock so the
                # Orchestration board can enumerate/reconnect. Backward-compatible:
                # "count" is the new documented key; "active" is retained so any
                # pre-Orchestration caller keeps working; "max" is unchanged.
                with ACTIVE_LOCK:
                    n = len(ACTIVE_PROCS)
                    runs = [dict(r) for r in RUNS.values()]
                self._send_json(
                    {"count": n, "active": n, "max": MAX_CHAT_PROCS, "runs": runs}
                )

            else:
                self._send_json({"error": "not found"}, status=404)
        except Exception as e:
            logger.exception("GET %s failed", self.path)
            try:
                self._send_json({"error": str(e)}, status=500)
            except Exception:
                pass

    def do_POST(self):
        try:
            if self._reject_bad_host():
                return
            parsed = urlsplit(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            if self._reject_bad_token(qs):
                return

            # H1: all state-changing POSTs are CSRF-guarded (Sec-Fetch-Site / Origin).
            if path in ("/refresh", "/api/chat", "/api/chat/stop"):
                if self._reject_cross_origin():
                    return
            if path == "/refresh":
                t0 = time.time()
                try:
                    result = run_index()
                    self._send_json({"ok": True, "nSessions": result["nSessions"], "secs": result["secs"]})
                except Exception as e:
                    logger.exception("refresh failed")
                    self._send_json({"ok": False, "error": str(e)}, status=500)

            elif path == "/api/chat":
                if not ENABLE_RUN:
                    self._send_json(
                        {"error": "run disabled; start with --enable-run"}, status=403
                    )
                    return
                if self._reject_non_json_ct():
                    return
                body, err = self._read_json_body()
                if err:
                    self._send_json({"error": err}, status=400)
                    return
                self._handle_chat(body)

            elif path == "/api/chat/stop":
                if not ENABLE_RUN:
                    self._send_json(
                        {"error": "run disabled; start with --enable-run"}, status=403
                    )
                    return
                if self._reject_non_json_ct():
                    return
                body, err = self._read_json_body()
                if err:
                    self._send_json({"error": err}, status=400)
                    return
                pid = body.get("pid")
                if not isinstance(pid, int) or isinstance(pid, bool):
                    self._send_json({"error": "pid must be an integer"}, status=400)
                    return
                with ACTIVE_LOCK:
                    proc = ACTIVE_PROCS.get(pid)
                if proc is None:
                    self._send_json({"error": "no such active chat process"}, status=404)
                    return
                stop_chat_proc(proc)
                self._send_json({"ok": True})

            else:
                self._send_json({"error": "not found"}, status=404)
        except Exception as e:
            logger.exception("POST %s failed", self.path)
            try:
                self._send_json({"error": str(e)}, status=500)
            except Exception:
                pass


def is_loopback_bind(host):
    """True if `host` is a loopback address/name that never leaves the
    machine: 'localhost', 127.0.0.0/8 (IPv4), or ::1 (IPv6) — no DNS lookup
    involved. Anything else (0.0.0.0, a LAN/tailnet IP, a hostname) is
    non-loopback and, per the --token safe-by-default gate in main(), requires
    --token to start."""
    h = (host or "").strip().lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def compute_allowed_hosts(bound, port):
    """H1 allowlist: loopback names + the configured bind host, each with and
    without the served port. Lowercased for case-insensitive Host matching."""
    hosts = {"127.0.0.1", "localhost"}
    if bound:
        hosts.add(bound)
    allowed = set()
    for h in hosts:
        hl = h.strip().lower()
        if not hl:
            continue
        allowed.add(hl)
        allowed.add("%s:%d" % (hl, port))
    return frozenset(allowed)


def start_server(bind_host, port):
    global ALLOWED_HOSTS
    try:
        httpd = ThreadingHTTPServer((bind_host, port), StardriveHandler)
        bound = bind_host
    except OSError as e:
        if bind_host == BIND_FALLBACK:
            raise
        logger.warning(
            "bind %s:%d failed (%s), falling back to %s",
            bind_host,
            port,
            e,
            BIND_FALLBACK,
        )
        httpd = ThreadingHTTPServer((BIND_FALLBACK, port), StardriveHandler)
        bound = BIND_FALLBACK

    ALLOWED_HOSTS = compute_allowed_hosts(bound, port)
    logger.info("serving on %s:%d (allowed hosts: %s)", bound, port, ", ".join(sorted(ALLOWED_HOSTS)))
    print(f"stardrive serving on http://{bound}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutdown requested")
        httpd.shutdown()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def load_projects_cache_from_disk():
    """Warm PROJECTS_CACHE from an existing data.json without a full reindex
    (used at startup when data.json is already fresh, so run_index() — the
    only other place PROJECTS_CACHE gets built — never runs)."""
    global PROJECTS_CACHE
    if not os.path.isfile(DATA_JSON):
        return
    try:
        with open(DATA_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        projects = compute_projects(data.get("sessions", []))
        with _STATE_LOCK:
            PROJECTS_CACHE = projects
    except Exception as e:
        logger.warning("could not warm projects cache from data.json: %s", e)


def load_usage_cache_from_disk():
    """Warm USAGE_CACHE from an existing data.json without a full reindex (same
    rationale as load_projects_cache_from_disk — used at startup when
    data.json is already fresh). data.json's "sessions" list carries mtime/
    msgs/kb/project/projectLabel/cluster, enough for totals/byDay/byProject/
    byCluster, but not the per-session cost/tokens captured during parsing —
    those are never persisted to data.json, so cost_rows is omitted here and
    compute_usage leaves the "cost"/"tokens" keys out until the next
    run_index() (a POST /refresh or a restart with stale data)."""
    global USAGE_CACHE
    if not os.path.isfile(DATA_JSON):
        return
    try:
        with open(DATA_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        usage = compute_usage(data.get("sessions", []), data.get("clusters", []))
        with _STATE_LOCK:
            USAGE_CACHE = usage
    except Exception as e:
        logger.warning("could not warm usage cache from data.json: %s", e)


def main():
    global STORE_DIR, ENABLE_RUN, RUN_TIMEOUT, TOKEN, MAX_CHAT_PROCS
    parser = argparse.ArgumentParser(description="Stardrive indexer + server")
    parser.add_argument("--index-only", action="store_true", help="index once and exit")
    parser.add_argument("--serve", action="store_true", help="index if stale, then serve")
    parser.add_argument("--root", default=STORE_DIR, help="Claude Code session store (default: ~/.claude/projects)")
    parser.add_argument("--bind", default=DEFAULT_BIND, help="interface to serve on (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="port (default: 8877)")
    parser.add_argument(
        "--enable-run",
        action="store_true",
        help="allow POST /api/chat to spawn the Claude Code CLI (off by default)",
    )
    parser.add_argument(
        "--run-timeout",
        type=int,
        default=DEFAULT_RUN_TIMEOUT,
        help="kill a spawned chat process after N seconds (default: 900)",
    )
    parser.add_argument(
        "--max-runs",
        type=int,
        default=3,
        help="max concurrent chat/run processes, 1..8 inclusive (default: 3)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="require this bearer token on every request (Authorization: Bearer, "
        "?token=, or gh_token cookie); required to bind a non-loopback --bind",
    )
    args = parser.parse_args()

    # Phase 1 safe-by-default: a non-loopback --bind with no --token would
    # serve every session, unauthenticated, to anyone who can reach the port.
    # Loopback binds (127.0.0.0/8, ::1, localhost) with no token are unchanged
    # from today (no auth). Refuse immediately, before any indexing work.
    if not is_loopback_bind(args.bind) and not args.token:
        print(
            "refusing to bind %s without --token; anyone who can reach the port "
            "could read your sessions" % args.bind,
            file=sys.stderr,
        )
        sys.exit(1)

    # Orchestration: --max-runs sets the concurrency cap. Hard cap 8 (spec);
    # reject out-of-range at startup, before any indexing work. The atomic
    # slot-reservation mechanism in _handle_chat is unchanged — only the value
    # of MAX_CHAT_PROCS it compares against.
    if args.max_runs < 1 or args.max_runs > 8:
        print(
            "invalid --max-runs %d: must be between 1 and 8 inclusive"
            % args.max_runs,
            file=sys.stderr,
        )
        sys.exit(1)

    STORE_DIR = os.path.expanduser(args.root)
    ENABLE_RUN = args.enable_run
    RUN_TIMEOUT = args.run_timeout
    TOKEN = args.token
    MAX_CHAT_PROCS = args.max_runs

    configure_logging()
    logger.info(
        "stardrive start (store=%s, enable_run=%s, run_timeout=%s, auth=%s)",
        STORE_DIR,
        ENABLE_RUN,
        RUN_TIMEOUT,
        "on" if TOKEN else "off",
    )

    if args.index_only:
        result = run_index()
        print(
            "indexed: nFiles={nFiles} nSessions={nSessions} nClusters={nClusters} "
            "nEdges={nEdges} secs={secs}".format(**result)
        )
        return

    # default and --serve both: index if missing/stale, then serve.
    if not data_json_is_fresh():
        run_index()
    else:
        load_projects_cache_from_disk()
        load_usage_cache_from_disk()
    start_server(args.bind, args.port)


if __name__ == "__main__":
    main()
