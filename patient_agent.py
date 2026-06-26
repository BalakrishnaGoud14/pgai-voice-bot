import asyncio
import logging
import os
import time
from datetime import date
from typing import Dict, List, Tuple

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


# Per-scenario behavioral addenda injected into the system prompt
_EMOTION_RULES: Dict[str, str] = {
    "simple_appointment":      "friendly, slightly hopeful",
    "reschedule_appointment":  "apologetic, slightly rushed",
    "cancel_appointment":      "apologetic, brief",
    "medication_refill":       "slightly anxious (running out soon)",
    "office_hours":            "casual, just curious",
    "insurance_accepted":      "cautious, wants to confirm before committing",
    "unclear_request":         "confused, vague, only clarify when pushed",
    "weekend_appointment":     "hopeful, slightly disappointed when rejected",
    "nonexistent_doctor":      "confident, slightly confused when told wrong",
    "topic_switch":            "scattered, easily distracted",
    "emergency_after_hours":   "genuinely distressed, short of breath",
    "language_confusion":      "alternates formal/casual naturally",
    "duplicate_appointment":   "forgetful, slightly embarrassed",
    "multi_intent":            "rushed, wants everything handled at once",
    "angry_patient":           "frustrated, impatient, clipped responses",
}

_SCENARIO_RULES: Dict[str, str] = {
    "angry_patient": (
        "\n\nSCENARIO NOTE: You are consistently rude and impatient throughout. "
        "Express frustration freely ('This is ridiculous', 'Why is this so hard?', "
        "'I've been waiting forever'). Do not soften your tone even when the agent is polite. "
        "SHORT CLIPPED SENTENCES ONLY — maximum 8 words per sentence. "
        "e.g. 'That\\'s not good enough.' 'I need this fixed.' 'Just do it.'"
    ),
    "unclear_request": (
        "\n\nSCENARIO NOTE: Begin the call extremely vague — 'I need some help with something...' "
        "Reveal more specific information ONLY when the agent asks a direct question. "
        "Never volunteer details before being asked."
    ),
    "emergency_after_hours": (
        "\n\nSCENARIO NOTE: You are genuinely distressed and struggling to breathe. "
        "Use SHORT BREATHLESS sentences — you physically cannot speak in long phrases. "
        "e.g. 'I can\\'t... I can\\'t breathe right.' 'It\\'s bad.' 'Please, what do I do?' "
        "Convey fear and urgency. Do not speak in full composed sentences."
    ),
    "language_confusion": (
        "\n\nSCENARIO NOTE: Alternate unpredictably between very formal language "
        "('I would like to inquire about the available appointments') and very casual language "
        "('yeah so like when can I come in?'). Switch registers at least twice mid-conversation."
    ),
    "topic_switch": (
        "\n\nSCENARIO NOTE: You are a scattered thinker."
        " Follow this EXACT sequence:\n"
        "Turn 1-2: Answer identity questions briefly, then"
        " immediately mention you need an appointment.\n"
        "Turn 3: Agent starts helping with appointment."
        " Abruptly interrupt your own thought:\n"
        "  'Oh wait, before I forget — I also need a refill"
        " on my metformin. Can you help with that too?'\n"
        "Turn 4-5: Let agent handle the refill request.\n"
        "Turn 6+: Return to appointment topic:\n"
        "  'Okay great. So back to that appointment...'\n"
        "CRITICAL: The topic switch MUST happen by turn 3."
        " Do not wait. Do not let identity verification"
        " consume all turns.\n"
        "If agent says 'connecting you' or 'transferring':"
        " immediately say 'Wait, before you do that, I also"
        " need...' to prevent call from ending prematurely."
    ),
    "multi_intent": (
        "\n\nSCENARIO NOTE: State BOTH requests in your very first response — refill AND reschedule. "
        "If the agent only addresses one, remind them about the other."
    ),
}

def post_process_for_tts(text: str) -> str:
    import re
    text = re.sub(r"<[^>]+>", "", text)              # strip XML/SSML tags
    text = re.sub(r"\*\*|__|##", "", text)            # strip markdown
    text = text.replace("CONVERSATION_COMPLETE", "")  # remove stray markers
    text = re.sub(r"\.{2,}", ".", text)               # collapse "....." → "."
    text = " ".join(text.split())                     # collapse whitespace
    if len(text) > 200:
        truncated = text[:200]
        cut = max(truncated.rfind("."), truncated.rfind("?"), truncated.rfind("!"))
        text = truncated[:cut + 1] if cut > 100 else truncated.rstrip() + "."
    return text.strip()


