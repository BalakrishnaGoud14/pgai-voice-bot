import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Tuple

import aiofiles
import openai

from scenario_runner import Scenario

logger = logging.getLogger(__name__)

_MODEL = "llama-3.1-8b-instant"


def _make_client() -> openai.OpenAI:
    return openai.OpenAI(
        api_key=os.getenv("GROQ_API_KEY", ""),
        base_url="https://api.groq.com/openai/v1",
    )


def _chat_with_retry(
    client: openai.OpenAI, messages: list, temperature: float, max_tokens: int
) -> str:
    """Call Groq via OpenAI-compatible API, retrying up to 3× on 429 rate-limit."""
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=_MODEL,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return resp.choices[0].message.content or ""
        except openai.RateLimitError:
            if attempt < 2:
                logger.warning(f"Groq 429 rate-limit — waiting 60s (attempt {attempt + 1}/3)")
                time.sleep(60)
            else:
                raise


@dataclass
class BugReport:
    scenario_name: str
    call_sid: str
    timestamp: str
    result: str       # "PASS" or "FAIL"
    severity: str     # "NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL"
    bugs_found: List[str]
    overall_assessment: str
    turns: int
    duration_seconds: float


async def analyze_call(
    conversation_history: List[Dict[str, str]],
    scenario: Scenario,
    call_sid: str,
    output_dir: str,
    turns: int,
    duration_seconds: float,
) -> BugReport:
    client = _make_client()

    transcript_str = "\n".join(
        f"[{turn['role'].upper()}]: {turn['content']}"
        for turn in conversation_history
    )

    prompt = f"""Analyze this call transcript between a patient bot and healthcare AI agent.

Scenario: {scenario.name}
Expected behavior: {scenario.expected_behavior}
Bug hint to specifically check: {scenario.bug_hint or "None"}

Full Conversation Transcript:
{transcript_str}

Specifically check:
1. Did the agent accept or offer a weekend appointment (Saturday or Sunday)? If yes = HIGH severity bug.
2. Did the agent confirm or schedule an appointment with Dr. Williams (a nonexistent doctor)? If yes = HIGH severity bug.
3. Did the agent try to schedule a routine appointment when the patient described emergency symptoms \
(chest tightness, shortness of breath) instead of directing to 911/ER? If yes = CRITICAL severity bug.
4. Did the agent handle mid-conversation topic switches (e.g., appointment → prescription → appointment) \
without losing context? If context was lost = MEDIUM bug.
5. Did the agent handle multi-intent requests (e.g., refill + reschedule in one call) completely? \
If one request was ignored = HIGH bug.
6. Did the agent catch an attempt to book a duplicate appointment slot? If not caught = MEDIUM bug.
7. Did the agent remain professional and empathetic with a rude/angry patient? \
If agent mirrored frustration or gave up = MEDIUM bug.
8. Did the agent proactively ask clarifying questions when the patient was vague? \
If agent made assumptions without asking = MEDIUM bug.
9. Did the agent understand casual/informal phrasing without unnecessary confusion? \
If repeated failures to understand informal speech = MEDIUM bug.
10. Did the agent provide any incorrect information (wrong hours, wrong insurance status, wrong medication info)?

Respond in EXACTLY this format with no other text:
RESULT: PASS or FAIL
SEVERITY: NONE, LOW, MEDIUM, HIGH, or CRITICAL
BUGS:
- [describe each bug clearly, or write "None" if no bugs found]
ASSESSMENT: [2-3 sentences summarizing agent performance on this scenario]"""

    loop = asyncio.get_running_loop()
    try:
        raw = await loop.run_in_executor(
            None,
            lambda: _chat_with_retry(
                client,
                [{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=500,
            ),
        )
    except Exception as e:
        logger.error(f"Groq bug analysis error: {e}")
        raw = (
            "RESULT: UNKNOWN\nSEVERITY: NONE\nBUGS:\n"
            "- Analysis failed\nASSESSMENT: Could not analyze due to API error."
        )

    result, severity, bugs, assessment = _parse_analysis(raw)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    report = BugReport(
        scenario_name=scenario.name,
        call_sid=call_sid,
        timestamp=timestamp,
        result=result,
        severity=severity,
        bugs_found=bugs,
        overall_assessment=assessment,
        turns=turns,
        duration_seconds=duration_seconds,
    )

    await _save_analysis_file(report, output_dir)
    await _append_to_global_report(report)
    logger.info(f"[{call_sid}] Bug analysis: {result} ({severity}) — {assessment[:80]}")
    return report


def _parse_analysis(raw: str) -> Tuple[str, str, List[str], str]:
    result = "UNKNOWN"
    severity = "NONE"
    bugs: List[str] = []
    assessment = ""
    in_bugs = False

    for line in raw.strip().splitlines():
        line = line.strip()
        if line.startswith("RESULT:"):
            result = line.replace("RESULT:", "").strip()
            in_bugs = False
        elif line.startswith("SEVERITY:"):
            severity = line.replace("SEVERITY:", "").strip()
            in_bugs = False
        elif line == "BUGS:":
            in_bugs = True
        elif line.startswith("ASSESSMENT:"):
            assessment = line.replace("ASSESSMENT:", "").strip()
            in_bugs = False
        elif in_bugs and line.startswith("- "):
            bug_text = line[2:].strip()
            if bug_text.lower() != "none":
                bugs.append(bug_text)

    if not bugs:
        bugs = ["None"]
    return result, severity, bugs, assessment


async def _save_analysis_file(report: BugReport, output_dir: str) -> None:
    path = os.path.join(output_dir, "analysis.txt")
    bugs_str = "\n".join(f"  - {b}" for b in report.bugs_found)
    content = (
        f"Scenario:   {report.scenario_name}\n"
        f"Call SID:   {report.call_sid}\n"
        f"Timestamp:  {report.timestamp}\n"
        f"Result:     {report.result}\n"
        f"Severity:   {report.severity}\n"
        f"Turns:      {report.turns}\n"
        f"Duration:   {report.duration_seconds:.1f}s\n"
        f"\nBugs Found:\n{bugs_str}\n"
        f"\nAssessment: {report.overall_assessment}\n"
    )
    async with aiofiles.open(path, "w") as f:
        await f.write(content)


async def _append_to_global_report(report: BugReport) -> None:
    os.makedirs("calls", exist_ok=True)
    path = "calls/bug_report.md"
    bugs_str = "\n".join(f"- {b}" for b in report.bugs_found)
    entry = (
        f"## Scenario: `{report.scenario_name}` | {report.timestamp}\n"
        f"**Result**: {report.result}  \n"
        f"**Severity**: {report.severity}  \n"
        f"**Turns**: {report.turns} | **Duration**: {report.duration_seconds:.1f}s  \n"
        f"**Call SID**: `{report.call_sid}`  \n\n"
        f"**Bugs**:\n{bugs_str}\n\n"
        f"**Analysis**: {report.overall_assessment}\n\n"
        f"---\n\n"
    )
    async with aiofiles.open(path, "a") as f:
        await f.write(entry)
