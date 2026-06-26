import asyncio
import base64
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import aiofiles
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import Connect, VoiceResponse

from audio_handler import DeepgramSTT
from bug_reporter import analyze_call
from patient_agent import PatientAgent
from scenario_runner import Scenario, get_scenario

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state (single asyncio event loop — no locks needed for dicts)
# ---------------------------------------------------------------------------
call_sessions: Dict[str, "CallSession"] = {}
# Kept after session ends so the /recording webhook can find the output dir
completed_sessions: Dict[str, str] = {}

twilio_client: Optional[TwilioClient] = None
TWILIO_ACCOUNT_SID: str = ""
TWILIO_AUTH_TOKEN: str = ""
NGROK_URL: str = ""

@dataclass
class CallSession:
    scenario: Scenario
    call_sid: str
    patient_agent: PatientAgent
    output_dir: str
    conversation_history: List[Dict[str, str]] = field(default_factory=list)
    deepgram_stt: Optional[DeepgramSTT] = None
    agent_transcript_buffer: str = ""
    last_speech_final_time: float = 0.0
    turn_count: int = 0
    call_start_time: float = field(default_factory=time.time)
    is_responding: bool = False
    # Dynamic greeting detection (Fix 1)
    speech_final_count: int = 0
    greeting_complete: bool = False
    # Groq response pre-started at silence detection, awaited in /call/respond
    pregenerated_task: Optional[asyncio.Task] = None
    pregenerated_agent_text: str = ""
    # asyncio.Lock serializes the two competing triggers (utterance_end + silence watcher)
    redirect_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global twilio_client, NGROK_URL, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
    load_dotenv()
    TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
    NGROK_URL = os.getenv("NGROK_URL", "")
    twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    os.makedirs("calls", exist_ok=True)
    logger.info(f"Server ready. NGROK_URL={NGROK_URL}")
    yield
    logger.info("Server shutting down")


app = FastAPI(lifespan=lifespan)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _stream_twiml(call_sid: str) -> str:
    """TwiML that opens a Twilio Media Stream WebSocket to this server."""
    host = NGROK_URL.replace("https://", "").replace("http://", "")
    stream_url = f"wss://{host}/media-stream/{call_sid}"
    response = VoiceResponse()
    connect = Connect()
    connect.stream(url=stream_url)
    response.append(connect)
    return str(response)


def _xml(twiml: str) -> Response:
    return Response(content=twiml, media_type="application/xml")


async def _run_call_analysis(session: "CallSession") -> None:
    """Save final transcript + run bug analysis + write result.json."""
    call_sid = session.call_sid
    duration = time.time() - session.call_start_time
    transcript_lines = [
        f"Scenario:  {session.scenario.name}",
        f"Call SID:  {call_sid}",
        f"Timestamp: {datetime.now().isoformat()}",
        f"Duration:  {duration:.1f}s",
        f"Turns:     {session.turn_count}",
        "",
        "=== CONVERSATION ===",
        "",
    ]
    for turn in session.conversation_history:
        transcript_lines.append(f"[{turn['role'].upper()}]: {turn['content']}")
        transcript_lines.append("")
    try:
        async with aiofiles.open(os.path.join(session.output_dir, "transcript.txt"), "w") as f:
            await f.write("\n".join(transcript_lines))
    except Exception as e:
        logger.error(f"[{call_sid}] Transcript save failed: {e}")
    try:
        report = await analyze_call(
            conversation_history=session.conversation_history,
            scenario=session.scenario,
            call_sid=call_sid,
            output_dir=session.output_dir,
            turns=session.turn_count,
            duration_seconds=duration,
        )
        bugs_count = len([b for b in report.bugs_found if b.lower() != "none"])
        result_data = json.dumps({
            "scenario": session.scenario.name,
            "call_sid": call_sid,
            "result": report.result,
            "severity": report.severity,
            "bugs_count": bugs_count,
            "turns": report.turns,
            "duration_seconds": report.duration_seconds,
        })
        async with aiofiles.open(os.path.join(session.output_dir, "result.json"), "w") as f:
            await f.write(result_data)
        logger.info(f"[{call_sid}] Analysis done: {report.result} / {report.severity}")
    except Exception as e:
        logger.error(f"[{call_sid}] Analysis failed: {e}")


