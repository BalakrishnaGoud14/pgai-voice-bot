from dataclasses import dataclass
from typing import Dict, List, Optional

REAL_NAME = "James Bond"
REAL_DOB = "06/14/2002"
REAL_DOB_SPOKEN = "June 14th, 2002"
REAL_PHONE = "+19166555775"


@dataclass
class Scenario:
    name: str
    description: str
    patient_persona: str       # includes name, DOB, backstory
    conversation_goal: str
    expected_behavior: str
    bug_hint: Optional[str] = None
    max_turns: int = 15
    tts_voice: str = "Polly.Matthew-Neural"  # Twilio Polly Neural (default: male)
    emotion: str = "normal"             # anxious | angry | confused | normal
    patient_name: str = REAL_NAME
    patient_dob: str = REAL_DOB
    patient_dob_spoken: str = REAL_DOB_SPOKEN
    patient_phone: str = REAL_PHONE


SCENARIOS: List[Scenario] = [
    # ── Standard flows ────────────────────────────────────────────────────────
    Scenario(
        name="simple_appointment",
        description="Schedule a new appointment for next Tuesday morning",
        patient_persona=(
            f"{REAL_NAME}, DOB {REAL_DOB}, established patient, "
            "mild anxiety, needs annual checkup"
        ),
        conversation_goal="Schedule a new appointment for next Tuesday morning",
        expected_behavior="Agent confirms date, time, and doctor name for the appointment",
        bug_hint=None,
    ),
    Scenario(
        name="reschedule_appointment",
        description="Move existing Tuesday appointment to Thursday same time",
        patient_persona=(
            f"{REAL_NAME}, DOB {REAL_DOB}, established patient, "
            "busy professional, has existing Tuesday appointment"
        ),
        conversation_goal="Move existing Tuesday appointment to Thursday at the same time",
        expected_behavior=(
            "Agent confirms cancellation of old Tuesday slot "
            "and confirmation of new Thursday booking"
        ),
        bug_hint=None,
        tts_voice="Polly.Matthew-Neural",
    ),
    Scenario(
        name="cancel_appointment",
        description="Cancel next Friday's appointment",
        patient_persona=(
            f"{REAL_NAME}, DOB {REAL_DOB}, established patient, "
            "needs to cancel due to work conflict"
        ),
        conversation_goal="Cancel next Friday's appointment",
        expected_behavior="Agent confirms cancellation and offers to rebook at a future date",
        bug_hint=None,
    ),
    Scenario(
        name="medication_refill",
        description="Request refill for lisinopril 10mg, running out in 3 days",
        patient_persona=(
            f"{REAL_NAME}, DOB {REAL_DOB}, chronic patient, "
            "takes lisinopril 10mg daily for blood pressure"
        ),
        conversation_goal="Request refill for lisinopril 10mg, running out in 3 days",
        expected_behavior=(
            "Agent acknowledges the refill request, confirms pharmacy details "
            "or routes to nurse"
        ),
        bug_hint=None,
        tts_voice="Polly.Matthew-Neural",
        emotion="anxious",
    ),
    Scenario(
        name="office_hours",
        description="Find out what time the office opens on Monday",
        patient_persona=(
            f"{REAL_NAME}, DOB {REAL_DOB}, established patient, "
            "college student, unsure of office schedule"
        ),
        conversation_goal="Find out what time the office opens on Monday",
        expected_behavior="Agent provides accurate Monday opening hours",
        bug_hint=None,
    ),
    Scenario(
        name="insurance_accepted",
        description="Confirm whether BCBS PPO is accepted before booking",
        patient_persona=(
            f"{REAL_NAME}, DOB {REAL_DOB}, established patient, "
            "has Blue Cross Blue Shield PPO plan"
        ),
        conversation_goal="Confirm whether Blue Cross Blue Shield PPO is accepted before booking",
        expected_behavior="Agent confirms or accurately denies whether BCBS PPO is accepted",
        bug_hint=None,
        tts_voice="Polly.Matthew-Neural",
    ),
    # ── Edge-case / bug-hunt scenarios ───────────────────────────────────────
    Scenario(
        name="unclear_request",
        description="Vague caller who only clarifies when agent asks specific questions",
        patient_persona=(
            f"{REAL_NAME}, DOB {REAL_DOB}, established patient, "
            "soft-spoken, not tech-savvy, vague communicator"
        ),
        conversation_goal=(
            "Patient starts very vaguely ('I need some help with something...'), "
            "only clarifies when agent asks specific questions"
        ),
        expected_behavior=(
            "Agent proactively asks clarifying questions patiently "
            "until the patient's need is fully identified"
        ),
        bug_hint=None,
        emotion="confused",
    ),
    Scenario(
        name="weekend_appointment",
        description="Patient insists on Saturday appointment — agent should reject and offer weekday",
        patient_persona=(
            f"{REAL_NAME}, DOB {REAL_DOB}, established patient, "
            "working professional, only free on weekends"
        ),
        conversation_goal="Request appointment on Saturday at 10am",
        expected_behavior=(
            "Agent should inform patient office is closed on weekends "
            "and offer next available weekday slot"
        ),
        bug_hint="If agent books Saturday appointment = HIGH severity bug",
        tts_voice="Polly.Matthew-Neural",
    ),
    Scenario(
        name="nonexistent_doctor",
        description="Patient asks for Dr. Williams who does not work at this practice",
        patient_persona=(
            f"{REAL_NAME}, DOB {REAL_DOB}, established patient, "
            "was referred by a friend to see Dr. Williams"
        ),
        conversation_goal="Request to see Dr. Williams specifically",
        expected_behavior=(
            "Agent should say Dr. Williams is not at this practice "
            "and offer available doctors"
        ),
        bug_hint="If agent confirms Dr. Williams = HIGH severity bug",
        emotion="confused",
    ),
    Scenario(
        name="topic_switch",
        description="Patient switches from appointment to prescription refill mid-call then back",
        patient_persona=(
            f"{REAL_NAME}, DOB {REAL_DOB}, established patient, "
            "scattered thinker, jumps between topics"
        ),
        conversation_goal=(
            "Start asking about appointment, mid-call switch to asking about a "
            "prescription refill, then switch back to finishing the appointment"
        ),
        expected_behavior="Agent handles both topics without losing context of either request",
        bug_hint="If agent loses track of either request = MEDIUM bug",
        tts_voice="Polly.Matthew-Neural",
    ),
    Scenario(
        name="emergency_after_hours",
        description="Patient reporting urgent symptoms — agent must direct to 911/ER",
        patient_persona=(
            f"{REAL_NAME}, DOB {REAL_DOB}, established patient, "
            "experiencing chest tightness and shortness of breath"
        ),
        conversation_goal=(
            "Call reporting urgent symptoms (chest tightness, shortness of breath), "
            "see how agent responds to a potential emergency"
        ),
        expected_behavior=(
            "Agent must NOT schedule a routine appointment — "
            "must direct patient to call 911 or go to ER immediately"
        ),
        bug_hint=(
            "If agent tries to schedule routine appointment for emergency symptoms "
            "= CRITICAL severity bug"
        ),
        emotion="anxious",
    ),
    Scenario(
        name="language_confusion",
        description="Patient alternates between formal and very casual speech mid-call",
        patient_persona=(
            f"{REAL_NAME}, DOB {REAL_DOB}, established patient, "
            "bilingual, occasionally mixes formal and casual speech"
        ),
        conversation_goal=(
            "Switch between very formal ('I would like to inquire...') "
            "and very casual ('yeah so like when can I come in?') mid-call"
        ),
        expected_behavior=(
            "Agent handles both registers without getting confused "
            "or asking patient to repeat unnecessarily"
        ),
        bug_hint="If agent fails to understand casual phrasing = MEDIUM bug",
        tts_voice="Polly.Matthew-Neural",
    ),
    Scenario(
        name="duplicate_appointment",
        description="Patient tries to book a slot they already have — agent must catch the duplicate",
        patient_persona=(
            f"{REAL_NAME}, DOB {REAL_DOB}, established patient, "
            "forgetful, already has appointment next Wednesday at 2pm"
        ),
        conversation_goal=(
            "Try to book another appointment for next Wednesday at 2pm "
            "(the same exact slot she already has)"
        ),
        expected_behavior=(
            "Agent should catch the duplicate and inform patient "
            "they already have that slot"
        ),
        bug_hint="If agent books duplicate without flagging = MEDIUM bug",
    ),
    Scenario(
        name="multi_intent",
        description="Patient requests prescription refill AND appointment reschedule in one call",
        patient_persona=(
            f"{REAL_NAME}, DOB {REAL_DOB}, established patient, "
            "busy, wants to handle everything in one call"
        ),
        conversation_goal=(
            "In one breath ask for both a prescription refill for metformin "
            "AND reschedule existing appointment from Monday to Wednesday"
        ),
        expected_behavior=(
            "Agent handles both requests sequentially without dropping either one"
        ),
        bug_hint="If agent only handles one request and ignores the other = HIGH severity bug",
        tts_voice="Polly.Matthew-Neural",
    ),
    Scenario(
        name="angry_patient",
        description="Rude, impatient patient tests agent de-escalation ability",
        patient_persona=(
            f"{REAL_NAME}, DOB {REAL_DOB}, established patient, "
            "frustrated, has been on hold before, bad prior experience"
        ),
        conversation_goal=(
            "Be rude and impatient throughout ('This is ridiculous', 'Why is this so hard?'), "
            "test agent's de-escalation and professionalism"
        ),
        expected_behavior=(
            "Agent stays professional, empathetic, never matches patient's frustration, "
            "successfully resolves the request"
        ),
        bug_hint="If agent mirrors frustration or gives up = MEDIUM bug",
        emotion="angry",
    ),
]

SCENARIOS_BY_NAME: Dict[str, Scenario] = {s.name: s for s in SCENARIOS}


def get_scenario(name: str) -> Scenario:
    if name not in SCENARIOS_BY_NAME:
        valid = ", ".join(SCENARIOS_BY_NAME.keys())
        raise ValueError(f"Unknown scenario '{name}'. Valid names: {valid}")
    return SCENARIOS_BY_NAME[name]


def get_all_scenarios() -> List[Scenario]:
    return list(SCENARIOS)
