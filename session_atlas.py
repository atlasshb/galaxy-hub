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
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STORE_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")
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
TITLE_MAX_LEN = 70
STALE_SECONDS = 6 * 3600
LOG_TRUNCATE_BYTES = 1024 * 1024

TOKEN_RE = re.compile(r"[a-z][a-z0-9_-]{2,}")
HEX_RE = re.compile(r"^[0-9a-f-]{8,}$")
WS_RE = re.compile(r"\s+")

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
    t0 = time.time()
    overrides = load_titles_override()

    sessions = []
    docs = []
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
    }

    tmp_path = DATA_JSON + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp_path, DATA_JSON)

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


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class AtlasHandler(BaseHTTPRequestHandler):
    server_version = "SessionAtlas/1.0"

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

    def do_GET(self):
        try:
            path = urlsplit(self.path).path
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


def main():
    global STORE_DIR
    parser = argparse.ArgumentParser(description="Session Atlas indexer + server")
    parser.add_argument("--index-only", action="store_true", help="index once and exit")
    parser.add_argument("--serve", action="store_true", help="index if stale, then serve")
    parser.add_argument("--root", default=STORE_DIR, help="Claude Code session store (default: ~/.claude/projects)")
    parser.add_argument("--bind", default=DEFAULT_BIND, help="interface to serve on (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="port (default: 8877)")
    args = parser.parse_args()

    STORE_DIR = os.path.expanduser(args.root)

    configure_logging()
    logger.info("session_atlas start (store=%s)", STORE_DIR)

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
    start_server(args.bind, args.port)


if __name__ == "__main__":
    main()
