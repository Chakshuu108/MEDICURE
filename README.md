# MediCore AI — Technical Documentation

> A full-stack, multi-agent AI healthcare platform that automates the entire patient lifecycle — from hospital registration to autonomous post-consultation health monitoring — using an orchestrated pipeline of 8 specialized AI agents backed by GROQ's Llama 3.3 70B.

---

## What This Is

MediCore AI is a hospital management system where every major workflow is handled or augmented by a dedicated AI agent. It is not a chatbot with a database bolted on — the AI agents are the core of the application. They perceive patient data, reason over it, and take real actions (booking appointments, firing doctor alerts, generating clinical narratives) autonomously.

The system serves four user roles: **Admin** (hospital onboarding), **Doctor** (clinical dashboard), **Reception** (patient intake), and **Patient** (self-service AI portal). Each role has its own UI module, but they all share the same agent layer and SQLite database.

---

## System Architecture

```
                        STREAMLIT FRONTEND
         ┌──────────┬──────────┬────────────┬────────────┐
         │ admin_ui │doctor_ui │reception_ui│ patient_ui │
         └──────────┴──────────┴─────┬──────┴────────────┘
                                      │
                              ┌───────▼────────┐
                              │  Orchestrator  │  ← single entry point for all agent work
                              └──┬──┬──┬──┬───┘
                                 │  │  │  │
            ┌────────────────────┘  │  │  └──────────────────────┐
            │             ┌─────────┘  └──────────┐              │
            ▼             ▼                        ▼              ▼
       AgenticAI    RiskScoring           HealthGuardian   HealthIntelligence
      (chat/action) (0-100 score)        (auto loop)       (ML + narrative)
            │
     ┌──────┴──────┐
     ▼             ▼
  MCQAgent   AlertingAgent
 (check-ins)  (throttled)

                    ALL AGENTS → GROQ API (Llama 3.3 70B)
                    ALL AGENTS → SQLite (healthcare.db)
```

---

## The Database

Everything lives in a single SQLite database with 11 tables. The schema is designed so that every agent can read from and write to the same patient record without collision.

### Tables and their purpose

**hospitals** — root entity. Every doctor, receptionist, and patient is scoped to a hospital. Stores name, contact info, and a hashed password for admin login.

**doctors** — belongs to a hospital. Has a unique `doctor_code` (short alphanumeric) used alongside email for login. Stores specialization and gender.

**patients** — belongs to both a hospital and a doctor. Stores full vitals: age, gender, blood group, weight, height, disease/diagnosis, contact info, and a `risk_score` field that agents update in real time.

**prescriptions** — each prescription record stores the full medicines list as a JSON blob: `[{name, dosage, timing, duration_days, start_date}, ...]`. This lets the schema stay flexible — a prescription can have any number of medicines without needing a separate medicines table.

**chat_history** — append-only log of every message between the patient and any AI agent. Each row has: patient_id, role (user/assistant), content, timestamp.

**opd_slots** — pre-generated time slots per doctor per date. Each slot has an `is_booked` boolean. Booking an appointment means finding a free slot and flipping that flag while writing a corresponding row to `opd_bookings`.

**opd_bookings** — the actual booking record linking a patient to a specific slot, with a `status` field (booked/cancelled/visited).

**alerts** — created by agents, directed to doctors. Each alert has: type, message, severity (high/medium/low), resolved boolean, and an optional doctor_id target.

**mcq_responses** — one row per patient per day. Stores the generated questions as JSON, the patient's responses as JSON, a computed total_score, status (Improving/Stable/Worsening), adherence_status, and a side_effects JSON array.

**meet_transcript_lines** — real-time consultation transcript. Each row is a single spoken sentence with a speaker label and timestamp in milliseconds.

**meet_summaries** — GROQ-generated structured JSON summaries of consultations, linking back to a session_id shared between doctor and patient.

### Key schema decisions

UUIDs are used for all primary keys (hospital_id, doctor_id, patient_id, etc.) — generated at insert time, never auto-increment integers.

Passwords are stored as SHA-256 hashes. No salting — this is a demonstration system; production would use bcrypt.

JSON blobs are used for medicines lists, MCQ questions, and MCQ responses rather than normalized join tables. This trades query flexibility for schema simplicity — since these are always read/written as complete units, the JSON approach is correct here.

