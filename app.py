#!/usr/bin/env python3
"""GitHub → Ollama → Slack commit summarizer webhook service."""

import hashlib
import hmac
import json
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler

import requests
from dotenv import load_dotenv
from flask import Flask, Request, abort, jsonify, request

load_dotenv()

# Configuration
GITHUB_WEBHOOK_SECRET = os.environ["GITHUB_WEBHOOK_SECRET"]
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
SLACK_ALLOWED_CHANNEL_ID = os.environ["SLACK_ALLOWED_CHANNEL_ID"]
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
PORT = int(os.getenv("PORT", "5000"))
BIND_HOST = os.getenv("BIND_HOST", "100.105.195.86")

PAUSE_FLAG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".paused")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_PATH = os.path.expanduser("~/logs/commit-summarizer.log")
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

handler = RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=3)
handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
))
logger = logging.getLogger("commit-summarizer")
logger.setLevel(logging.INFO)
logger.addHandler(handler)
logger.addHandler(logging.StreamHandler(sys.stdout))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def verify_signature(req: Request) -> bool:
    """Verify the GitHub HMAC-SHA256 webhook signature."""
    signature_header = req.headers.get("X-Hub-Signature-256")
    if not signature_header:
        return False
    expected = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(),
        req.get_data(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def verify_slack_signature(req: Request) -> bool:
    """Verify Slack request signature (HMAC-SHA256, v0 scheme)."""
    timestamp = req.headers.get("X-Slack-Request-Timestamp", "")
    signature = req.headers.get("X-Slack-Signature", "")
    if not timestamp or not signature:
        return False
    try:
        if abs(time.time() - int(timestamp)) > 60 * 5:
            return False
    except ValueError:
        return False
    body = req.get_data(as_text=True)
    basestring = f"v0:{timestamp}:{body}"
    expected = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(),
        basestring.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def is_paused() -> bool:
    return os.path.exists(PAUSE_FLAG_PATH)


def extract_push_data(payload: dict) -> dict:
    """Pull relevant fields from a GitHub push event payload."""
    commits = []
    for c in payload.get("commits", []):
        commits.append({
            "message": c.get("message", ""),
            "author": c.get("author", {}).get("name", "unknown"),
            "added": c.get("added", []),
            "modified": c.get("modified", []),
            "removed": c.get("removed", []),
        })

    ref = payload.get("ref", "")
    branch = ref.split("/")[-1] if "/" in ref else ref

    return {
        "repo": payload.get("repository", {}).get("full_name", "unknown"),
        "branch": branch,
        "pusher": payload.get("pusher", {}).get("name", "unknown"),
        "commits": commits,
    }


def build_commit_text(data: dict) -> str:
    """Format commit data into a readable block for the LLM."""
    lines = [
        f"Repository: {data['repo']}",
        f"Branch: {data['branch']}",
        f"Pushed by: {data['pusher']}",
        "",
    ]
    for i, c in enumerate(data["commits"], 1):
        files = c["added"] + c["modified"] + c["removed"]
        lines.append(f"Commit {i}: {c['message']}")
        lines.append(f"  Author: {c['author']}")
        lines.append(f"  Files changed: {', '.join(files) if files else 'none listed'}")
        lines.append("")
    return "\n".join(lines)


def summarize_with_ollama(commit_text: str) -> str:
    """Send commit data to Ollama and return a plain-English summary."""
    system_prompt = (
        "You are summarizing software changes for a non-technical business audience. "
        "Be concise, explain what changed and why it matters in 2-3 sentences. "
        "Do not use technical jargon. Do not repeat the raw commit data."
    )
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": OLLAMA_MODEL,
                "stream": False,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Summarize these code changes:\n\n{commit_text}"},
                ],
            },
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"].strip()
    except requests.RequestException as exc:
        logger.error("Ollama request failed: %s", exc)
        return "(AI summary unavailable — Ollama could not be reached.)"
    except (KeyError, ValueError) as exc:
        logger.error("Unexpected Ollama response: %s", exc)
        return "(AI summary unavailable — unexpected response from Ollama.)"


