#!/usr/bin/env python3
"""GitHub → Ollama → Slack commit summarizer webhook service."""

import hashlib
import hmac
import json
import logging
import os
import sys
import threading
import time
from collections import deque
from logging.handlers import RotatingFileHandler

import requests
from dotenv import load_dotenv
from flask import Flask, Request, abort, jsonify, request

load_dotenv()

# Configuration
GITHUB_WEBHOOK_SECRET = os.environ["GITHUB_WEBHOOK_SECRET"]
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
SLACK_ALLOWED_CHANNEL_ID = os.environ["SLACK_ALLOWED_CHANNEL_ID"]
# Preferred: post as the app's bot user so messages can be edited later.
# Fallback: legacy incoming webhook (messages posted this way are NOT editable).
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
# Only DMs from this Slack user ID may trigger summary edits.
SLACK_ADMIN_USER_ID = os.getenv("SLACK_ADMIN_USER_ID", "")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "120"))
# Ollama keep_alive: how long model weights stay loaded after a request.
# Set to 0 to unload immediately (keeps idle weights out of swap), or a
# duration like "5m". Empty = Ollama's default.
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "")
PORT = int(os.getenv("PORT", "5000"))
BIND_HOST = os.getenv("BIND_HOST", "100.105.195.86")

if not SLACK_BOT_TOKEN and not SLACK_WEBHOOK_URL:
    raise SystemExit("Set SLACK_BOT_TOKEN (preferred) or SLACK_WEBHOOK_URL in the environment")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PAUSE_FLAG_PATH = os.path.join(BASE_DIR, ".paused")
LAST_POST_PATH = os.path.join(BASE_DIR, "last_post.json")

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


SUMMARY_SYSTEM_PROMPT = (
    "You are summarizing software changes for a non-technical business audience. "
    "Be concise, explain what changed and why it matters in 2-3 sentences. "
    "Do not use technical jargon. Do not repeat the raw commit data."
)


def chat_with_ollama(messages: list) -> str | None:
    """Send a chat request to Ollama; return the reply text or None on failure."""
    payload = {"model": OLLAMA_MODEL, "stream": False, "messages": messages}
    if OLLAMA_KEEP_ALIVE != "":
        try:
            payload["keep_alive"] = int(OLLAMA_KEEP_ALIVE)
        except ValueError:
            payload["keep_alive"] = OLLAMA_KEEP_ALIVE
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json=payload,
            timeout=OLLAMA_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"].strip()
    except requests.RequestException as exc:
        logger.error("Ollama request failed: %s", exc)
        return None
    except (KeyError, ValueError) as exc:
        logger.error("Unexpected Ollama response: %s", exc)
        return None


def summarize_with_ollama(commit_text: str) -> str:
    """Send commit data to Ollama and return a plain-English summary."""
    reply = chat_with_ollama([
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": f"Summarize these code changes:\n\n{commit_text}"},
    ])
    return reply or "(AI summary unavailable — Ollama could not be reached.)"


def revise_with_ollama(commit_text: str, prev_summary: str, instruction: str) -> str | None:
    """Rewrite a summary using a correction from the maintainer."""
    return chat_with_ollama([
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": (
            "You previously summarized these code changes:\n\n"
            f"{commit_text}\n\n"
            f"Your summary was:\n{prev_summary}\n\n"
            "The maintainer says the summary is wrong and gave this correction. "
            "Treat the correction as ground truth even where it contradicts the "
            "commit data, and write a replacement summary:\n"
            f"{instruction}"
        )},
    ])


# ---------------------------------------------------------------------------
# Slack posting
# ---------------------------------------------------------------------------

def slack_api(method: str, payload: dict) -> dict:
    """Call a Slack Web API method with the bot token."""
    try:
        resp = requests.post(
            f"https://slack.com/api/{method}",
            json=payload,
            headers={
                "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                "Content-Type": "application/json; charset=utf-8",
            },
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.error("Slack %s request failed: %s", method, exc)
        return {"ok": False, "error": str(exc)}
    if not body.get("ok"):
        logger.error("Slack %s returned error: %s", method, body.get("error"))
    return body


def build_summary_blocks(meta: dict, summary: str) -> list:
    num_commits = meta["num_commits"]
    commit_word = "commit" if num_commits == 1 else "commits"
    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"New push to {meta['repo']}",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Branch:*\n`{meta['branch']}`"},
                {"type": "mrkdwn", "text": f"*Pushed by:*\n{meta['pusher']}"},
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
    ]


def save_last_post(record: dict) -> None:
    with open(LAST_POST_PATH, "w") as f:
        json.dump(record, f, indent=2)