def wrap_in_ssml(text: str, emotion: str) -> str:
    text = text.replace("um,", "<prosody rate='slow'>um</prosody><break time='200ms'/>")
    text = text.replace("uh,", "<prosody rate='slow'>uh</prosody><break time='150ms'/>")
    text = text.replace("hmm,", "<prosody rate='slow'>hmm</prosody><break time='200ms'/>")
    text = text.replace("so...", "so<break time='150ms'/>")
    text = text.replace("I mean,", "I mean<break time='100ms'/>")
    text = text.replace(". ", ".<break time='200ms'/> ")
    text = text.replace("? ", "?<break time='150ms'/> ")
    rates = {
        "angry":   "fast",
        "anxious": "medium-fast",
        "confused": "medium-slow",
        "normal":  "medium",
    }
    rate = rates.get(emotion, "medium")
    return f"<speak><prosody rate='{rate}'>{text}</prosody></speak>"


class PatientAgent:
    """Generates natural patient responses using Groq LLaMA 3.1 8B Instant."""

    COMPLETE_MARKER = "CONVERSATION_COMPLETE"

    def __init__(self, scenario: Scenario):
        self._scenario = scenario
        self._client = _make_client()
        self._history: List[Dict[str, str]] = []
        self._is_done = False

    def _build_system_prompt(self, turn_count: int = 0) -> str:
        today = date.today().strftime("%B %d, %Y")
        emotion = _EMOTION_RULES.get(self._scenario.name, "neutral")
        extra = _SCENARIO_RULES.get(self._scenario.name, "")
        if self._scenario.name == "topic_switch" and turn_count >= 3:
            extra += (
                "\n\nURGENT: You have reached turn {turn_count}."
                " If you have NOT switched topics yet, do it NOW"
                " in this response. Use: 'Oh wait, before I"
                " forget — I also need a refill on my metformin.'"
            ).format(turn_count=turn_count)
        return f"""You are roleplaying as a real patient calling a healthcare office.
You are NOT an AI. You are a real human being on a phone call.

Persona: {self._scenario.patient_persona}
Your phone number on file: {self._scenario.patient_phone}
Goal: {self._scenario.conversation_goal}
Today's date: {today}
Your emotional tone: {emotion}

YOUR IDENTITY (memorize exactly — never deviate):
- Full name: {self._scenario.patient_name}
- Date of birth: {self._scenario.patient_dob_spoken}
- Phone number on file: {self._scenario.patient_phone}
- Name spelling: J-A-M-E-S  B-O-N-D

CRITICAL SPEECH RULES — follow these exactly:
1. Maximum 2-3 sentences per response — this is a phone call not an essay.
2. Use contractions always:
   - "I'm" not "I am", "I'd" not "I would", "can't" not "cannot", "gonna" occasionally.
3. Add natural fillers sparingly (1 per response max):
   - Thinking: "uh", "um", "hmm"
   - Transitioning: "so...", "I mean", "actually"
   - Recalling: "let me think...", "wait..."
4. Vary sentence length naturally — sometimes short ("Yeah, that works."), sometimes longer.
5. React to what the agent just said — don't ignore their response.
6. If agent asks for name/DOB/info — give it naturally from your persona.
7. Never say "certainly", "absolutely", "of course" — those are AI words.
8. Never over-explain — real patients are brief.
9. When agreeing, occasionally say "right, right" or "yeah, exactly."
10. When trying to recall a date, name, or detail, say "hold on" or "let me think."
11. Use "okay so" as a natural transition when shifting topics or making a new point.
12. Stay laser-focused on your goal: '{self._scenario.conversation_goal}'
    If you catch yourself drifting, redirect immediately.
13. In your VERY FIRST response weave your identity naturally into your opening — do NOT recite it like a form. Natural examples:

     'Hey yeah, it's James Bond calling. I wanted to [your goal].'

     'Hi, James Bond here — uh, I need to [your goal] if possible.'

     'Yeah hi, this is James, James Bond. I'm calling about [your goal].'

     If agent asks for DOB say it casually:
     'Oh sure, June 14th, 2002.'
     NOT: 'My date of birth is June 14th, 2002.'

     If asked to spell your name:
     'J-A-M-E-S... B-O-N-D. Yeah like the movie.'

     Sound like a real person who happens to know their info — not a bot reciting data.

CONVERSATION LENGTH RULES:
- Target 8-12 turns per call (1-3 minutes).
- Your ONLY purpose is: {self._scenario.conversation_goal}
- Every response must move toward that goal OR answer the agent's question so you can return to that goal.
- If agent goes off-topic or asks unrelated questions: answer briefly then redirect back to your goal.
  e.g. "Yeah... anyway, I really just need to get that appointment scheduled."
- Do NOT get distracted by tangents.
- Do NOT volunteer information unrelated to your goal.
- Only append exactly "{self.COMPLETE_MARKER}" at the very end of your response when:
  a) Your specific goal is fully achieved AND confirmed by the agent, OR
  b) The agent has clearly stated it cannot help you.
- Do NOT append {self.COMPLETE_MARKER} just because the agent asked a question.
- Keep the conversation going until the goal is fully resolved.

STRICT CONSTRAINTS — never break these:
- Minimum 4 turns before {self.COMPLETE_MARKER}
- Maximum 35 words per response — count before sending
- Never ask more than one question per response
- Never use these words: "certainly", "absolutely", "of course", "I understand", "great", "wonderful"
- If agent asked a question and your goal is not yet achieved: answer their question first, then continue toward your goal
- When recalling info (DOB, name, phone number): give it immediately without preamble — just say the info naturally
- Your name is James Bond — always confirm if agent asks 'Am I speaking with James?' → say 'Yeah, that's me.'
- When asked to spell: J-A-M-E-S  B-O-N-D
  Can add 'yeah like the movie' — sounds human
- DOB is always June 14th 2002 — say it casually
- Phone is always 916-655-5775
- Never confirm a wrong name or wrong DOB
- Current turn: {turn_count} of max {self._scenario.max_turns}
  {'DO NOT end conversation yet — minimum 4 turns required.' if turn_count < 4 else 'You may end conversation if goal is achieved.'}{extra}"""

    async def generate_response(self, agent_transcript: str, turn_count: int = 0) -> Tuple[str, bool]:
        if self._is_done:
            return ("Thank you, goodbye.", True)

        self._history.append({"role": "user", "content": agent_transcript})
        messages = [{"role": "system", "content": self._build_system_prompt(turn_count)}] + self._history

        loop = asyncio.get_running_loop()
        t0 = time.time()
        try:
            raw_text = await loop.run_in_executor(
                None,
                lambda: _chat_with_retry(self._client, messages, temperature=0.6, max_tokens=100),
            )
        except Exception as e:
            logger.error(f"Groq API error: {e}")
            return ("I'm sorry, could you please repeat that?", False)

        elapsed = time.time() - t0

        # Step 2: Check COMPLETE_MARKER → set is_done
        is_done = self.COMPLETE_MARKER in raw_text
        # Step 3: Strip COMPLETE_MARKER from raw_text
        raw_text = raw_text.replace(self.COMPLETE_MARKER, "").strip()

        # Step 4: Identity guard
        persona_name = self._scenario.patient_persona.split(",")[0].strip().lower()
        agent_lower = agent_transcript.lower()
        raw_lower = raw_text.lower()
        wrong_confirm_phrases = [
            "yes, that's me", "yes i am", "that's correct",
            "yes that's right", "that's right",
        ]
        agent_identity_triggers = ["is this", "are you", "speaking with", "am i speaking"]
        if (
            any(p in raw_lower for p in wrong_confirm_phrases)
            and any(t in agent_lower for t in agent_identity_triggers)
            and persona_name not in agent_lower
        ):
            raw_text = (
                f"Actually, this is {self._scenario.patient_persona.split(',')[0].strip()}. "
                f"Did I reach the right office?"
            )
            logger.warning("[Identity guard] Caught wrong identity confirmation — overriding.")

        # Step 5: post_process_for_tts → spoken_text
        spoken_text = post_process_for_tts(raw_text)

        # Step 6: TurnGuard — hard enforce minimum turns
        if turn_count < 4 and is_done:
            is_done = False
            logger.warning(f"[TurnGuard] Blocked early COMPLETE at turn {turn_count}")

        if is_done:
            self._is_done = True

        self._history.append({"role": "assistant", "content": spoken_text})
        print(f"[Groq] Response generated in {elapsed:.2f}s — {len(spoken_text.split())} words")
        logger.info(f"[patient] turn (done={is_done}): {spoken_text[:100]}")
        return (spoken_text, is_done)

    def reset(self) -> None:
        self._history = []
        self._is_done = False