async def _save_transcript(session: "CallSession") -> None:
    """Write conversation history to transcript.txt (called after every turn)."""
    lines = [
        f"Scenario:  {session.scenario.name}",
        f"Call SID:  {session.call_sid}",
        f"Timestamp: {datetime.now().isoformat()}",
        f"Turns:     {session.turn_count}",
        "",
    ]
    for entry in session.conversation_history:
        role = entry["role"].upper()
        lines.append(f"[{role}]: {entry['content']}")
    try:
        async with aiofiles.open(
            os.path.join(session.output_dir, "transcript.txt"), "w"
        ) as f:
            await f.write("\n".join(lines))
    except Exception as e:
        logger.error(f"Failed to save transcript: {e}")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})



@app.post("/call/start/{scenario_name}")
async def call_start(scenario_name: str, request: Request):
    """
    Twilio's first webhook when the outbound call connects.
    Creates session state and returns TwiML to open a media stream immediately
    so we catch the agent's opening greeting.
    """
    form = await request.form()
    call_sid = form.get("CallSid", "")
    if not call_sid:
        return _xml("<Response><Hangup/></Response>")

    try:
        scenario = get_scenario(scenario_name)
    except ValueError as e:
        logger.error(f"Scenario error: {e}")
        return _xml("<Response><Say>Configuration error.</Say><Hangup/></Response>")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = os.path.join("calls", f"{scenario_name}_{timestamp}")
    os.makedirs(output_dir, exist_ok=True)

    session = CallSession(
        scenario=scenario,
        call_sid=call_sid,
        patient_agent=PatientAgent(scenario),
        output_dir=output_dir,
    )
    call_sessions[call_sid] = session
    logger.info(f"[{call_sid}] Session created: scenario={scenario_name} dir={output_dir}")

    return _xml(_stream_twiml(call_sid))


@app.post("/call/respond/{call_sid}")
async def call_respond(call_sid: str):
    """
    Called by Twilio after the redirect triggered by silence/utterance detection.
    Reads the buffered agent transcript, generates the patient's response via Groq,
    and returns TwiML with <Say> followed by a redirect to /call/listen.
    """
    session = call_sessions.get(call_sid)
    if not session:
        return _xml("<Response><Hangup/></Response>")

    agent_text = session.agent_transcript_buffer.strip()
    session.agent_transcript_buffer = ""
    session.is_responding = False  # Reset for the next turn

    if agent_text:
        session.conversation_history.append({"role": "agent", "content": agent_text})
        logger.info(f"[{call_sid}] Agent said: {agent_text[:120]}")
    else:
        # Nothing was transcribed yet — re-open the stream and keep listening
        logger.warning(f"[{call_sid}] Empty agent transcript, resuming listen")
        if session.pregenerated_task:
            session.pregenerated_task.cancel()
            session.pregenerated_task = None
        return _xml(_stream_twiml(call_sid))

    # Use pre-generated response if available and text matches
    if session.pregenerated_task and session.pregenerated_agent_text == agent_text:
        t0 = time.time()
        patient_text, is_done = await session.pregenerated_task
        session.pregenerated_task = None
        logger.info(f"[{call_sid}] Groq wait: {time.time()-t0:.2f}s (pre-generated)")
    else:
        if session.pregenerated_task:
            session.pregenerated_task.cancel()
            session.pregenerated_task = None
        patient_text, is_done = await session.patient_agent.generate_response(agent_text, turn_count=session.turn_count)
    session.conversation_history.append({"role": "patient", "content": patient_text})
    session.turn_count += 1
    logger.info(f"[{call_sid}] Patient turn {session.turn_count}: {patient_text[:120]}")

    # Save transcript after every turn so it's never lost if the call ends unexpectedly
    asyncio.create_task(_save_transcript(session))

    response = VoiceResponse()
    response.say(patient_text, voice=session.scenario.tts_voice, language="en-US")

    if is_done or session.turn_count >= session.scenario.max_turns:
        response.redirect(f"{NGROK_URL}/call/end/{call_sid}", method="POST")
    else:
        response.redirect(f"{NGROK_URL}/call/listen/{call_sid}", method="POST")

    return _xml(str(response))


@app.post("/call/listen/{call_sid}")
async def call_listen(call_sid: str):
    """
    Resets the buffer and opens a fresh media stream to listen to the next agent turn.
    Keeping this as a separate endpoint from /call/start ensures buffer reset
    happens deterministically at endpoint entry, not inside the WebSocket handler.
    """
    session = call_sessions.get(call_sid)
    if not session:
        return _xml("<Response><Hangup/></Response>")

    session.agent_transcript_buffer = ""
    session.last_speech_final_time = 0.0
    return _xml(_stream_twiml(call_sid))


