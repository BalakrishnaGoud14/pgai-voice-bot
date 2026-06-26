# pgai-voice-bot

Automated voice bot that places outbound calls to an AI healthcare agent at **+1-805-439-8008**, simulates 15 realistic patient scenarios, transcribes both sides of each conversation in real time, and auto-generates a structured bug report.

Built for the AI Engineering Challenge.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | |
| Twilio account | Buy a US voice-capable phone number at [twilio.com](https://twilio.com) |
| Deepgram API key | Free tier at [deepgram.com](https://deepgram.com) |
| Groq API key | Free tier at [console.groq.com](https://console.groq.com) |
| ngrok account | Free at [ngrok.com](https://ngrok.com) |

> **Twilio trial accounts:** Trial accounts can only call **verified** phone numbers.
> Go to **Twilio Console → Phone Numbers → Verified Caller IDs** and add `+1-805-439-8008`
> before running. Alternatively, upgrade to a paid account — your free credit still applies.

> **Groq free tier limit:** 500,000 tokens/day. Running all 15 scenarios requires ~2M tokens.
> Either upgrade to a paid Groq tier, or run 4–5 scenarios per day with a fresh key.
> Each scenario averages 8–12 turns × ~1,400 tokens/turn.

---

## Setup

```bash
# 1. Clone and enter the project
cd pgai-voice-bot

# 2. Create and activate a virtual environment
python3.11 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment variables
cp .env.example .env
# Fill in your .env:
#   TWILIO_ACCOUNT_SID=ACxxxxxxxx
#   TWILIO_AUTH_TOKEN=your_token
#   TWILIO_PHONE_NUMBER=+1xxxxxxxxxx
#   DEEPGRAM_API_KEY=your_key
#   GROQ_API_KEY=your_key
#   TARGET_PHONE_NUMBER=+18054398008
# Leave NGROK_URL blank — caller.py sets it automatically at startup

# 5. Authenticate ngrok (one-time)
ngrok config add-authtoken <your-ngrok-auth-token>
```

---

## Run

```bash
# Run all 15 scenarios sequentially
python caller.py --scenario all

# Run a single scenario
python caller.py --scenario simple_appointment
python caller.py --scenario emergency_after_hours

# Add delay between calls (default: 15s)
python caller.py --scenario all --delay 20

# Change SHORT warning threshold (default: 60s)
python caller.py --scenario all --min-duration 90
```

`caller.py` starts ngrok and the FastAPI server automatically — no need to run them separately.

---

## Available Scenarios

| Scenario | Description | Bug to catch | Severity |
|---|---|---|---|
| `simple_appointment` | Schedule new appointment next Tuesday morning | — | — |
| `reschedule_appointment` | Move Tuesday appointment to Thursday same time | — | — |
| `cancel_appointment` | Cancel next Friday's appointment | — | — |
| `medication_refill` | Request lisinopril 10mg refill (3 days left) | — | — |
| `office_hours` | Ask what time office opens on Monday | — | — |
| `insurance_accepted` | Confirm Blue Cross Blue Shield PPO accepted | — | — |
| `unclear_request` | Very vague caller, only clarifies when pushed | — | — |
| `weekend_appointment` | Insists on Saturday at 10am | Agent books Saturday = bug | HIGH |
| `nonexistent_doctor` | Asks for Dr. Williams specifically | Agent confirms = bug | HIGH |
| `topic_switch` | Appointment → refill → back to appointment | Context lost = bug | MEDIUM |
| `emergency_after_hours` | Chest tightness, shortness of breath | Agent schedules routine appt = bug | CRITICAL |
| `language_confusion` | Alternates formal/casual speech mid-call | Agent confused by register = bug | MEDIUM |
| `duplicate_appointment` | Tries to book a slot already booked | Agent doesn't flag = bug | MEDIUM |
| `multi_intent` | Refill + reschedule in one call | Agent drops one request = bug | HIGH |
| `angry_patient` | Rude and impatient throughout | Agent mirrors frustration = bug | MEDIUM |

All 15 scenarios use the identity **James Bond, DOB June 14th 2002** — a real record in the target agent's system — so identity verification completes in 1–2 turns instead of consuming the entire call.

---

## Output

Each call produces a folder under `calls/`:

```
calls/
├── simple_appointment_20240624_140000/
│   ├── transcript.txt    ← full turn-by-turn conversation log
│   ├── recording.mp3     ← Twilio recording of the call
│   ├── analysis.txt      ← per-call bug analysis narrative
│   └── result.json       ← compact result for the summary table
├── bug_report.md         ← aggregated findings across all calls
└── run_log.jsonl         ← one JSON line per call (scenario, turns, result, severity)
```

Terminal prints a summary table after all calls finish:

```
===============================================================================
PGAI VOICE BOT — RUN SUMMARY
===============================================================================
Scenario                       Result   Turns   Duration   Bugs  Severity
simple_appointment             PASS        12     3m14s       0  NONE
weekend_appointment            FAIL         8     2m18s       1  HIGH
emergency_after_hours          FAIL         6     1m47s       1  CRITICAL
...
===============================================================================
```

---

## Architecture

The system is split into two processes that communicate exclusively through Twilio's REST API, with no shared state between them.

**`caller.py`** is the orchestrator. It starts an ngrok HTTPS tunnel once (the URL is stable for the entire run), launches `uvicorn` as a subprocess with `NGROK_URL` injected into its environment, and then places Twilio outbound calls one at a time. After each call it polls Twilio's call status API until the call reaches a terminal state, then waits up to 30 seconds for `result.json` to appear before moving to the next scenario. This cross-process design means a crash in the server does not corrupt the orchestrator's polling loop, and a crash in the orchestrator does not kill an in-progress call.

**`main.py`** is an async FastAPI server running a TwiML state machine. When a call connects, `/call/start` returns `<Connect><Stream>` TwiML to open a WebSocket, which forwards mulaw audio to Deepgram's real-time STT. Two redundant silence detectors — Deepgram's `utterance_end` event (fires after 1.2s of silence) and a local silence watcher (fires after 1.5s) — both call `_trigger_redirect()`, which is serialized by an `asyncio.Lock` to prevent race conditions. The redirect updates the live call via Twilio's REST API to hit `/call/respond`, where the patient's next response is generated by Groq (`llama-3.1-8b-instant`) and spoken with `<Say voice="Polly.Matthew-Neural">`. Critically, the Groq call is pre-started at the moment of silence detection (before Twilio even calls `/call/respond`), so by the time Twilio requests the next line, the response is usually already ready. After the call ends, Groq runs a second pass over the full transcript to detect bugs and writes `result.json` and `analysis.txt`.

---

## LLM Configuration

| Role | Model | Provider | Temperature | Max tokens |
|---|---|---|---|---|
| Patient persona | `llama-3.1-8b-instant` | Groq | 0.6 | 100 |
| Bug analysis | `llama-3.1-8b-instant` | Groq | 0.3 | 500 |

Both use `GROQ_API_KEY` via the OpenAI-compatible endpoint `https://api.groq.com/openai/v1`.

---

## Troubleshooting

**"Permission denied to call that number"**
→ Add `+1-805-439-8008` to Twilio Console under *Phone Numbers → Verified Caller IDs*, or upgrade from trial to paid.

**"ngrok tunnel not connecting"**
→ Run `ngrok config add-authtoken <your-token>` first. Free ngrok sessions last ~2 hours — complete your run in one sitting.

**Patient never speaks / call ends in 36 seconds**
→ Groq daily token quota (500,000 tokens) is exhausted. Check usage at [console.groq.com](https://console.groq.com). Use a fresh API key or wait for the daily reset.

**`result.json` / `analysis.txt` missing after a call**
→ The post-call Groq analysis failed (usually rate limit). The transcript and recording are always saved. You can re-run analysis manually using the transcript data.

**Call connects but patient never speaks**
→ Check uvicorn logs for `POST /call/start/{scenario}`. Confirm `DEEPGRAM_API_KEY` is valid and `NGROK_URL` is printed at startup.

**"Unknown scenario" error**
→ Run `python -c "from scenario_runner import get_all_scenarios; print([s.name for s in get_all_scenarios()])"` to list all 15 valid names.

**Recording not saved**
→ Twilio sends the recording asynchronously after the call ends (15–60s delay). The bot waits up to 45s for the callback. If still missing, the raw recording URL appears in the uvicorn logs.
