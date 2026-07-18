#!/usr/bin/env python3
"""
SESSION ATLAS - indexer + HTTP server over the Claude Code session store.

Read-only over ~/.claude/projects/<projdir>/*.jsonl (top-level files only,
never recurses into subagents/workflows subdirectories). Builds data.json
(TF-IDF clustering + similarity graph over sessions) and serves it plus
index.html on the tailnet. Python 3 stdlib only. See BUILD-SPEC.md.
"""

import argparse
import glob
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
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
LOG_FILE = os.path.join(APP_DIR, "atlas_sessions.log")
TITLES_OVERRIDE = os.path.join(APP_DIR, "titles_override.json")
INDEX_HTML = os.path.join(APP_DIR, "index.html")

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8877
BIND_FALLBACK = "127.0.0.1"

MAX_LINES = 4000
MAX_BYTES = 5 * 1024 * 1024
MAX_DOC_MSGS = 20
MAX_DOC_CHARS = 6000
MAX_SEARCH_TERMS = 5000
MAX_SEARCH_POSTINGS = 200
MAX_SEARCH_RESULTS = 100
TITLE_MAX_LEN = 70
STALE_SECONDS = 6 * 3600
LOG_TRUNCATE_BYTES = 1024 * 1024

# v2: chat/transcript/skills/memory
TRANSCRIPT_MAX_MSGS = 500
TRANSCRIPT_MAX_CHARS = 2 * 1024 * 1024
MAX_CHAT_PROCS = 3
DEFAULT_RUN_TIMEOUT = 900
MAX_CHAT_BODY_BYTES = 1 * 1024 * 1024

TOKEN_RE = re.compile(r"[a-z][a-z0-9_-]{2,}")
HEX_RE = re.compile(r"^[0-9a-f-]{8,}$")
WS_RE = re.compile(r"\s+")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)

# ---------------------------------------------------------------------------
# v2 mutable server state (guarded by locks; ThreadingHTTPServer = one thread
# per connection)
# ---------------------------------------------------------------------------

_STATE_LOCK = threading.Lock()
PROJECTS_CACHE = []  # list of {dir,label,sessions,lastActivity}, rebuilt by run_index()

ACTIVE_LOCK = threading.Lock()
ACTIVE_PROCS = {}  # pid -> subprocess.Popen, chat children currently streaming

ENABLE_RUN = False
RUN_TIMEOUT = DEFAULT_RUN_TIMEOUT

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

logger = logging.getLogger("session_atlas")


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

    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        for raw_line in f:
            total_lines += 1
            if total_lines > MAX_LINES:
                break
            bytes_read += len(raw_line.encode("utf-8", "replace"))
            if bytes_read > MAX_BYTES:
                break
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
    """Validate each path component against NAME_RE (no slashes, no '..', no
    absolute paths possible), join under STORE_DIR, then verify the resolved
    realpath is actually inside STORE_DIR (realpath prefix check catches
    symlink/junction escapes). Returns the resolved absolute path, or None if
    any check fails."""
    if not parts or any(not p or not NAME_RE.match(p) for p in parts):
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
# v2: /api/transcript — parse one session jsonl into chat-bubble messages
# ---------------------------------------------------------------------------


def build_transcript(path, session_id, override_title=None):
    messages = []
    total_chars = 0
    truncated = False
    total_lines = 0
    bytes_read = 0
    summary_title = None
    first_user_text = None

    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        for raw_line in f:
            total_lines += 1
            if total_lines > MAX_LINES:
                truncated = True
                break
            bytes_read += len(raw_line.encode("utf-8", "replace"))
            if bytes_read > MAX_BYTES:
                truncated = True
                break
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

            if t == "user":
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
                parts = []
                if isinstance(content, str):
                    if content:
                        parts.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            bt = block.get("text")
                            if isinstance(bt, str) and bt:
                                parts.append(bt)
                if not parts:
                    continue
                entry_text = "\n".join(parts)

            if len(messages) >= TRANSCRIPT_MAX_MSGS or total_chars >= TRANSCRIPT_MAX_CHARS:
                truncated = True
                break

            remaining = TRANSCRIPT_MAX_CHARS - total_chars
            if len(entry_text) > remaining:
                entry_text = entry_text[:remaining]
                truncated = True

            messages.append({"role": t, "text": entry_text, "ts": ts})
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
# v2: locate the Claude Code CLI binary for headless spawning
#
# Per RECON.md: the npm shim (claude.cmd) is a batch wrapper around a real
# native executable at <npm-root>\node_modules\@anthropic-ai\claude-code\bin\
# claude.exe. Spawning that exe directly with an argv array (no shell) avoids
# all .cmd/cmd.exe quoting hazards. Only fall back to shell-wrapping the shim
# itself if the real exe can't be located.
# ---------------------------------------------------------------------------


def resolve_claude_binary():
    """Returns (path, needs_shell) or (None, False) if no usable binary found."""
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


# ---------------------------------------------------------------------------
# Full index build -> data.json
# ---------------------------------------------------------------------------