def post_to_slack(data: dict, summary: str) -> bool:
    """Post a formatted message to Slack via incoming webhook."""
    num_commits = len(data["commits"])
    commit_word = "commit" if num_commits == 1 else "commits"

    slack_payload = {
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"New push to {data['repo']}",
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Branch:*\n`{data['branch']}`"},
                    {"type": "mrkdwn", "text": f"*Pushed by:*\n{data['pusher']}"},
                    {"type": "mrkdwn", "text": f"*Commits:*\n{num_commits} {commit_word}"},
                ],
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Summary*\n{summary}",
                },
            },
        ],
    }

    try:
        resp = requests.post(
            SLACK_WEBHOOK_URL,
            json=slack_payload,
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.error("Slack post failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/webhook", methods=["POST"])
def webhook():
    # Verify signature
    if not verify_signature(request):
        logger.warning("Invalid signature from %s", request.remote_addr)
        abort(403)

    # Only handle push events
    event = request.headers.get("X-GitHub-Event", "")
    if event == "ping":
        logger.info("Received ping event")
        return jsonify({"status": "pong"}), 200
    if event != "push":
        logger.info("Ignoring event type: %s", event)
        return jsonify({"status": "ignored", "event": event}), 200

    payload = request.get_json(silent=True)
    if not payload:
        logger.warning("Empty or invalid JSON payload")
        abort(400)

    data = extract_push_data(payload)
    logger.info(
        "Push received: %s/%s by %s (%d commits)",
        data["repo"], data["branch"], data["pusher"], len(data["commits"]),
    )

    # Only summarize pushes to the default (main) branch
    default_branch = payload.get("repository", {}).get("default_branch", "main")
    if data["branch"] != default_branch:
        logger.info("Ignoring push to non-default branch %s (default: %s)", data["branch"], default_branch)
        return jsonify({"status": "skipped", "reason": "non-default branch"}), 200

    if not data["commits"]:
        logger.info("No commits in push (branch delete?), skipping")
        return jsonify({"status": "skipped", "reason": "no commits"}), 200

    if is_paused():
        logger.info("Service is paused, skipping summary for %s/%s", data["repo"], data["branch"])
        return jsonify({"status": "skipped", "reason": "paused"}), 200

    # Summarize with Ollama
    commit_text = build_commit_text(data)
    logger.info("Requesting summary from Ollama (%s)…", OLLAMA_MODEL)
    summary = summarize_with_ollama(commit_text)
    logger.info("Summary: %s", summary)

    # Post to Slack
    slack_ok = post_to_slack(data, summary)
    if slack_ok:
        logger.info("Posted to Slack successfully")
    else:
        logger.error("Failed to post to Slack")

    return jsonify({"status": "ok", "slack_posted": slack_ok}), 200


@app.route("/slack/command", methods=["POST"])
def slack_command():
    if not verify_slack_signature(request):
        logger.warning("Invalid Slack signature from %s", request.remote_addr)
        abort(403)

    channel_id = request.form.get("channel_id", "")
    if channel_id != SLACK_ALLOWED_CHANNEL_ID:
        logger.info("Rejected slash command from channel %s (not allowed)", channel_id)
        return jsonify({
            "response_type": "ephemeral",
            "text": "This command can only be used in the configured channel.",
        }), 200

    command = request.form.get("command", "")
    user = request.form.get("user_name", "unknown")

    if command == "/pause":
        with open(PAUSE_FLAG_PATH, "w") as f:
            f.write(f"paused by {user} at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        logger.info("Paused by %s", user)
        return jsonify({
            "response_type": "in_channel",
            "text": f":pause_button: Commit summaries paused by @{user}.",
        }), 200

    if command == "/resume":
        try:
            os.remove(PAUSE_FLAG_PATH)
            logger.info("Resumed by %s", user)
            text = f":arrow_forward: Commit summaries resumed by @{user}."
        except FileNotFoundError:
            text = "Commit summaries are already running."
        return jsonify({"response_type": "in_channel", "text": text}), 200

    return jsonify({
        "response_type": "ephemeral",
        "text": f"Unknown command: {command}",
    }), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "healthy",
        "paused": is_paused(),
    }), 200


if __name__ == "__main__":
    logger.info("Starting commit-summarizer on %s:%d", BIND_HOST, PORT)
    app.run(host=BIND_HOST, port=PORT)
