"""What the summary model is asked to write.

One Qwen call writes the debrief and `checks.check_summary` then removes what the transcript cannot back up. This was
tuned on real interviews: a single focused call was both faster and more accurate than multi-step pipelines. The
wording is load-bearing for a 9B model (one added sentence flipped a candidate from Weak to Strong), so re-check real
transcripts after any change to these prompts.
"""
SUMMARY_SYSTEM = (
    "You write debriefs of meetings from speech-recognition transcripts that mix Arabic and English. Some phrases are "
    "misheard: if a phrase does not make sense, ignore it; never repeat it, guess what it meant, or hold it against the "
    "speaker, since the garbling comes from the speech recognition. Write in plain English in your own words, without "
    "quoting the transcript. Use only what was said.")
INTERVIEW_TASK = """Write a debrief of this job interview so the team can remember the candidate and compare candidates
later. Stay neutral and judge by how the candidate answered the follow-up questions, not by what they claimed or what
their CV says. The interviewers' description of their own company is not about the candidate.
## Overview
3-4 sentences: who the candidate is, what they presented, how the interview went.
## Depth check
One bullet per technical question the interviewers probed (how something works, why, or how a problem was solved;
not background or team questions): what was asked and the gist of the answer, ending with
**Clear** (specific detail on how or why), **Vague** (generic, or interviewers had to keep re-asking) or
**Couldn't explain** (no real answer, or credited someone else).
## Strengths
Only what was answered clearly. If nothing was, write "None demonstrated."
## Concerns
## Logistics
Only availability, salary expectation or notice period the candidate stated. Leave this section out if none."""
MEETING_TASK = """Write a debrief of this meeting so someone can remember it weeks later and act on it.
## Overview
3-4 sentences: who met, what about, and what came out of it.
## Key points
4-7 bullets with the concrete details that matter.
## Decisions
## Action items
Each with its owner.
## Open questions
Leave out any section with nothing to put in it."""
CONTEXTS = {
    "General meeting": "",
    "Job interview": "",
    "1:1": "This is a one-on-one: capture feedback given, concerns raised and agreed follow-ups.",
    "Client call": "This is a client call: capture the client's needs, objections, commitments and follow-ups.",
}


def summary_context(kind: str, hint: str = "") -> str:
    """The task text for one summary: the meeting type's instructions plus the user's optional hint."""
    base = INTERVIEW_TASK if kind == "Job interview" else MEETING_TASK
    return "\n".join(filter(None, [base, CONTEXTS.get(kind, ""), f"Extra context from the user: {hint}" if hint else ""]))
