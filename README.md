# Commit Summarizer

A lightweight webhook service that listens for GitHub push events, generates plain-English summaries using a local LLM (via [Ollama](https://ollama.com)), and posts them to Slack.

Designed for teams where non-technical stakeholders want to stay informed about code changes without reading diffs.

## How it works

1. GitHub sends a push webhook to this service
2. The service verifies the HMAC-SHA256 signature
3. Commit data (messages, authors, files changed) is sent to a local Ollama model
4. An AI-generated summary is posted to a Slack channel

## Requirements

- Python 3.10+
- [Ollama](https://ollama.com) running locally with a model pulled (default: `qwen2.5:3b`)
- A Slack incoming webhook URL
- A GitHub webhook secret

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

## Endpoints

- `POST /webhook` — GitHub webhook receiver
- `GET /health` — Health check

## License

MIT