def run_index():
    global PROJECTS_CACHE
    t0 = time.time()
    overrides = load_titles_override()

    sessions = []
    docs = []
    search_docs = []
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
                search_docs.append((len(sessions) - 1, parsed["search_text"]))
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
    with _STATE_LOCK:
        PROJECTS_CACHE = projects

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


class AtlasHandler(BaseHTTPRequestHandler):
    server_version = "SessionAtlas/2.0"

    def log_message(self, fmt, *args):
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type):
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
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

    # -- v2: POST /api/chat — headless CLI spawn, streamed as SSE -----------

    def _handle_chat(self, body):
        prompt = body.get("prompt")
        resume = body.get("resume")
        cwd_req = body.get("cwd")
        perm_mode = body.get("permissionMode") or "default"

        if not isinstance(prompt, str) or not prompt.strip():
            self._send_json({"error": "prompt is required"}, status=400)
            return
        if resume is not None and (not isinstance(resume, str) or not NAME_RE.match(resume)):
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

        with ACTIVE_LOCK:
            if len(ACTIVE_PROCS) >= MAX_CHAT_PROCS:
                self._send_json({"error": "too many concurrent chat processes"}, status=429)
                return

        argv = [exe, "-p", prompt, "--output-format", "stream-json", "--verbose"]
        if resume:
            argv += ["--resume", resume]
        if perm_mode != "default":
            argv += ["--permission-mode", perm_mode]

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            if needs_shell:
                cmd_str = subprocess.list2cmdline(argv)
                proc = subprocess.Popen(
                    cmd_str,
                    cwd=run_cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    shell=True,
                    text=True,
                    bufsize=1,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creationflags,
                )
            else:
                proc = subprocess.Popen(
                    argv,
                    cwd=run_cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    text=True,
                    bufsize=1,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creationflags,
                )
        except OSError as e:
            logger.exception("failed to spawn claude binary")
            self._send_json({"error": "failed to spawn claude: %s" % e}, status=500)
            return

        with ACTIVE_LOCK:
            ACTIVE_PROCS[proc.pid] = proc
        logger.info("chat spawn pid=%s resume=%s cwd=%s", proc.pid, resume, run_cwd)

        timed_out = {"v": False}

        def _on_timeout():
            timed_out["v"] = True
            logger.warning("chat pid=%s hit --run-timeout, killing", proc.pid)
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass

        timer = threading.Timer(RUN_TIMEOUT, _on_timeout)
        timer.daemon = True
        timer.start()

        self._sse_start()
        client_gone = False
        try:
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
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
                except Exception:
                    pass
            code = proc.poll()
            with ACTIVE_LOCK:
                ACTIVE_PROCS.pop(proc.pid, None)
            logger.info("chat pid=%s exited code=%s", proc.pid, code)
            if not client_gone:
                try:
                    self._sse_send(
                        'data: {"type":"atlas_done","code":%s}\n\n'
                        % (json.dumps(code) if code is not None else "null")
                    )
                    self._sse_end()
                except OSError:
                    pass

    def do_GET(self):
        try:
            parsed = urlsplit(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            if path == "/":
                if os.path.isfile(INDEX_HTML):
                    self._send_file(INDEX_HTML, "text/html; charset=utf-8")
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

            elif path == "/api/transcript":
                project = (qs.get("project") or [""])[0]
                sid = (qs.get("id") or [""])[0]
                if not NAME_RE.match(project or "") or not NAME_RE.match(sid or ""):
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
                if not sep or not NAME_RE.match(project) or not NAME_RE.match(fname):
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
                with ACTIVE_LOCK:
                    n = len(ACTIVE_PROCS)
                self._send_json({"active": n, "max": MAX_CHAT_PROCS})

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
            path = urlsplit(self.path).path
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
                body, err = self._read_json_body()
                if err:
                    self._send_json({"error": err}, status=400)
                    return
                self._handle_chat(body)

            else:
                self._send_json({"error": "not found"}, status=404)
        except Exception as e:
            logger.exception("POST %s failed", self.path)
            try:
                self._send_json({"error": str(e)}, status=500)
            except Exception:
                pass


def start_server(bind_host, port):
    try:
        httpd = ThreadingHTTPServer((bind_host, port), AtlasHandler)
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
        httpd = ThreadingHTTPServer((BIND_FALLBACK, port), AtlasHandler)
        bound = BIND_FALLBACK

    logger.info("serving on %s:%d", bound, port)
    print(f"session_atlas serving on http://{bound}:{port}")
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


def main():
    global STORE_DIR, ENABLE_RUN, RUN_TIMEOUT
    parser = argparse.ArgumentParser(description="Session Atlas indexer + server")
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
    args = parser.parse_args()

    STORE_DIR = os.path.expanduser(args.root)
    ENABLE_RUN = args.enable_run
    RUN_TIMEOUT = args.run_timeout

    configure_logging()
    logger.info(
        "session_atlas start (store=%s, enable_run=%s, run_timeout=%s)",
        STORE_DIR,
        ENABLE_RUN,
        RUN_TIMEOUT,
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
    start_server(args.bind, args.port)


if __name__ == "__main__":
    main()