@app.post("/call/end/{call_sid}")
async def call_end(call_sid: str):
    """Saves transcript, runs bug analysis, and hangs up."""
    session = call_sessions.pop(call_sid, None)

    if session:
        if session.pregenerated_task and not session.pregenerated_task.done():
            session.pregenerated_task.cancel()
            session.pregenerated_task = None
        completed_sessions[call_sid] = session.output_dir
        await _run_call_analysis(session)

    voice = session.scenario.tts_voice if session else "Polly.Joanna-Neural"
    response = VoiceResponse()
    response.say("Thank you, goodbye.", voice=voice, language="en-US")
    response.hangup()
    return _xml(str(response))


@app.post("/recording")
async def recording_webhook(request: Request):
    """
    Twilio fires this after the recording is ready.
    We schedule the download as a background task — Twilio sometimes needs a few
    seconds to finalize the file after the webhook fires.
    """
    form = await request.form()
    recording_url = str(form.get("RecordingUrl", ""))
    call_sid = str(form.get("CallSid", ""))

    output_dir = completed_sessions.get(call_sid) or next(
        (s.output_dir for s in call_sessions.values() if s.call_sid == call_sid),
        None,
    )

    if output_dir and recording_url:
        asyncio.create_task(_download_recording(recording_url, call_sid, output_dir))
    else:
        logger.warning(f"[{call_sid}] Recording webhook missing data: url={recording_url}")

    return Response("OK")


async def _download_recording(url: str, call_sid: str, output_dir: str) -> None:
    # Give Twilio time to finalize the recording file
    await asyncio.sleep(3)
    mp3_url = f"{url}.mp3"
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    mp3_url,
                    auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
                    follow_redirects=True,
                    timeout=30.0,
                )
            if resp.status_code == 200:
                path = os.path.join(output_dir, "recording.mp3")
                async with aiofiles.open(path, "wb") as f:
                    await f.write(resp.content)
                logger.info(f"[{call_sid}] Recording saved: {path}")
                return
            elif resp.status_code == 404:
                logger.warning(f"[{call_sid}] Recording not ready (attempt {attempt}/3), retrying in 3s")
                await asyncio.sleep(3)
            else:
                logger.error(f"[{call_sid}] Recording download failed: HTTP {resp.status_code}")
                return
        except Exception as e:
            logger.error(f"[{call_sid}] Recording download error (attempt {attempt}/3): {e}")
            await asyncio.sleep(3)


