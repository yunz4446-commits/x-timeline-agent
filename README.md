# X Timeline Agent

AI-powered X (Twitter) timeline curation agent. Automatically scrapes your timeline, classifies tweets by usefulness, generates deep summaries, and delivers digests via Feishu — plus an interactive chat agent to answer questions about your timeline.

## Features

- **Browser Automation** — Playwright-based X scraper with stealth plugin, cookie persistence
- **AI Classification** — LLM scores every tweet on 6 dimensions (information density, actionable insights, unique perspectives, experience sharing, key events, emotional resonance)
- **Deep Summarization** — Topic clustering, cross-referencing viewpoints, incremental summaries with state persistence
- **Semantic Search** — Sentence-transformer embeddings for bilingual keyword search
- **Scheduled Digests** — Configurable daily digest push via Feishu webhook/bot
- **Interactive Chat** — 10-intent tool-calling agent (summarize, search, bookmark, recall context, memory management)
- **Long-term Memory** — Extracts user preferences and topic snapshots, merges via embedding similarity

## Quick Start

### Prerequisites

- Python 3.12+
- Playwright Chromium
- LLM API key (DeepSeek or any OpenAI-compatible endpoint)
- Feishu webhook URL (for receiving digests)
- X (Twitter) account

### Installation

```bash
git clone https://github.com/your-username/x-timeline-agent.git
cd x-timeline-agent
pip install -r requirements.txt
playwright install --with-deps chromium
```

### Configuration

```bash
cp .env.example .env
# Edit .env — fill in your LLM_API_KEY and FEISHU_WEBHOOK_URL
```

See `config.yaml` for optional tuning (schedule times, scoring thresholds, etc.).

### Usage

```bash
python main.py setup     # Check configuration
python main.py login     # Open browser to log into X
python main.py fetch     # Test scrape + classify
python main.py digest    # Send a test digest
python main.py chat      # Interactive chat agent
python main.py run       # Start scheduler + Feishu server
```

## Project Structure

```
src/
├── agent/          # Chat agent (core, tools, prompts, memory, summarize, narrative)
├── browser/        # X.com automation (Playwright + stealth)
├── classifier/     # LLM tweet classification
├── digest/         # Digest builder + markdown formatting
├── channels/       # Feishu webhook + enterprise bot
├── scheduler/      # APScheduler jobs (fetch, classify, digest, cleanup)
├── db/             # SQLAlchemy models + repository
└── search/         # Sentence-transformer embeddings + semantic search
```

## License

MIT
