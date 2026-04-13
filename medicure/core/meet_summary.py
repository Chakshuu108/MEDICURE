"""
meet_summary.py — Automated consultation summarizer for MediCore.

Flow:
  1. Both browsers silently capture mic via Web Speech API (auto-start, no button)
  2. Each final sentence → saved to SQLite meet_transcript_lines with timestamp_ms
  3. merge_and_summarize() merges both sides chronologically → Groq → JSON summary
  4. Summary saved to meet_summaries → visible to both doctor and patient
"""

import json
import requests
import streamlit as st
from core.config import GROQ_API_KEY, GROQ_MODEL


# ── Groq prompt ───────────────────────────────────────────────────────────────

SUMMARY_PROMPT = """You are a medical summarization assistant for MediCore, a healthcare platform.

You will receive a raw transcript from a doctor-patient consultation.
The transcript may be in any language (Hindi, Punjabi, English, or mixed).

Your job:
1. Translate everything to English if needed
2. Extract and structure the information into the following sections

Return ONLY a valid JSON object in this exact format, nothing else:

{
  "symptoms": ["list of symptoms the patient reported"],
  "diagnosis": "doctor's diagnosis or suspected condition",
  "prescriptions": [
    {
      "medicine": "medicine name",
      "dose": "dosage",
      "frequency": "how often",
      "duration": "for how long"
    }
  ],
  "dos": ["things the patient should do"],
  "donts": ["things the patient should avoid"],
  "precautions": ["any special precautions mentioned"],
  "followup": "follow-up instruction or date if mentioned",
  "summary": "2-3 line plain English summary of the entire consultation"
}

If any section has no information, return an empty list [] or empty string "".
Do not add any text outside the JSON. Do not use markdown code blocks.

Transcript:
"""


# ── Merge + Summarize ─────────────────────────────────────────────────────────

def merge_and_summarize(room_name: str, slot_id: str, doctor_id: str, patient_id: str) -> dict:
    """
    1. Fetch all transcript lines for the room (already sorted by timestamp_ms)
    2. Format as chronological dialogue
    3. Send to Groq
    4. Save summary to DB
    5. Return summary dict
    """
    from data.database import get_transcript_lines, save_meet_summary

    lines = get_transcript_lines(room_name)
    if not lines:
        return {"error": "No transcript captured yet."}

    # Format: MM:SS Speaker: text
    formatted_lines = []
    for line in lines:
        ts_ms = line["timestamp_ms"]
        minutes = (ts_ms // 60000) % 60
        seconds = (ts_ms // 1000) % 60
        time_str = f"{minutes:02d}:{seconds:02d}"
        formatted_lines.append(f"{time_str} {line['speaker_label']}: {line['text']}")

    transcript = "\n".join(formatted_lines)

    if len(transcript.strip()) < 20:
        return {"error": "Transcript too short to summarize."}

    if not GROQ_API_KEY:
        return {"error": "GROQ_API_KEY not configured."}

    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "user", "content": SUMMARY_PROMPT + transcript}
                ],
                "temperature": 0.2,
                "max_tokens": 1500,
            },
            timeout=30,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()

        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        summary = json.loads(raw)
        save_meet_summary(
            slot_id=slot_id,
            doctor_id=doctor_id,
            patient_id=patient_id,
            transcript=transcript,
            summary_json=json.dumps(summary)
        )
        return summary

    except json.JSONDecodeError as e:
        return {"error": f"AI returned invalid JSON: {e}"}
    except Exception as e:
        return {"error": str(e)}


# ── Summary card renderer ─────────────────────────────────────────────────────

def render_summary_card(summary: dict, created_at: str = ""):
    """Render a structured consultation summary as a styled card."""

    if not summary:
        return

    if "error" in summary:
        st.error(f"Summary error: {summary['error']}")
        return

    st.markdown(f"""
    <div style="background:linear-gradient(135deg,#0f0f1a,#1a1a2e);
                border:1px solid #7C3AED;border-radius:14px;
                padding:18px 20px;margin-bottom:16px;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">
        <span style="font-size:1rem;font-weight:700;color:#A78BFA;">
          🩺 Consultation Summary
        </span>
        <span style="font-size:0.7rem;color:#475569;">{created_at}</span>
      </div>
    """, unsafe_allow_html=True)

    if summary.get("summary"):
        st.markdown(f"""
        <div style="background:rgba(124,58,237,0.1);border-left:3px solid #7C3AED;
                    padding:10px 14px;border-radius:0 8px 8px 0;margin-bottom:14px;
                    font-size:0.85rem;color:#e2e8f0;line-height:1.6;">
          {summary['summary']}
        </div>
        """, unsafe_allow_html=True)

    col1, col2 = st.columns(2)

    with col1:
        if summary.get("symptoms"):
            st.markdown("**🤒 Symptoms**")
            for s in summary["symptoms"]:
                st.markdown(f"- {s}")

        if summary.get("diagnosis"):
            st.markdown("**🔬 Diagnosis**")
            st.markdown(f"> {summary['diagnosis']}")

        if summary.get("precautions"):
            st.markdown("**⚠️ Precautions**")
            for p in summary["precautions"]:
                st.markdown(f"- {p}")

    with col2:
        if summary.get("prescriptions"):
            st.markdown("**💊 Prescriptions**")
            for rx in summary["prescriptions"]:
                med  = rx.get("medicine", "")
                dose = rx.get("dose", "")
                freq = rx.get("frequency", "")
                dur  = rx.get("duration", "")
                parts = [x for x in [dose, freq, dur] if x]
                st.markdown(f"""
                <div style="background:#1a1a2e;border:1px solid #2d2d44;
                            border-radius:8px;padding:8px 12px;margin-bottom:6px;">
                  <span style="font-weight:600;color:#A78BFA;">💊 {med}</span><br>
                  <span style="font-size:0.75rem;color:#94a3b8;">{' · '.join(parts)}</span>
                </div>
                """, unsafe_allow_html=True)

        if summary.get("dos"):
            st.markdown("**✅ Do's**")
            for d in summary["dos"]:
                st.markdown(f"- {d}")

        if summary.get("donts"):
            st.markdown("**❌ Don'ts**")
            for d in summary["donts"]:
                st.markdown(f"- {d}")

    if summary.get("followup"):
        st.markdown(f"""
        <div style="background:rgba(16,185,129,0.1);border:1px solid #34D399;
                    border-radius:8px;padding:10px 14px;margin-top:10px;
                    font-size:0.82rem;color:#34D399;">
          📅 <strong>Follow-up:</strong> {summary['followup']}
        </div>
        """, unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)
