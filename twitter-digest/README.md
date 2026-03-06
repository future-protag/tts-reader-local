# twitter-digest

Fetch and digest Twitter/X content with LLM post-processing.

## Overview

twitter-digest collects tweets from specified accounts or searches, then uses an LLM to summarize and organize the content into a readable digest.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your API keys
```

## Usage

```bash
python twitter_digest.py
```

## Configuration

Configure in `.env`:
- `TWITTER_BEARER_TOKEN` — Twitter API v2 bearer token
- `LLM_API_KEY` — API key for the LLM provider used for digest generation
