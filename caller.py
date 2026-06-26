#!/usr/bin/env python3
"""
caller.py — CLI orchestrator for pgai-voice-bot.

Starts ngrok, starts the FastAPI server, then places Twilio calls for each
scenario sequentially. Polls Twilio's own call status API to know when each
call is done (cross-process safe — no shared asyncio state with the server).

After each call the server writes calls/<scenario>_<ts>/result.json with
bug analysis results. This file is read here to build the final summary table.

Usage:
    python caller.py --scenario all
    python caller.py --scenario simple_appointment
    python caller.py --scenario emergency_after_hours --delay 20
"""

import argparse
import glob
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional

import httpx
from dotenv import load_dotenv
from twilio.rest import Client as TwilioClient

from scenario_runner import Scenario, get_all_scenarios, get_scenario

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {"completed", "failed", "busy", "no-answer", "canceled"}


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------
def validate_env() -> None:
    required = [
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_PHONE_NUMBER",
        "DEEPGRAM_API_KEY",
        "GROQ_API_KEY",
    ]
    missing = [v for v in required if not os.getenv(v)]
    if missing:
        print(f"\nERROR: Missing required environment variables:\n  {', '.join(missing)}")
        print("\nCopy .env.example to .env and fill in all values.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Infrastructure startup
# ---------------------------------------------------------------------------
def start_ngrok(port: int) -> str:
    """Start ngrok tunnel and return the stable HTTPS URL.

    The URL is locked for the entire run — ngrok is never restarted mid-run
    because doing so would break any active Twilio webhook URLs.
    """
    from pyngrok import ngrok
    tunnel = ngrok.connect(port, "http")
    url = tunnel.public_url.replace("http://", "https://")
    logger.info(f"ngrok tunnel active: {url}")
    return url


def start_server(port: int, env: dict) -> subprocess.Popen:
    """Launch uvicorn as a subprocess. Returns once the /health endpoint responds."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", str(port)],
        env=env,
    )
    logger.info(f"Starting uvicorn on port {port}...")
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://localhost:{port}/health", timeout=1.0)
            if r.status_code == 200:
                logger.info("Server is ready")
                return proc
        except Exception:
            pass
        time.sleep(1)

    logger.error("Server failed to start within 30s — check for port conflicts or import errors")
    proc.terminate()
    sys.exit(1)


# ---------------------------------------------------------------------------
# Call execution
# ---------------------------------------------------------------------------
def run_scenario(
    scenario: Scenario,
    client: TwilioClient,
    ngrok_url: str,
    target_number: str,
    from_number: str,
    run_log_path: str,
    min_duration: int = 60,
) -> Dict:
    logger.info("")
    logger.info("=" * 64)
    logger.info(f"Scenario: {scenario.name}")
    logger.info(f"  {scenario.description}")
    logger.info("=" * 64)

    start_time = time.time()

    call = client.calls.create(
        to=target_number,
        from_=from_number,
        url=f"{ngrok_url}/call/start/{scenario.name}",
        method="POST",
        record=True,
        recording_status_callback=f"{ngrok_url}/recording",
        recording_status_callback_method="POST",
    )
    call_sid = call.sid
    logger.info(f"Call placed: {call_sid}")

    final_status = _wait_for_call(client, call_sid, ngrok_url, scenario.name)
    call_duration = time.time() - start_time

    # Give the server a moment to finish writing analysis + result.json
    time.sleep(5)
    result_data = _read_result_json(scenario.name)

    warning = "SHORT" if call_duration < min_duration else ""

    log_entry: Dict = {
        "scenario": scenario.name,
        "call_sid": call_sid,
        "status": final_status,
        "duration_seconds": round(call_duration, 1),
        "turns": result_data.get("turns", 0),
        "bugs_count": result_data.get("bugs_count", 0),
        "severity": result_data.get("severity", "UNKNOWN"),
        "result": result_data.get("result", "UNKNOWN"),
        "warning": warning,
        "timestamp": datetime.now().isoformat(),
    }

    if warning:
        logger.warning(
            f"[{scenario.name}] Call completed in {call_duration:.0f}s "
            f"(below --min-duration={min_duration}s) — conversation may have been too short"
        )

    with open(run_log_path, "a") as f:
        f.write(json.dumps(log_entry) + "\n")

    logger.info(
        f"Call done: status={final_status}  duration={call_duration:.0f}s  "
        f"result={log_entry['result']}  severity={log_entry['severity']}"
    )
    return log_entry


def _read_result_json(scenario_name: str, timeout: int = 30) -> dict:
    """Wait for the server to write result.json after bug analysis, then return its contents."""
    pattern = os.path.join("calls", f"{scenario_name}_*", "result.json")
    deadline = time.time() + timeout
    while time.time() < deadline:
        # Take the most recently modified match — calls run sequentially so this is always correct
        matches = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
        if matches:
            try:
                with open(matches[0]) as f:
                    return json.load(f)
            except Exception:
                pass
        time.sleep(2)
    logger.warning(f"result.json not found for scenario '{scenario_name}' after {timeout}s")
    return {}


def _wait_for_recording(call_sid: Optional[str], timeout: int = 45) -> None:
    """Poll up to 45s for recording.mp3; log warning and move on if not received."""
    if not call_sid:
        return
    pattern = os.path.join("calls", "*", "recording.mp3")
    deadline = time.time() + timeout
    while time.time() < deadline:
        matches = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
        if matches and (time.time() - os.path.getmtime(matches[0])) < 120:
            logger.info("Recording file confirmed — safe to shut down.")
            return
        remaining = int(deadline - time.time())
        if remaining % 10 == 0 and remaining > 0:
            logger.info(f"Waiting for Twilio recording... ({remaining}s remaining)")
        time.sleep(5)

    logger.warning(f"Recording not received within {timeout}s — marking timeout in result.json")
    # Update the most recent result.json with recording_timeout flag
    result_pattern = os.path.join("calls", "*", "result.json")
    result_matches = sorted(glob.glob(result_pattern), key=os.path.getmtime, reverse=True)
    if result_matches:
        try:
            with open(result_matches[0]) as f:
                data = json.load(f)
            data["recording_timeout"] = True
            with open(result_matches[0], "w") as f:
                json.dump(data, f)
            logger.info(f"Marked recording_timeout in {result_matches[0]}")
        except Exception as e:
            logger.warning(f"Could not update result.json: {e}")


def _wait_for_call(
    client: TwilioClient,
    call_sid: str,
    ngrok_url: str,
    scenario_name: str,
    max_wait: int = 360,
) -> str:
    """
    Poll Twilio until the call reaches a terminal status.
    On busy/no-answer: retry once after 30s with a fresh call.
    """
    status = _poll_call_status(client, call_sid, max_wait)

    if status in ("busy", "no-answer"):
        logger.warning(f"Call {call_sid} got '{status}', retrying in 30s...")
        time.sleep(30)
        retry_call = client.calls.create(
            to=os.getenv("TARGET_PHONE_NUMBER", ""),
            from_=os.getenv("TWILIO_PHONE_NUMBER", ""),
            url=f"{ngrok_url}/call/start/{scenario_name}",
            method="POST",
            record=True,
            recording_status_callback=f"{ngrok_url}/recording",
            recording_status_callback_method="POST",
        )
        logger.info(f"Retry call placed: {retry_call.sid}")
        status = _poll_call_status(client, retry_call.sid, max_wait)

    return status


def _poll_call_status(client: TwilioClient, call_sid: str, max_wait: int) -> str:
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            status = client.calls(call_sid).fetch().status
        except Exception as e:
            logger.warning(f"Error fetching status for {call_sid}: {e}")
            time.sleep(5)
            continue

        if status in TERMINAL_STATUSES:
            return status

        time.sleep(5)

    logger.warning(f"Timeout waiting for call {call_sid} — forcing complete")
    try:
        client.calls(call_sid).update(status="completed")
    except Exception:
        pass
    return "timeout"


# ---------------------------------------------------------------------------
# Summary output
# ---------------------------------------------------------------------------
def print_summary(results: List[Dict]) -> None:
    print("\n" + "=" * 95)
    print("PGAI VOICE BOT — RUN SUMMARY")
    print("=" * 95)
    print(f"{'Scenario':<30} {'Result':<8} {'Turns':>6} {'Duration':>10} {'Bugs':>5}  {'Severity':<10} {'Warning'}")
    print("-" * 95)
    for r in results:
        name = r.get("scenario", "")[:28]
        result = r.get("result", "?")
        turns = r.get("turns", 0)
        secs = r.get("duration_seconds", 0)
        duration = f"{int(secs)//60}m {int(secs)%60:02d}s"
        bugs = r.get("bugs_count", 0)
        severity = r.get("severity", "?")
        warn = "⚠️  SHORT" if r.get("warning") == "SHORT" else ""
        print(f"{name:<30} {result:<8} {turns:>6} {duration:>10} {bugs:>5}  {severity:<10} {warn}")
    print("=" * 95)
    print()
    print("Full bug report  →  calls/bug_report.md")
    print("Transcripts      →  calls/<scenario>_<timestamp>/transcript.txt")
    print("Recordings       →  calls/<scenario>_<timestamp>/recording.mp3")
    print("Run log          →  calls/run_log.jsonl")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    load_dotenv()
    validate_env()

    parser = argparse.ArgumentParser(
        description="pgai-voice-bot: automated healthcare AI scenario tester",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python caller.py --scenario all\n"
            "  python caller.py --scenario weekend_appointment\n"
            "  python caller.py --scenario all --delay 20"
        ),
    )
    parser.add_argument(
        "--scenario",
        default="all",
        help="Scenario name to run, or 'all' for all 15 (default: all)",
    )
    parser.add_argument(
        "--delay",
        type=int,
        default=15,
        help="Seconds to wait between calls (default: 15)",
    )
    parser.add_argument(
        "--min-duration",
        type=int,
        default=60,
        dest="min_duration",
        help="Minimum expected call duration in seconds; shorter calls get ⚠️ SHORT warning (default: 60)",
    )
    args = parser.parse_args()

    port = int(os.getenv("PORT", "8000"))
    target_number = os.getenv("TARGET_PHONE_NUMBER", "+18054398008")
    from_number = os.getenv("TWILIO_PHONE_NUMBER", "")

    # Resolve scenarios
    if args.scenario == "all":
        scenarios = get_all_scenarios()
    else:
        try:
            scenarios = [get_scenario(args.scenario)]
        except ValueError as e:
            print(f"ERROR: {e}")
            sys.exit(1)

    os.makedirs("calls", exist_ok=True)
    run_log_path = "calls/run_log.jsonl"

    # ---- Start infrastructure (ngrok first, then server) ----
    ngrok_url = start_ngrok(port)
    # Pass NGROK_URL into the server subprocess via environment
    env = {**os.environ, "NGROK_URL": ngrok_url}

    server_proc: Optional[subprocess.Popen] = None
    results: List[Dict] = []

    try:
        server_proc = start_server(port, env)
        client = TwilioClient(
            os.getenv("TWILIO_ACCOUNT_SID"),
            os.getenv("TWILIO_AUTH_TOKEN"),
        )

        for i, scenario in enumerate(scenarios):
            result = run_scenario(
                scenario=scenario,
                client=client,
                ngrok_url=ngrok_url,
                target_number=target_number,
                from_number=from_number,
                run_log_path=run_log_path,
                min_duration=args.min_duration,
            )
            results.append(result)

            if i < len(scenarios) - 1:
                logger.info(f"Waiting {args.delay}s before next call...")
                time.sleep(args.delay)

        # Wait for Twilio to send the recording callback for the last call.
        # Twilio processes recordings asynchronously and the webhook can arrive
        # 15–60s after the call ends — well after result.json is written.
        _wait_for_recording(results[-1]["call_sid"] if results else None)

    finally:
        if server_proc:
            server_proc.terminate()
            logger.info("uvicorn stopped")
        try:
            from pyngrok import ngrok
            ngrok.kill()
            logger.info("ngrok stopped")
        except Exception:
            pass

    print_summary(results)


if __name__ == "__main__":
    main()