def load_last_post() -> dict | None:
    try:
        with open(LAST_POST_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def post_to_slack(data: dict, summary: str, commit_text: str) -> bool:
    """Post a summary to Slack.

    Uses chat.postMessage when a bot token is configured (so the message can be
    edited later via DM), otherwise falls back to the legacy incoming webhook.
    """
    meta = {
        "repo": data["repo"],
        "branch": data["branch"],
        "pusher": data["pusher"],
        "num_commits": len(data["commits"]),
    }
    blocks = build_summary_blocks(meta, summary)
    fallback_text = f"New push to {meta['repo']} — {summary}"

    if SLACK_BOT_TOKEN:
        body = slack_api("chat.postMessage", {
            "channel": SLACK_ALLOWED_CHANNEL_ID,
            "text": fallback_text,
            "blocks": blocks,
        })
        if not body.get("ok"):
            return False
        save_last_post({
            "ts": body["ts"],
            "channel": body["channel"],
            "meta": meta,
            "commit_text": commit_text,
            "summary": summary,
        })
        return True

    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json={"blocks": blocks}, timeout=15)
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.error("Slack post failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# DM-driven summary edits
# ---------------------------------------------------------------------------

_seen_event_ids: set = set()
_seen_event_order: deque = deque()


def _event_already_seen(event_id: str) -> bool:
    """Dedupe Slack event redeliveries (bounded memory)."""
    if not event_id or event_id in _seen_event_ids:
        return bool(event_id)
    _seen_event_ids.add(event_id)
    _seen_event_order.append(event_id)
    while len(_seen_event_order) > 500:
        _seen_event_ids.discard(_seen_event_order.popleft())
    return False


def send_dm(channel: str, text: str) -> None:
    slack_api("chat.postMessage", {"channel": channel, "text": text})


def handle_edit_request(instruction: str, dm_channel: str) -> None:
    """Revise the last posted summary per the maintainer's DM and edit it in place."""
    last = load_last_post()
    if not last:
        send_dm(dm_channel, "I don't have an editable summary on record yet — "
                            "only messages posted after the bot-token upgrade can be edited.")
        return

    logger.info("Edit requested for ts=%s: %s", last["ts"], instruction)
    new_summary = revise_with_ollama(last["commit_text"], last["summary"], instruction)
    if not new_summary:
        send_dm(dm_channel, "Couldn't generate a revised summary — Ollama is unreachable. "
                            "The channel message is unchanged.")
        return

    body = slack_api("chat.update", {
        "channel": last["channel"],
        "ts": last["ts"],
        "text": f"New push to {last['meta']['repo']} — {new_summary}",
        "blocks": build_summary_blocks(last["meta"], new_summary),
    })
    if body.get("ok"):
        last["summary"] = new_summary
        save_last_post(last)
        logger.info("Summary ts=%s updated", last["ts"])
        send_dm(dm_channel, f"Done — updated the summary in <#{last['channel']}>:\n\n{new_summary}")
    else:
        send_dm(dm_channel, f"Slack refused the edit ({body.get('error')}). "
                            "The channel message is unchanged.")


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
    slack_ok = post_to_slack(data, summary, commit_text)
    if slack_ok:
        logger.info("Posted to Slack successfully")
    else:
        logger.error("Failed to post to Slack")

    return jsonify({"status": "ok", "slack_posted": slack_ok}), 200


@app.route("/slack/events", methods=["POST"])
def slack_events():
    if not verify_slack_signature(request):
        logger.warning("Invalid Slack signature from %s", request.remote_addr)
        abort(403)

    payload = request.get_json(silent=True) or {}

    # One-time URL verification handshake when enabling Event Subscriptions
    if payload.get("type") == "url_verification":
        return jsonify({"challenge": payload.get("challenge", "")}), 200

    if payload.get("type") != "event_callback":
        return "", 200
    if _event_already_seen(payload.get("event_id", "")):
        return "", 200

    event = payload.get("event", {})
    # Only plain DMs from the configured maintainer; ignore our own replies
    if event.get("type") != "message" or event.get("channel_type") != "im":
        return "", 200
    if event.get("bot_id") or event.get("subtype"):
        return "", 200
    if not SLACK_ADMIN_USER_ID or event.get("user") != SLACK_ADMIN_USER_ID:
        logger.info("Ignoring DM from user %s (not the configured admin)", event.get("user"))
        return "", 200

    instruction = (event.get("text") or "").strip()
    if not instruction:
        return "", 200

    # Slack expects an ACK within 3 seconds; the Ollama rewrite takes longer,
    # so do the work in the background and reply via DM when done.
    threading.Thread(
        target=handle_edit_request,
        args=(instruction, event.get("channel", "")),
        daemon=True,
    ).start()
    return "", 200


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
        "editable_posts": bool(SLACK_BOT_TOKEN),
    }), 200


if __name__ == "__main__":
    logger.info("Starting commit-summarizer on %s:%d", BIND_HOST, PORT)
    app.run(host=BIND_HOST, port=PORT)
