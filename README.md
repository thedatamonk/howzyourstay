# Hostel Feedback Agent

An AI-powered voice agent that automatically calls hostel guests post-checkout to collect structured feedback via a real-time phone conversation.

## What It Does

Hostel operators often struggle to collect meaningful post-stay feedback. This system automates that process:

1. A single API call triggers an outbound phone call to a guest
2. An AI agent conducts a natural voice conversation, covering 9 feedback domains
3. The full transcript is transcribed and summarized into structured insights
4. Results are persisted to a database and retrievable via API

The guest experiences a natural phone conversation; the hostel gets structured, actionable data.

## Core Functionality

- **Outbound calls** — Initiates calls via Twilio to guest phone numbers post-checkout
- **Real-time voice AI** — Streams bidirectional audio between Twilio and OpenAI Realtime API over WebSocket
- **Structured feedback** — AI agent covers 9 domains: cleanliness, staff, amenities, location, room comfort, value for money, social atmosphere, specific incidents, and overall satisfaction
- **Transcript + summary** — Full conversation transcript is captured; GPT-4o-mini generates a structured summary with overview, pain points, highlights, recommendations, and sentiment
- **Async task model** — Calls are initiated in the background; results are polled via a task ID

## Architecture

```mermaid
sequenceDiagram
    participant Client
    participant Server as FastAPI Server
    participant DB as PostgreSQL
    participant Twilio
    participant Guest
    participant OAI as OpenAI Realtime API
    participant GPT as GPT-4o-mini

    Client->>Server: POST /get_feedback
    Server->>DB: Create FeedbackSession (PENDING)
    Server-->>Client: task_id (immediate response)

    Note over Server: Background task starts
    Server->>DB: Update FeedbackSession (IN_PROGRESS)
    Server->>Twilio: Initiate outbound call
    Twilio-->>Server: call SID
    Server->>DB: Store call SID

    Twilio->>Guest: Phone rings
    Twilio->>Server: POST /twilio/status/{task_id} (ringing)

    Guest->>Twilio: Answers
    Twilio->>Server: POST /twilio/voice/{task_id}
    Server-->>Twilio: TwiML: <Say> greeting + <Connect><Stream wss=".../twilio/stream/{task_id}"/>

    Twilio->>Server: WebSocket /twilio/stream/{task_id}
    Server->>OAI: Open Realtime WebSocket + send session config

    loop Conversation (bidirectional audio)
        Twilio->>Server: Audio (μ-law PCM)
        Server->>OAI: input_audio_buffer.append
        OAI->>Server: response.output_audio.delta
        Server->>Twilio: Audio response
        OAI->>Server: Transcription events
        Server->>Server: Append to transcript
    end

    OAI->>Server: response.function_call_arguments.done (end_conversation)
    Server->>OAI: ACK function result
    Server->>OAI: Close Realtime WebSocket
    Server->>Twilio: Close media stream WebSocket

    Note over Server: Post-conversation (still in handle_media_stream)
    Server->>GPT: Transcript → summarize
    GPT-->>Server: Structured summary
    Server->>DB: Update FeedbackSession (COMPLETED) + transcript + summary

    Twilio->>Server: POST /twilio/status/{task_id} (completed)

    Client->>Server: GET /get_feedback/{task_id}
    Server-->>Client: transcript + summary
```

### Key Components

| Component | Description |
|---|---|
| `app.py` | FastAPI application; defines all HTTP and WebSocket endpoints |
| `caller/twilio_client.py` | `CallHandler` — manages Twilio call initiation and the Twilio ↔ OpenAI audio bridge |
| `db/models.py` | SQLAlchemy models (`FeedbackSession`, `SessionStatus` enum) |
| `db/db_session.py` | Async SQLAlchemy session setup |
| `schemas.py` | Pydantic response schemas |
| `helpers.py` | Utility: `require_env()` for validated env var access |
| `guidelines/guidelines_samarya.txt` | Feedback domain guidelines loaded into the AI system prompt |

## Tech Stack

- **FastAPI** — async web framework and WebSocket server
- **OpenAI Realtime API** — bidirectional real-time voice conversation
- **OpenAI GPT-4o-mini** — transcript summarization
- **Twilio Voice** — outbound call management and audio streaming
- **SQLAlchemy (async)** + **asyncpg** — async PostgreSQL ORM
- **PostgreSQL** — persistence for sessions, transcripts, summaries
- **uvicorn** — ASGI server
- **uv** — Python package manager

## Project Structure

