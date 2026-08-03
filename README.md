# Commit Summarizer

A lightweight webhook service that listens for GitHub push events, generates plain-English summaries using a local LLM (via [Ollama](https://ollama.com)), and posts them to Slack.

Designed for teams where non-technical stakeholders want to stay informed about code changes without reading diffs.

## How it works

1. GitHub sends a push webhook to this service
2. The service verifies the HMAC-SHA256 signature
3. Commit data (messages, authors, files changed) is sent to a local Ollama model
4. An AI-generated summary is posted to a Slack channel as the app's bot user
5. If a summary comes out wrong, the maintainer DMs the bot a correction and the
   bot edits its own channel message in place

## Requirements

- Python 3.10+
- [Ollama](https://ollama.com) running locally with a model pulled (default: `qwen2.5:3b`)
- A Slack app with a bot token (scopes: `chat:write`, `channels:history`, `im:history`, `commands`), invited to the target channel
- A GitHub webhook secret

> **Why a bot token and not an incoming webhook?** Messages posted through an
> incoming webhook belong to a separate bot identity that no token can act on —
> Slack rejects `chat.update`/`chat.delete` on them (`cant_update_message`), so
> a bad summary is stuck forever. Messages posted with the bot token via
> `chat.postMessage` can be edited in place. The webhook still works as a
> fallback (`SLACK_WEBHOOK_URL`) if no bot token is configured, but DM edits
> won't work for those posts.

## Setup

### 1. Install Ollama and pull a model

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:3b
```

### 2. Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env with your actual values
```

### 4. Run

```bash
python app.py
```

Or deploy as a systemd service:

```ini
[Unit]
Description=Commit Summarizer Webhook
After=network.target ollama.service

[Service]
Type=simple
User=your-user
WorkingDirectory=/path/to/commit-summarizer
ExecStart=/path/to/commit-summarizer/venv/bin/python app.py
EnvironmentFile=/path/to/commit-summarizer/.env
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 5. Configure GitHub webhook

In your repo: Settings → Webhooks → Add webhook

| Field | Value |
|---|---|
| Payload URL | `https://your-domain.com/webhook` |
| Content type | `application/json` |
| Secret | *(your GITHUB_WEBHOOK_SECRET)* |
| Events | Just the push event |

### 6. Configure Slack slash commands (optional)

To pause and resume summaries from Slack, add two slash commands to your Slack app:

1. https://api.slack.com/apps → your app → **Slash Commands** → **Create New Command**
   - Command: `/pause`
   - Request URL: `https://your-domain.com/slack/command`
   - Save
2. Repeat for `/resume` (same Request URL)
3. **Basic Information** → copy the **Signing Secret** into `SLACK_SIGNING_SECRET`
4. Get the channel ID where the commands are allowed (right-click the channel → View channel details → bottom of the panel) and put it in `SLACK_ALLOWED_CHANNEL_ID`
5. Reinstall the app to your workspace if Slack prompts you

When paused, the service still ACKs GitHub webhooks but skips the Ollama summary and the Slack post.

### 7. Enable DM edits (optional)

Lets the configured maintainer fix a bad summary by DMing the bot — the bot
rewrites the summary with Ollama (treating the DM as ground truth) and edits
the original channel message in place.

1. https://api.slack.com/apps → your app → **OAuth & Permissions** → add the
   `im:history` bot scope (plus `chat:write` and `channels:history` if missing),
   then reinstall the app
2. **Event Subscriptions** → toggle **Enable Events** → Request URL:
   `https://your-domain.com/slack/events` (the service answers the verification
   challenge automatically — it must already be running)
3. Under **Subscribe to bot events**, add `message.im` and save
4. Set `SLACK_ADMIN_USER_ID` in `.env` to your Slack member ID (profile → ⋮ →
   Copy member ID) — DMs from anyone else are ignored
5. Restart the service, then DM the bot something like
   *"the last summary is wrong — the maintainer list was trimmed, not expanded"*

Only the most recent summary is editable, and only if it was posted via the
bot token (the state lives in `last_post.json` next to `app.py`).

## Endpoints

- `POST /webhook` — GitHub webhook receiver
- `POST /slack/events` — Slack Events API receiver (DM-driven summary edits)
- `POST /slack/command` — Slack slash command handler (`/pause`, `/resume`)
- `GET /health` — Health check (returns `paused` status and whether posts are editable)

## License

AGPL 3.0