---

## Workflow: How Each Role Works

### Admin creates the hospital ecosystem

The admin registers a hospital (generates a UUID hospital_id). They then add doctors by filling in name, email, specialization, gender, and password — the system generates a short `doctor_code` automatically. This doctor_code is what doctors use alongside their email to log in, providing a second factor without full 2FA. Receptionists are added similarly and scoped to the hospital.

### Reception registers a patient

The receptionist fills in full patient vitals and selects an assigned doctor from the hospital's doctor list. On save, the system: creates the patient record, immediately offers OPD slot booking, and — if email is provided — dispatches a confirmation email with a ReportLab-generated PDF receipt attached. The PDF is built dynamically: it pulls hospital letterhead details, patient name, doctor name, booking date/time, and renders them in A4 format using ReportLab's Platypus layout engine.

### Doctor manages patients

The doctor sees a date picker. Selecting a date queries `opd_bookings` for that date filtered by their doctor_id — showing exactly who is scheduled. For each patient they can: write a prescription (saved as a JSON medicines blob), view the AI risk score (fetched from `patients.risk_score`, refreshed by the RiskScoringAgent), trigger a full Health Intelligence Report, and view/resolve alerts.

### Patient uses the AI portal

Authentication goes through Google OAuth 2.0. The patient's browser is redirected to Google's consent screen. On return, the app exchanges the authorization code for access and refresh tokens. The access token is stored in session state and used to call the Google Calendar API for prescription sync.

Once logged in, the patient lands on a dashboard with: their profile and active prescriptions, an AI chat interface, a daily MCQ check-in, an alerts view, and a consultation summaries view. Every interaction flows through the AgentOrchestrator.

---

## The Agent Layer — Full Detail

### AgentOrchestrator

`orchestrator.py` is the single entry point that all UI modules call. It holds instances of every agent and routes work between them:

- `on_patient_message(patient_id, message, session_state)` → routes to AgenticAI, then always runs AlertingAgent afterward
- `on_prescription_created(patient_id)` → re-evaluates risk score, re-checks alerts, returns a schedule preview
- `on_patient_login(patient_id)` → runs the full Health Guardian cycle

This design means the UI never talks directly to individual agents — it only calls the Orchestrator, which decides what runs and in what order.

---

### AgenticAI — The Multi-Turn Decision Engine

This is the most complex component. It handles all patient chat with full intent classification, slot memory, and autonomous action execution.

#### Intent classification

Every patient message is sent to GROQ with a system prompt that defines 8 intents and their required slots:

| Intent | Slots required |
|--------|---------------|
| book_appointment | doctor, date, time |
| cancel_appointment | appointment_id_or_doctor |
| view_appointments | — |
| view_prescriptions | — |
| check_symptoms | symptoms |
| medication_query | medication_name |
| view_alerts | — |
| general_health | — |

GROQ returns a JSON object: `{intent, slots{}, confidence, missing_slots[]}`. The prompt includes the last 8 turns of conversation history and the full patient context (profile, active medications, recent check-in statuses, upcoming appointments).

#### Slot filling and memory across turns

This is where it gets interesting. A single booking request might span 3 separate messages:

```
Patient: "I want to book an appointment"
Agent:   "Which doctor would you like to see?"
Patient: "Dr. Sharma"
Agent:   "On which date?"
Patient: "This Thursday"
Agent:   → executes booking
```

The slots accumulate in `st.session_state` under a key like `agent_slots_{patient_id}`. On each message, GROQ extracts whatever slots are present in the new message (`fresh_slots`). The `_merge_slots()` function then merges these with whatever was already remembered — fresh non-null values override remembered ones, remembered values fill gaps. This means the patient never has to repeat information.

The agent asks for only the **first** missing slot, not all of them at once. It checks `INTENTS[intent]["slots"]` in order and stops at the first one that's still empty.

#### Action executors

Once all slots are present, the corresponding executor runs:

- `execute_booking()` — queries available OPD slots for that doctor+date combination, picks the first free slot, calls `book_opd_slot()` to flip `is_booked=True` and write the `opd_bookings` row, then triggers a confirmation email
- `execute_cancel()` — fetches active bookings for the patient, fuzzy-matches the doctor name from the slot (strips "Dr." prefix for comparison), and cancels the matched booking
- `execute_symptom_check()` — sends symptoms + full patient profile to GROQ with the SYMPTOM_RULES prompt fragment. The LLM must end its response with exactly one triage verdict token
- `execute_medication_query()` — sends the medication name + full prescription list to GROQ with the SAFETY_RULES prompt fragment, which mandates checking for condition-drug mismatches, dosage limits, and drug-drug interactions