```
hostel-feedback-agent/
├── app.py                        # FastAPI app, all endpoints
├── schemas.py                    # Pydantic request/response models
├── helpers.py                    # require_env() utility
├── pyproject.toml                # Project dependencies (uv)
├── docker-compose.yaml           # PostgreSQL container
├── .env                          # Environment variables (not committed)
├── caller/
│   └── twilio_client.py          # CallHandler: Twilio + OpenAI Realtime bridge
├── db/
│   ├── models.py                 # FeedbackSession ORM model
│   └── db_session.py             # Async DB session setup
└── guidelines/
    └── guidelines_samarya.txt    # Feedback areas loaded into AI system prompt
```

## Setup

### Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) package manager
- Docker (for PostgreSQL)
- [Twilio account](https://www.twilio.com/) with a phone number
- OpenAI API key with Realtime API access
- [ngrok](https://ngrok.com/) for local webhook tunneling

### 1. Clone the repository

```bash
git clone <repo-url>
cd hostel-feedback-agent
```

### 2. Configure environment variables

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

```env
# Database
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/hostel_feedback_agent

# Twilio
TWILIO_ACCOUNT_SID=<TWILIO_ACCOUNT_SID>
TWILIO_AUTH_TOKEN=<TWILIO_AUTH_TOKEN>
TWILIO_PHONE_NUMBER=<TWILIO_PHONE_NUMBER>

# Public URL (set this to your ngrok forwarding URL when running locally)
BASE_URL=https://<your-ngrok-subdomain>.ngrok-free.app

# OpenAI
OPENAI_API_KEY=<OPENAI_API_KEY>
OPENAI_REALTIME_MODEL_NAME=gpt-4o-realtime-preview
OPENAI_CHAT_MODEL_NAME=gpt-4o-mini

# App config
RENDER=false
ENV=local
```

### 3. Start PostgreSQL

```bash
docker compose up -d
```

### 4. Install dependencies

```bash
uv sync
```

### 5. Start ngrok

Twilio needs a publicly accessible URL to send webhooks to your local server:

```bash
ngrok http 8000
```

You'll see output like this:

```
Forwarding  https://abc123.ngrok-free.app -> http://localhost:8000
```

Copy the `https://` forwarding URL and set it as `BASE_URL` in your `.env`:

```env
BASE_URL=https://abc123.ngrok-free.app
```

### 6. Start the server

```bash
uv run uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

On first startup, the server automatically creates all required database tables.

## API Reference

### `POST /get_feedback`

Initiates a feedback call for a booking. Returns immediately with a `task_id`.

**Query parameters:**
- `booking_id` (string, required) — the booking identifier

**Response:**
```json
{
  "task_id": "task_a1b2c3d4e5f6",
  "status": "initiated",
  "message": "Feedback call initiated for booking BK-2024-001"
}
```

---

### `GET /get_feedback/{task_id}`

Poll for the status and results of a feedback call.

**Response:**
```json
{
  "task_id": "task_a1b2c3d4e5f6",
  "booking_id": "BK-2024-001",
  "status": "completed",
  "phone_number": "+91xxxxxxxxxx",
  "duration_seconds": 187,
  "transcript": [
    {"role": "agent", "content": "Hi Rohil! Thank you for taking my call..."},
    {"role": "user", "content": "The room was great but the WiFi was slow..."}
  ],
  "summary": {
    "overview": "Guest had a positive experience overall...",
    "painpoints": ["WiFi speed was unreliable"],
    "highlights": ["Friendly staff", "Clean rooms"],
    "recommendations": ["Upgrade WiFi infrastructure"],
    "sentiment": "positive"
  },
  "created_at": "2024-01-20T10:00:00Z",
  "completed_at": "2024-01-20T10:03:07Z"
}
```

Session `status` values: `pending` → `in_progress` → `completed` / `failed`

---

### `GET /health`

Health check.

```json
{"status": "healthy", "service": "hostel-feedback-system"}
```

---

### Internal Twilio Webhooks (called by Twilio, not directly)

| Endpoint | Description |
|---|---|
| `POST /twilio/voice/{task_id}` | Called when guest answers; returns TwiML to connect audio stream |
| `POST /twilio/status/{task_id}` | Call lifecycle status updates from Twilio |
| `WebSocket /twilio/stream/{task_id}` | Bidirectional audio bridge between Twilio and OpenAI Realtime |

## Feedback Collection Areas

The AI agent is guided to collect feedback across 9 domains:

1. **Cleanliness & Hygiene** — rooms, bathrooms, common areas, bedding
2. **Staff & Service** — friendliness, helpfulness, check-in/out experience
3. **Amenities & Facilities** — WiFi, kitchen, lockers, laundry
4. **Location & Accessibility** — transport links, safety, nearby attractions
5. **Room & Comfort** — bed quality, noise, temperature, privacy
6. **Value for Money** — price perception, expectations vs. reality
7. **Social Atmosphere** — community feel, organized activities, common areas
8. **Specific Incidents** — any issues and how they were handled
9. **Overall Satisfaction** — rating, likelihood to recommend, key takeaways