# ---------------------------------------------------------------------------
# WebSocket — Twilio Media Stream bridge
# ---------------------------------------------------------------------------
@app.websocket("/media-stream/{call_sid}")
async def media_stream(websocket: WebSocket, call_sid: str):
    """
    Core real-time loop:
      Twilio → mulaw audio chunks → Deepgram STT → silence detection → Twilio redirect
    """
    await websocket.accept()
    session = call_sessions.get(call_sid)
    if not session:
        logger.warning(f"[{call_sid}] No session for media stream — closing")
        await websocket.close()
        return

    silence_task: Optional[asyncio.Task] = None
    loop = asyncio.get_running_loop()
    # True only if _trigger_redirect fired from THIS stream instance.
    # The finally block uses this to distinguish a normal redirect-close from
    # an agent hangup — if we redirected, /call/listen will open the next stream.
    redirected_this_stream = False

    # ------------------------------------------------------------------
    # Transcript callback — dynamic greeting detection
    # ------------------------------------------------------------------
    async def on_transcript(text: str, is_speech_final: bool) -> None:
        session.agent_transcript_buffer += " " + text
        if not is_speech_final:
            return

        session.last_speech_final_time = time.time()
        session.speech_final_count += 1
        logger.debug(f"[{call_sid}] speech_final #{session.speech_final_count}: {text[:60]}")

        if session.speech_final_count == 1:
            logger.info(f"[{call_sid}] speech_final #1 received")

        elif session.speech_final_count == 2:
            # Real greeting complete — open turn-taking
            session.greeting_complete = True
            logger.info(f"[{call_sid}] Greeting complete (speech_final #2)")

        # speech_final_count > 2: normal turn — silence watcher handles redirect

    # ------------------------------------------------------------------
    # Utterance-end callback — only fires after greeting is complete
    # ------------------------------------------------------------------
    async def on_utterance_end() -> None:
        logger.debug(f"[{call_sid}] utterance_end event")
        call_elapsed = time.time() - session.call_start_time
        if not session.greeting_complete and call_elapsed < 12.0:
            logger.debug(f"[{call_sid}] utterance_end ignored — greeting not detected yet")
            return
        if not session.greeting_complete:
            session.greeting_complete = True
            logger.info(f"[{call_sid}] Greeting fallback: forcing complete at {call_elapsed:.1f}s")
        await _trigger_redirect()

    # ------------------------------------------------------------------
    # Core redirect logic — protected by asyncio.Lock to prevent the
    # silence watcher and utterance_end from both firing the redirect.
    # ------------------------------------------------------------------
    async def _trigger_redirect() -> None:
        nonlocal redirected_this_stream
        async with session.redirect_lock:
            if session.is_responding:
                return
            agent_text = session.agent_transcript_buffer.strip()
            if not agent_text:
                return
            session.is_responding = True
            redirected_this_stream = True

            # If the speculative task was started for a different (shorter) text,
            # cancel it and start a fresh generate_response with the full text.
            # If it matches (or is close), /call/respond will use it directly.
            if (
                not session.pregenerated_task
                or session.pregenerated_agent_text != agent_text
            ):
                if session.pregenerated_task:
                    session.pregenerated_task.cancel()
                session.pregenerated_agent_text = agent_text
                session.pregenerated_task = asyncio.create_task(
                    session.patient_agent.generate_response(agent_text, turn_count=session.turn_count)
                )

            logger.info(f"[{call_sid}] Redirecting to /call/respond")
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda: twilio_client.calls(call_sid).update(
                            url=f"{NGROK_URL}/call/respond/{call_sid}",
                            method="POST",
                        ),
                    ),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                logger.error(f"[{call_sid}] Twilio REST timeout — releasing lock")
                session.is_responding = False
            except Exception as e:
                logger.error(f"[{call_sid}] Twilio redirect failed: {e}")
                session.is_responding = False

    # ------------------------------------------------------------------
    # Silence watcher — fires after 1s of silence once greeting is done
    # ------------------------------------------------------------------
    async def silence_watcher() -> None:
        try:
            while True:
                await asyncio.sleep(0.1)
                if session.is_responding:
                    break
                if session.last_speech_final_time > 0:
                    elapsed = time.time() - session.last_speech_final_time
                    call_elapsed = time.time() - session.call_start_time
                    in_fallback = not session.greeting_complete and call_elapsed >= 12.0
                    ready = session.greeting_complete or in_fallback
                    # Greeting fallback: fire at 0.5s (agent has finished, buffer has content).
                    # Mid-call: 2.5s backup — utterance_end (2.0s) fires first, preventing interrupts.
                    threshold = 0.5 if in_fallback else 1.5
                    if elapsed >= threshold and ready:
                        if not session.greeting_complete:
                            session.greeting_complete = True
                            logger.info(f"[{call_sid}] Greeting fallback: forcing complete at {call_elapsed:.1f}s")
                        logger.debug(f"[{call_sid}] Silence timeout ({elapsed:.1f}s)")
                        await _trigger_redirect()
                        break
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Connect Deepgram
    # ------------------------------------------------------------------
    stt = DeepgramSTT(on_transcript=on_transcript, on_utterance_end=on_utterance_end)
    session.deepgram_stt = stt
    try:
        await stt.connect()
    except Exception as e:
        logger.error(f"[{call_sid}] Deepgram connect failed: {e}")
        await websocket.close()
        return

    silence_task = asyncio.create_task(silence_watcher())

    # ------------------------------------------------------------------
    # Main message loop — forward Twilio audio to Deepgram
    # ------------------------------------------------------------------
    try:
        async for message in websocket.iter_text():
            data = json.loads(message)
            event = data.get("event")

            if event == "connected":
                logger.info(f"[{call_sid}] Twilio stream connected")

            elif event == "start":
                sid = data.get("start", {}).get("streamSid", "")
                logger.info(f"[{call_sid}] Stream started (streamSid={sid})")

            elif event == "media":
                payload = data.get("media", {}).get("payload", "")
                if payload:
                    await stt.send_audio(base64.b64decode(payload))

            elif event == "stop":
                logger.info(f"[{call_sid}] Stream stopped by Twilio")
                break

    except WebSocketDisconnect:
        logger.info(f"[{call_sid}] WebSocket disconnected")
    except Exception as e:
        logger.error(f"[{call_sid}] WebSocket error: {e}")
    finally:
        if silence_task and not silence_task.done():
            silence_task.cancel()
        await stt.close()
        # Only run fallback analysis if this stream closed due to agent hangup.
        # If we issued a redirect, the stream closes normally and /call/listen
        # will open the next stream — the session must NOT be consumed here.
        if not redirected_this_stream and call_sid in call_sessions:
            ended_session = call_sessions.pop(call_sid)
            completed_sessions[call_sid] = ended_session.output_dir
            logger.info(f"[{call_sid}] Remote hangup — running analysis")
            asyncio.create_task(_run_call_analysis(ended_session))