#### Medical safety — hard-coded into the prompt

Both AgenticAI and ConversationAgent include safety prompt fragments that cannot be overridden by user messages. The LLM is told explicitly:

- Never confirm a prescription is correct without clinical reasoning
- Flag mismatches like antibiotics for viral infections, antihypertensives without a BP condition, antidiabetics without a blood sugar condition
- Flag dosage concerns (e.g., Paracetamol > 4g/day, Ibuprofen > 3200mg/day, Metformin > 2550mg/day)
- Flag interaction risks (Warfarin + NSAIDs, SSRIs + Tramadol, ACE inhibitors + potassium-sparing diuretics, statins + fibrates, etc.)
- Every medication response must end with a disclaimer

This is not a filter applied after generation — it is baked into the system prompt that frames every single GROQ call.

---

### RiskScoringAgent

Evaluates a 0–100 risk score for any patient on demand.

**Input to GROQ:** patient name, age, gender, disease, total medication count (summed across all prescriptions), and the last 5 patient chat messages concatenated.

**Output from GROQ:** a JSON object `{risk_score: int, risk_level: "low"|"medium"|"high", reasoning: str}`.

The result is immediately written back to `patients.risk_score` via `update_patient_risk()`.

**Caching:** results are stored in a module-level dict `_RISK_CACHE = {patient_id: (timestamp, result)}` with a 300-second TTL. If a cached result exists and is younger than 5 minutes, it is returned immediately without calling GROQ. This prevents multiple agents from triggering redundant LLM calls for the same patient within a short window.

---

### AlertingAgent

Runs after every patient message but is throttled with a 120-second cooldown per patient (tracked in `_last_alert_check` dict). When it does run:

1. Calls `RiskScoringAgent.evaluate()` — if risk level is "high", creates a `high_risk` alert
2. Calls `HealthEvaluationAgent.detect_behavioral_trends()` — if trend is "worsening", creates a `worsening_condition` alert

Alerts are written to the `alerts` table with the patient_id and the doctor_id from the patient's assigned doctor. Both the doctor dashboard and the patient portal render these alerts.

---

### MCQAgent — Daily Health Check-In

Generates 5 personalized daily questions for the patient's self-assessment.

**Generation:** a GROQ prompt includes the patient's disease, age, gender, and current medication list. The LLM must return a JSON array of exactly 5 questions, each with 3 answer options and a score for each option (+1, 0, or -1). The question distribution is fixed: 2 symptom questions, 1 adherence question, 1 side effects question, 1 general wellbeing question.

**Caching:** questions are stored in `mcq_responses` keyed by `(patient_id, date)`. If a row exists for today, the stored questions are returned directly — GROQ is not called again. A `force_regenerate` flag can bypass this.

**Scoring:** when the patient submits their answers, each selected option's score is summed. The total maps to a status: positive sum → Improving, zero → Stable, negative → Worsening. This status and the individual responses are stored in the same `mcq_responses` row. The adherence question answer is stored separately in `adherence_status` so other agents can query it directly.

---

### Health Guardian — Autonomous Perceive → Reason → Act

This agent runs once per patient login session with zero user input. It is designed to detect patterns that are only visible across multiple sessions — things a snapshot-based risk scorer would miss.

#### Perceive

Pulls the last 30 MCQ responses (roughly one month of daily check-ins). From these it builds:
- A score timeline (list of scores sorted oldest to newest)
- A day-of-week score map: `{0: [scores on Mondays], 1: [scores on Tuesdays], ...}` — averaged to detect weekly rhythms
- A list of dates where adherence_status contained keywords like "miss", "skip", "forgot"
- The patient's active prescription schedule (med name + frequency)
- The patient's next upcoming OPD booking

#### Reason

This snapshot is formatted into a structured prompt and sent to GROQ. The LLM is asked to analyze for:
- Day-of-week patterns (scores consistently worse on specific days)
- Medication-correlated patterns (scores dropping 2 days after known dose days)
- Sustained silence — many missed check-in days
- Multi-week worsening trends
- Anomalies only visible across sessions

The LLM must return a structured JSON with a `findings` array. Each finding has: type, title, description, a 3-step `reasoning_chain`, severity (low/medium/high), action (alert_doctor/flag_in_brief/monitor/none), and an `alert_message` to send if action is alert_doctor.

The already-flagged alerts are passed into the prompt as a list so the LLM does not duplicate alerts it already fired in previous sessions.

#### Act

The `_act()` function iterates the findings array. For each finding where `action == "alert_doctor"`, it calls `create_alert()` with the exact `alert_message` from the LLM output, prefixed with "🧬 Health Guardian — [finding title]".

There is one escalation rule built into Act: if the patient has an OPD appointment within 48 hours, any finding that would trigger an alert or flag is automatically escalated to severity="high" and its description is appended with "⚡ Escalated — appointment within 48 hours." This ensures the doctor sees critical information before the consultation.

The full output — snapshot, reasoning (including the LLM's reasoning chains), and action log — is stored in session state and rendered in the patient UI as a transparent "Guardian Report" panel.

---

### HealthIntelligenceAgent — ML Risk Pipeline

A four-stage pipeline that produces clinical risk predictions and narrative reports. Called from the Doctor dashboard via "Generate Health Intelligence Report".

#### Stage 1 — DataAggregator

Pulls from the database: last 30 MCQ responses, all prescriptions and medicines, last 20 chat messages, and all alerts. Parses MCQ response JSONs. Builds an `effect_freq` dict counting how often each side effect has been reported across check-ins. Counts how many chat messages contain "concern signal" keywords (pain, worse, struggling, etc.) vs "improvement signal" keywords (better, improved, great, etc.).

#### Stage 2 — FeatureEngineer

Converts the raw aggregated data into a fixed-length numeric feature vector for the ML model. The features are:

1. **adherence_rate** — fraction of last 14 MCQ rows where adherence_status was positive
2. **slope_7** — linear regression slope over the last 7 score values (positive = improving)
3. **slope_14** — same over 14 values
4. **score_mean_14** — mean score over last 14 check-ins, normalized to [0,1]
5. **score_std_14** — standard deviation of scores (high std = instability)
6. **consec_worse** — number of consecutive "Worsening" statuses at the end of the timeline
7. **days_silent** — days since last check-in
8. **concern_ratio** — concern_hits / (concern_hits + improvement_hits + 1)
9. **age_norm** — patient age / 100
10. **med_count_norm** — number of active medicines / 10

All features are normalized or clamped to reasonable ranges before being assembled into a NumPy array.

#### Stage 3 — RiskPredictor

A `GradientBoostingClassifier` from scikit-learn, trained on **in-memory synthetic priors** at instantiation time. These synthetic samples are hand-crafted feature vectors representing prototypical patients: healthy/adherent (label: low), deteriorating/non-adherent (label: high), and borderline cases (label: medium). This gives the model a reasonable baseline without requiring any external training data.

When `predict()` is called with a patient's feature vector, the model returns class probabilities for low/medium/high. These are converted to a 0–100 score: `score = prob_low * 20 + prob_med * 60 + prob_high * 90`, then clamped. Level thresholds: ≥65 = High, ≥35 = Medium, else Low.

`predict_trajectory()` projects the risk score forward for N days using a simplified exponential smoothing: the base score is adjusted by a `daily_delta` computed from the normalized slope and consecutive-worsening features. This gives the doctor a 7-day risk trajectory.

#### Stage 4 — NarrativeEngine

Passes the structured prediction output, feature values, and aggregated signals to GROQ in a clinical narrative prompt. The prompt explicitly tells the model to synthesize (not repeat numbers verbatim) and to end with a concrete physician action recommendation. Max 300 tokens, temperature 0.25 for consistency.

If GROQ fails, a deterministic fallback narrative is assembled from the feature values directly (no LLM call).

---

### ConversationAgent (Legacy Path)

The alternative to AgenticAI. Instead of intent classification + slot filling, it takes a simpler approach: every patient message is sent to GROQ with the full patient context injected as a system prompt — profile, disease, all active medications with dosages and timings, and the last 20 messages of chat history.

This makes it context-aware without needing multi-turn slot management. The tradeoff is that it cannot autonomously execute actions (no booking, no cancellation) — it can only answer questions and give guidance. It applies the same Medical Safety Framework as AgenticAI.

---

### SchedulingAgent

Converts prescriptions into Google Calendar events. For each medicine in the patient's prescriptions, it maps the `timing` field to one or more time slots (morning → 08:00, twice daily → 08:00 + 20:00, thrice daily → 08:00 + 14:00 + 20:00, etc.), then generates one calendar event per dose per day for the full duration. Each event includes a 10-minute popup reminder.

The agent calls the Google Calendar API using the patient's OAuth access token — it writes directly to the patient's primary calendar without requiring any backend service account.

---

## Session Management

Streamlit loses all `st.session_state` on browser page refresh. MediCore solves this by encoding critical session keys as a compact JSON blob and storing it in the `_s` URL query parameter:

```
?_s={"mode":"patient","patient_logged_in":true,"patient_id":"abc-123",...}
```

On every rerun, `_save_session_to_params()` encodes the current state. On the first load after a refresh, `_restore_session_from_params()` reads the `_s` param and re-populates session state before any UI renders.

The Google OAuth callback is intercepted before session restoration runs, because it arrives with a `?code=` parameter that must be exchanged for tokens before the `_s` state is touched. If this order were reversed, the OAuth state and session state would conflict.

---

## Consultation Summarizer

When a doctor and patient are in a live consultation session, both browsers use the Web Speech API to capture microphone input. Each recognized sentence is saved to `meet_transcript_lines` with a speaker label and millisecond timestamp.

When the session ends, `merge_and_summarize()` pulls all transcript lines for the session, sorts them by timestamp, and sends the merged transcript to GROQ. The prompt instructs the model to: translate any Hindi/Punjabi/mixed-language content to English, then extract a structured JSON with sections for symptoms, diagnosis, prescriptions, dos, don'ts, precautions, follow-up date, and a 2–3 sentence summary.

This JSON is stored in `meet_summaries` and displayed to both the doctor and patient as a formatted post-consultation brief.

---

## Email and PDF Generation

When a patient is registered or an OPD booking is confirmed, `email_service.py` builds a PDF in memory using ReportLab's Platypus engine and emails it as an attachment.

The PDF structure: hospital letterhead (name, address, phone pulled from the hospitals table), a "Booking Confirmed" header, a data table with patient name/ID/doctor/date/time/slot, a footer with the hospital's branding. All layout is done with Platypus `Table`, `Paragraph`, `HRFlowable`, and `Spacer` objects — no templates, fully programmatic.

The email is sent via SMTP with TLS using Python's `smtplib`. The PDF bytes are attached as a `MIMEBase` part with `application/pdf` content type.

---

## Styling

All visual customization is in `ui/styles.py` as a single `PURPLE_THEME` CSS string injected via `st.markdown(..., unsafe_allow_html=True)` on every rerun (required because Streamlit re-renders from scratch on each interaction). The theme overrides Streamlit's default colors with a purple/violet palette, defines `.card` and `.card-header` utility classes used throughout the UI modules, and sets custom scrollbar and sidebar styles.

---

## What Makes This Interesting

**The agents share state through the database, not through each other.** The Health Guardian reads MCQ data that was written by the MCQAgent. The AlertingAgent reads risk scores written by the RiskScoringAgent. No agent calls another directly except through the Orchestrator. This means each agent is independently testable and replaceable.

**The ML model is stateless.** The GradientBoostingClassifier in HealthIntelligenceAgent is re-trained from scratch on synthetic priors every time the agent is instantiated, then immediately applied to the live patient features. There is no model persistence to disk. The synthetic priors are deterministic, so results are reproducible. This is a deliberate tradeoff: no training pipeline to maintain, no model versioning — just feature engineering on the live data.

**The agentic chat has memory without a vector database.** Slot memory is stored in `st.session_state` (a Python dict), not in a vector store or external cache. The GROQ prompt always includes the last 8 conversation turns for context. The combination of explicit slot tracking + conversation history gives the agent both structured memory (slots) and unstructured context (conversation) without any external infrastructure.

**Safety prompts are not optional.** The medical safety framework is injected as a required section of the system prompt, not as a post-processing filter. The LLM is structurally unable to skip it — it is part of the instructions it is given before it processes any user message.
