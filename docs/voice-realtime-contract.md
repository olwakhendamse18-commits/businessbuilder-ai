# BusinessBuilder AI Phase 2 Realtime Voice Contract

Status: **Frozen for Phase 2A review**

Applies to repository SHA: `eac5ebcb010e071ba50d2f073d9b635aa789a994`

Feature status: **Disabled by default; no production activation authorized**

Last source review: 2026-07-24

## 1. Purpose and normative language

This document freezes the architecture, authority boundaries, event contract, lifecycle, security, privacy, failure behavior, and staged implementation plan for BusinessBuilder AI Realtime voice.

The words **must**, **must not**, **required**, and **prohibited** are normative. Any implementation that differs from this contract requires explicit review and an update to this document before activation.

Phase 2A is documentation only. It does not authorize microphone access, a Realtime connection, an OpenAI API request, an external action, browser control, deployment, or production enablement.

## 2. Frozen architecture and authority

### 2.1 Data flow

```text
Microphone
  → OpenAI Realtime transcription
  → BusinessBuilder /api/agent/message
  → run_businessbuilder_agent
  → saved canonical user and assistant messages
  → safe spoken rendering
```

This is a controlled hybrid/chained voice architecture. It extends the existing text agent rather than making the Realtime model the BusinessBuilder agent. OpenAI recommends a chained voice pipeline when an application needs explicit control over transcription, reasoning, and speech output, particularly for predictable or approval-heavy workflows.

### 2.2 OpenAI Realtime authority

OpenAI Realtime is responsible only for:

- microphone transport;
- voice activity events;
- input transcription;
- assistant audio rendering; and
- assistant output captions.

### 2.3 BusinessBuilder backend authority

The BusinessBuilder backend remains authoritative for:

- business reasoning;
- project memory;
- conversation history;
- tasks;
- approvals;
- risk classification;
- connected services;
- research;
- tool routing;
- external actions; and
- saved user and assistant response text.

The browser Realtime session must expose no BusinessBuilder tools. No external action may occur directly from a Realtime event. A Realtime transcript is input to the existing authenticated backend; it is not an instruction to bypass backend validation, approval, idempotency, or authorization.

## 3. Current Realtime session contract

### 3.1 Transport and endpoint

The browser uses WebRTC. The authenticated BusinessBuilder server uses OpenAI's unified interface and must send the SDP offer plus the server-constructed session policy to exactly:

```text
https://api.openai.com/v1/realtime/calls
```

The upstream URL must be pinned to that HTTPS origin and path, or checked against an equally strict allowlist. An arbitrary configured host must never receive `OPENAI_API_KEY`.

OpenAI call creation must return exactly `201 Created`, a valid SDP answer, and a valid `Location` header before the browser may receive SDP. The documented `Location` form is `/v1/realtime/calls/{call_id}`. BusinessBuilder extracts and stores only the opaque call ID; it never stores or follows the unrestricted header value and never returns the call ID to the browser. Canonical absolute `Location` compatibility is permitted only for exact HTTPS `api.openai.com` values with the same path, no alternate port or user information, and no query or fragment.

Authoritative upstream termination uses only:

```text
POST https://api.openai.com/v1/realtime/calls/{validated_call_id}/hangup
```

The URL is constructed from the pinned calls endpoint and a validated stored ID. Redirects are disabled, authentication uses the server API key, and the browser may supply neither a call ID nor an OpenAI URL.

### 3.2 Target session policy

The approved current model is `gpt-realtime-2.1`. The selected default voice is `marin`; any configured voice must be validated against the current documented Realtime voice allowlist before it is used.

The target structure is:

```json
{
  "type": "realtime",
  "model": "gpt-realtime-2.1",
  "output_modalities": ["audio"],
  "audio": {
    "input": {
      "transcription": {
        "model": "gpt-4o-mini-transcribe"
      },
      "turn_detection": {
        "type": "semantic_vad",
        "eagerness": "low",
        "create_response": false,
        "interrupt_response": true
      }
    },
    "output": {
      "voice": "marin"
    }
  },
  "instructions": "Use voice only as BusinessBuilder AI transport. Do not perform business reasoning, use tools, approve actions, or change canonical BusinessBuilder text. Render only an approved canonical response or an approved controlled notification supplied by BusinessBuilder.",
  "tools": [],
  "tool_choice": "none",
  "max_output_tokens": 900
}
```

`max_output_tokens: 900` is the frozen application limit for short spoken rendering. It is well below the model's documented maximum and is not permission for Realtime to generate canonical business reasoning. Phase 2B must verify the field against the then-current OpenAI schema before implementation; a schema mismatch is a stop gate.

`create_response` is false because completed user speech must go to `/api/agent/message`, not directly to a Realtime model response. `interrupt_response` may remain true to support barge-in during known assistant output, but BusinessBuilder still owns response state and cleanup.

### 3.3 Legacy fields prohibited as target configuration

| Legacy field | Required migration |
|---|---|
| `modalities` | Replace with `output_modalities`. |
| root `voice` | Move to `audio.output.voice`. |
| root `input_audio_transcription` | Move to `audio.input.transcription`. |
| root `turn_detection` | Move to `audio.input.turn_detection`. |
| `response.modalities` | Replace with `response.output_modalities`. |

No implementation may copy the current legacy fields into the Phase 2 target contract.

## 4. Server authority and client events

- Initial session policy is constructed server-side.
- The browser must not replace server instructions.
- Client `session.update` is omitted unless a narrowly approved dynamic field is later documented.
- A permitted client event must include an `event_id` where the event supports it.
- Realtime errors must be correlated to the originating `event_id`.
- If a `session.update` is ever approved, `session.updated` must be observed and validated before the interface enters the fully ready/listening state.
- A browser-provided model, voice, instructions, tools, tool choice, token limit, or turn-detection policy is untrusted and must not become server policy.
- Realtime tools remain empty and disabled.

## 5. Exact server-event contract

Routing must use exact string equality. Substring, prefix, suffix, or regular-expression matching across event families is prohibited.

Legend: **Yes** means the event is permitted to affect that concern. **No** means it must not.

| Exact server event | Interface state | User captions | Assistant captions | May create backend turn | Cleanup | Interruption |
|---|---|---:|---:|---:|---:|---:|
| `session.created` | `connected`; await validated policy readiness | No | No | No | No | No |
| `session.updated` | `listening` only after expected fields are validated | No | No | No | No | No |
| `error` | `error`, or recoverable prior state for a classified non-terminal error | No | No | No | Yes, when terminal | Yes, when tied to active output |
| `input_audio_buffer.speech_started` | `user_speaking` | No | No | No | No | Yes |
| `input_audio_buffer.speech_stopped` | `processing_transcript` | No | No | No | No | No |
| `input_audio_buffer.committed` | remain `processing_transcript` | No | No | No | No | No |
| `conversation.item.input_audio_transcription.delta` | `processing_transcript` | Yes, provisional only | No | No | No | No |
| `conversation.item.input_audio_transcription.completed` | enqueue `thinking` turn | Yes, canonical normalized input | No | **Yes; only this event** | No | No |
| `conversation.item.input_audio_transcription.failed` | `listening` or retryable `error` | Yes, failure status only | No | No | No | No |
| `response.created` | `speaking` only when response ownership matches current turn | No | No | No | No | Yes |
| `response.output_audio_transcript.delta` | retain owned output state | No | Yes, provisional only | No | No | Yes |
| `response.output_audio_transcript.done` | retain owned output state | No | Yes, but never overwrite canonical text | No | No | Yes |
| `response.output_audio.done` | await `response.done`/buffer stop | No | No | No | No | Yes |
| `output_audio_buffer.started` | `speaking` for the owned response | No | No | No | No | Yes |
| `output_audio_buffer.stopped` | `completed`, `waiting_for_approval`, or `listening` according to the owned turn | No | No | No | No | Yes |
| `output_audio_buffer.cleared` | `user_speaking`, `listening`, or `stopping` according to cause | No | No | No | No | Yes |
| `response.done` | finalize only the matching owned response | No | No; canonical text remains authoritative | No | No | Yes |
| `rate_limits.updated` | no user-facing state change unless a limit requires policy action | No | No | No | No | No |

The single backend-turn invariant is:

```text
event.type === "conversation.item.input_audio_transcription.completed"
```

Only exact equality with that value may create a new `/api/agent/message` request.

The following must never create user turns:

- `response.output_audio_transcript.delta`;
- `response.output_audio_transcript.done`;
- `response.output_audio.done`; and
- `response.done`.

## 6. Backend turn contract

Each completed input transcript is represented by a turn record in client lifecycle state containing:

- Realtime `item_id`;
- BusinessBuilder `conversation_id`;
- stable client `request_id`;
- BusinessBuilder voice-session ID;
- normalized transcript;
- received timestamp; and
- processing status.

The turn statuses are `queued`, `processing`, `completed`, `failed`, `superseded`, or `cancelled_before_dispatch`.

Rules:

1. Deduplicate by both `item_id` and `request_id`.
2. Never deduplicate only by transcript text.
3. Process at most one BusinessBuilder backend voice turn at a time.
4. Queue later completed turns in their received order.
5. Preserve the existing `/api/agent/message` idempotency contract.
6. Validate conversation ownership server-side on every request.
7. Do not let out-of-order backend responses speak over one another.
8. Do not begin speech for a response whose turn is no longer current.
9. A response may become speakable only after the backend request succeeds, returns valid JSON, contains the required canonical response fields, and still owns the current turn.
10. Realtime event ordering across different `item_id` values must not be assumed.

### 6.1 Transcript length gate

The maximum permitted voice transcript length is the approved canonical `/api/agent/message` input limit, enforced identically in the browser for usability and on the server for authority.

The canonical `/api/agent/message` maximum is **12,000 Unicode characters** for both text and voice-originated messages. Phase 2B.1 enforces this limit on the server before request-ID reservation, conversation creation, message persistence, or agent invocation. This provides a bounded input large enough for detailed business context while preventing unbounded work and keeping one authority boundary for both modes. Browser enforcement is a later usability layer; the server limit is authoritative.

When the limit is exceeded:

- do not call `run_businessbuilder_agent`;
- do not save a partial user message;
- mark the voice turn failed;
- retain the local transcript only as non-persisted UI text if safe;
- tell the user the message is too long and ask them to shorten it or use text mode; and
- return the interface to `listening` when the session is otherwise healthy.

## 7. Canonical response and approval contract

The successful `/api/agent/message` JSON and the saved assistant message are the canonical response. The visible transcript must display the canonical response exactly.

For ordinary non-consequential guidance, Realtime may render the canonical answer as speech. Realtime output captions are accessibility/playback aids and must not replace, edit, or become the saved canonical response.

For approval-needed, financial, publishing, account-connection, messaging, legal, tax, payment, or other consequential responses:

- do not ask Realtime to freely summarize consequential details;
- display the exact canonical details in the UI;
- keep approval cards authoritative;
- speak only an approved entry from the controlled approval phrase bank defined below;
- do not automatically approve or continue; and
- do not call any external tool.

Canonical approval-notification baseline:

> I prepared an action that requires your approval. Please review the approval panel before I continue.

Cancelled or failed speech must never remove or alter an already visible canonical response.

## Dynamic speech, phrase banks, and voice identity

### Ordinary non-consequential speech

Ordinary, non-consequential speech may be dynamic, but it must be grounded in the canonical successful response from `/api/agent/message`. This category includes planning, brainstorming, explanations, comparisons, next steps, project summaries, general business questions, non-consequential calculations, and internal drafts that are not being sent, published, purchased, approved, or otherwise actioned.

No fixed script is required for ordinary speech. Realtime may render the canonical answer conversationally, but the exact visible canonical response remains authoritative. Spoken rendering must not invent facts, change amounts or conclusions, imply an action occurred, or obscure a qualification in the canonical answer.

### Lifecycle phrase banks

Phase 2E may define small, reviewed phrase banks for:

- greeting;
- listening;
- acknowledgement;
- thinking;
- completed;
- mute and unmute;
- stop speaking;
- microphone permission denied;
- unsupported browser;
- connection lost and reconnecting;
- inactivity and maximum-duration timeout; and
- exit.

Each variation must preserve the lifecycle state's meaning, avoid false claims and invented data, and avoid unnecessary repetition. Speech must not talk over the user. Lifecycle audio and captions must respect the user's audio, caption, and reduced-motion settings. A phrase must never announce completion, connection, persistence, approval, or execution unless the corresponding authoritative state has occurred.

### Consequential and approval-controlled wording

Consequential speech is controlled. It includes approvals; payments or spending; publishing; messages or emails; connected or external accounts; deletion; legal, tax, or financial content; and any consequential amount, recipient, domain, account identifier, external confirmation, security detail, or privacy detail.

Realtime must not freely paraphrase or summarize those details. The UI's canonical text and approval panel remain authoritative. The approval phrase bank must remain small, reviewed, and semantically equivalent to:

> I prepared an action that requires your approval. Please review the approval panel before I continue.

Every approved variation must communicate all of the following without ambiguity:

- an action has been prepared;
- approval is required;
- execution is paused;
- the user must review the approval panel; and
- no action has occurred.

An approval phrase must not include or paraphrase consequential amounts, recipients, domains, account identifiers, legal or tax claims, security details, privacy details, or external confirmations. No free-form approval wording is permitted.

### Dynamic greeting context

A greeting may vary and may use only server-authorized, authenticated context: the user's preferred name, the current period of day, the active project, established progress, and an established next step.

A greeting must not:

- invent context, progress, or a next step;
- falsely claim a connection, saved change, completed action, or external result;
- expose personalized context before authentication and authorization;
- begin before the user explicitly selects Start Voice and the voice session is ready; or
- imitate Jarvis, an actor, a celebrity, or another protected or recognizable identity.

### Voice selection and personality

The application must not force one voice for every user. The server reads the saved `selected_voice`, validates it against a server-side allowlist derived from the current documented Realtime voices, and falls back to a safe approved default. A browser-supplied arbitrary voice is untrusted and must not become session policy.

The voice must not change after speech has begun in a Realtime session. Applying a different valid saved voice requires ending and restarting the voice session. No voice may be selected or prompted to impersonate a real person, actor, celebrity, fictional assistant, or other protected identity.

All allowed voices use the same BusinessBuilder personality: calm, professional, respectful, confident without false certainty, beginner-friendly, original, and slightly futuristic. Lifecycle speech is concise; ordinary non-consequential speech is conversational.

### Implementation-stage boundary

- Phase 2B secures the handshake, validates the saved voice and server allowlist, and does not implement phrase-bank playback.
- Phase 2C implements connection and lifecycle controls without weakening canonical authority or the Start Voice consent boundary.
- Phase 2D implements exact events, serialized canonical turns, ownership, and deduplication; it does not introduce dynamic output policy.
- Phase 2E implements dynamic ordinary speech, reviewed lifecycle and approval phrase banks, captions, interruption, and spoken exit.
- Phase 2F tests repetition, canonical authority, controlled wording, failures, privacy, accessibility, and cleanup.
- Phase 4 performs final personality, pacing, wording, and professional-polish tuning only within this frozen authority boundary.

## 8. Spoken exit contract

The exact normalized allowlist is:

```text
exit voice panel
exit the voice panel
close voice panel
close the voice panel
return to text mode
switch to text mode
```

Normalization must:

1. lowercase;
2. trim leading and trailing whitespace;
3. collapse repeated internal whitespace;
4. remove surrounding punctuation; and
5. require exact full-phrase equality.

Substring matching is prohibited. Browser `SpeechRecognition` is prohibited.

On a match:

1. do not send the phrase to `/api/agent/message`;
2. cancel active assistant output when applicable;
3. clear queued output audio when applicable;
4. stop microphone tracks;
5. close the data channel;
6. close the peer connection;
7. stop local and remote analysers;
8. end the BusinessBuilder voice-session record;
9. set the interface to `exiting`; and
10. return to `/command-center`.

Button exit, Escape exit, spoken exit, navigation exit, timeout exit, and connection-failure exit must call the same idempotent cleanup operation.

## 9. Interruption contract

The client owns an `activeResponse` record containing at least the current BusinessBuilder turn ID, Realtime response ID, generation token, state, and whether audio playback has started.

Barge-in occurs when `input_audio_buffer.speech_started` arrives while owned assistant output is active:

- cancel only the known active response;
- clear queued WebRTC output audio where supported;
- truncate conversation audio only when necessary to keep Realtime context consistent;
- move to `user_speaking`;
- do not cancel a BusinessBuilder backend request that has already completed unless a later design explicitly authorizes that behavior;
- never remove the canonical visible response;
- mark the prior response interrupted so late events cannot move the interface to `completed` or `speaking`; and
- ignore late output events whose response ID or generation token no longer owns the active session.

Sending a blind `response.cancel` when no response is owned is prohibited.

## 10. Lifecycle and cleanup contract

There is one idempotent cleanup operation. Repeated and concurrent calls must be safe and converge on one terminal result. The first terminal reason is retained unless a later server-authoritative reason is more severe.

Cleanup includes:

- duration, idle, disconnect-grace, and handshake timers;
- pending turn queue;
- active response ownership;
- data-channel handlers;
- data channel;
- peer-connection handlers;
- peer connection;
- microphone tracks;
- remote tracks;
- remote audio element;
- `AudioContext` objects;
- analysers;
- animation frames; and
- the BusinessBuilder database voice-session record.

Local cleanup, browser peer-connection closure, and upstream termination are distinct facts. A local record may be `ended` while upstream termination is pending or permanently failed. Closing WebRTC in the browser is not proof that OpenAI accepted server-requested termination.

Required cleanup triggers:

- Stop Voice;
- Exit Voice Panel;
- Escape;
- spoken exit;
- `pagehide`;
- logout or authentication loss;
- peer `failed`;
- prolonged peer `disconnected`;
- data-channel close;
- data-channel error;
- a terminal Realtime error;
- handshake failure;
- maximum duration;
- inactivity timeout; and
- server-enforced expiry.

`pagehide` cleanup is best effort. Server reconciliation remains authoritative and may not depend on the browser sending the end request.

## 11. Session admission and limits

Backend requirements:

- Voice preference or entitlement is enforced server-side.
- Runtime availability requires the dedicated voice feature flag, entitlement/preference, valid server configuration, and capacity. API-key presence alone is insufficient.
- Admission is atomic.
- At most the configured number of active sessions may exist per user.
- Parallel handshake requests cannot bypass that limit.
- Maximum duration is enforced server-side.
- Stale sessions are reconciled without requiring the user to start another session.
- The end route remains scoped to the authenticated user and is idempotent.
- Retries cannot create abandoned duplicate records.
- Upstream failure finalizes the local session record.

### 11.1 SQLite/PostgreSQL-compatible strategy for Phase 2B

No schema change is authorized in Phase 2A. Phase 2B should:

- run admission and local-session creation in one database transaction;
- use `BEGIN IMMEDIATE` on SQLite to serialize the read-count-and-insert decision;
- use a transaction-scoped PostgreSQL advisory lock keyed by the user and voice-admission namespace before the same decision;
- re-check active non-expired sessions while holding the lock;
- reuse a stable handshake request ID so a retry can find and finalize/reuse its own record rather than insert another;
- finalize the record in the same error path when the upstream handshake fails; and
- run periodic server reconciliation that marks expired active records ended independently of user traffic.

If tests show this cannot provide the required invariant under both databases, implementation must stop and return to contract review before any schema migration is proposed.

### Phase 2B.2A local admission and lifecycle

Each handshake request must supply `X-BusinessBuilder-Voice-Request-ID`. The value is client-generated, attempt-specific, no longer than 120 characters, and limited to ASCII letters, digits, underscore, hyphen, period, and colon. It is an idempotency key for one authenticated user's SDP attempt; it is not authentication and does not replace the voice CSRF token. Missing or invalid values are rejected before voice-session insertion.

Local voice-session lifecycle states are `starting`, `active`, and `ended`:

1. atomic admission inserts `starting`;
2. the upstream SDP handshake occurs after the database transaction has committed and no admission lock is held;
3. a valid upstream answer changes only that owned, unexpired `starting` row to `active`;
4. handshake failure or activation failure finalizes the row as `ended`; and
5. client stop and local reconciliation may change `starting` or `active` to `ended`, but an `ended` row is never revived.

The database stores `handshake_request_id`, the server-calculated `expires_at`, and `updated_at`. A partial unique index on `(user_id, handshake_request_id)` applies when the request ID is present. A second partial unique index permits at most one row per user whose status is `starting` or `active`. Before those indexes are created, repeated startup migration deterministically retains the newest non-expired non-terminal row and ends older duplicates with `duplicate_session_reconciled`; ordinary lifecycle records are not deleted.

SQLite admission uses `BEGIN IMMEDIATE`. PostgreSQL admission uses a transaction-scoped `pg_advisory_xact_lock` keyed by a stable voice-admission namespace and authenticated user ID. Under that lock, the server reconciles expiry, checks saved preference and runtime configuration, enforces non-terminal and daily limits, applies request-ID idempotency, and inserts the `starting` row. The same request ID in `starting` returns `voice_handshake_in_progress`; the same ID in `active` returns `voice_session_active`; and an `ended` request ID returns `voice_request_id_reused`, requiring a fresh ID and SDP offer. A different ID is rejected while another non-terminal row exists.

Abandoned `starting` rows use an exact **60-second** server-controlled handshake grace period when no valid fixed expiry is available; the browser cannot select or extend this grace. `active` rows expire at their fixed server-calculated `expires_at`; legacy active rows without that field use their server-configured maximum duration. A valid `expires_at` is authoritative for either non-terminal state. A non-terminal row with neither a valid expiry nor a valid `started_at`/`created_at` fails closed as `invalid_session_timestamp` rather than blocking admission indefinitely. Reconciliation is idempotent, scans only `starting` and `active` rows during normal runtime, runs opportunistically behind a process-local monotonic throttle, and is also available through `flask reconcile-voice-sessions`. The CLI prints counts only on success, exits non-zero with a safe classification on failure, and never contacts OpenAI. Production scheduling of that command remains unauthorized until deployment planning is separately approved.

An admitted `starting` row counts as one session-start attempt for the daily session-start limit. A failed upstream handshake therefore consumes one start attempt, which limits repeated failed connection attempts. It does not consume the full configured session duration: duration accounting uses only actual elapsed local lifecycle time and is clamped so it can never be negative. Changing this failed-handshake accounting policy requires contract review.

Phase 2B.2A makes BusinessBuilder's local admission and accounting state expire authoritatively. It does **not** prove that an already-established upstream Realtime call can be forcibly terminated by the BusinessBuilder server without client cooperation or an approved server-side sideband control mechanism. Phase 2B.2B must resolve and test that upstream-termination limitation before voice activation is authorized. `VOICE_RUNTIME_ENABLED` remains disabled by default.

### Phase 2B.2B authoritative upstream termination

The successful handshake stores the server-extracted `upstream_call_id` in the same transaction that changes the owned, unexpired `starting` row to `active`. An active row requires a call ID and `termination_status = 'not_requested'`. SDP is withheld until that transaction commits.

The local lifecycle remains `starting`, `active`, and `ended`. A separate termination lifecycle is frozen as:

- `not_applicable`: no identified upstream call exists;
- `not_requested`: the active call is identified but no end is requested;
- `pending`: durable termination intent is ready or scheduled;
- `in_progress`: one claimant owns a bounded lease;
- `accepted`: OpenAI returned the documented `200 OK` and began termination; and
- `failed_permanent`: the failure is non-retryable or the bounded attempt limit was exhausted.

`accepted` does not claim teardown completion. `ended` does not imply `accepted`. An ended row with an upstream call ID must remain `pending`, `in_progress`, `accepted`, or `failed_permanent`.

Client end and expiry first end the local lifecycle and durably schedule termination in a database transaction. Network work occurs only after commit. Reconciliation and the `before_request` hook remain database-only. The existing background worker independently reconciles expiry, atomically leases due terminations, releases the database transaction, calls the pinned endpoint, and finalizes only when its cryptographically random claim token still owns the attempt.

SQLite claims use `BEGIN IMMEDIATE`. PostgreSQL claims use row locking with `FOR UPDATE SKIP LOCKED`. Claims have a finite lease, at most eight attempts, bounded HTTP timeouts, and exponential backoff of approximately 15, 30, 60, 120, 240, 480, and then 900 seconds with small bounded jitter. A timeout may mean OpenAI accepted an earlier request; retries are therefore at least once. OpenAI does not document hangup idempotency or already-ended behavior, so BusinessBuilder guarantees local claim idempotency only.

Expired leases are reconciled atomically before claim selection. An expired lease below eight attempts returns to immediately due `pending`; an expired eighth-attempt lease becomes `failed_permanent` without a ninth HTTP request. A non-expired lease remains exclusively owned. Activation-failure cleanup preserves `accepted`, `failed_permanent`, valid `in_progress` leases, and existing `pending` retry schedules rather than reviving or accelerating them.

The same transaction-scoped stale-lease normalization applies during reconciliation, claim selection, and every repeated end request. Any path that observes an expired eighth attempt finalizes it as `failed_permanent`; scheduling cannot revive it as `pending`. Activation-failure cleanup establishes exact user, local-session, handshake-request, and upstream-call identity attribution before mutating a row. A different newly created call ID never replaces, ends, or schedules the row's existing identity. Only that unattached new ID may receive best-effort in-memory cleanup after transaction closure; failure leaves the documented distributed-commit orphan risk because two identities cannot safely share one local row.

Before the upstream-call unique index is created, migration validates every non-null stored call ID. Invalid identities are cleared and classified without HTTP. Duplicate valid identities retain one deterministic survivor: a consistent terminal record is preferred, followed by a consistent terminating record, a consistent active record, stable recency, and finally the highest local ID. Non-survivors are ended when necessary, have the duplicate identity cleared, and become `not_applicable`; migration never hangs up the survivor.

If valid SDP and a call ID are returned but activation fails, BusinessBuilder withholds SDP, durably records pending cleanup when possible, and attempts immediate hangup outside the transaction. A database outage after call creation may prevent durable retry storage, so an in-memory best-effort hangup is attempted. A duplicate call ID already owned by another local row is not blindly terminated. These are explicit distributed-commit residual risks.

Worker voice maintenance remains bounded per iteration and runs before generic work, while the existing generic queue keeps its prior drain behavior: processed work continues without an added polling delay and idle iterations sleep. This prevents an idle busy loop without reducing established generic throughput or starving bounded voice maintenance.

## 12. Security contract

- `OPENAI_API_KEY` remains server-side.
- The upstream host is pinned or strictly allowlisted.
- Only HTTPS is permitted.
- The documented `OpenAI-Safety-Identifier` header is used.
- Its value is derived with a server-secret HMAC over a stable internal user identifier and a voice-specific namespace.
- A plain predictable hash is prohibited.
- The safety identifier must never be exposed as a public user identifier.
- Same-origin and CSRF protection applies to every state-changing route.
- Content types are explicitly validated.
- Existing SDP-size validation is preserved.
- Transcript size is bounded by the approved canonical message limit.
- Session responses use `Cache-Control: no-store`.
- Logs must not contain API keys, authorization headers, cookies, SDP bodies, raw audio, safety identifiers, or full sensitive transcripts.
- Logs and browser responses must not contain `Location` values, upstream call IDs, termination claim tokens, or OpenAI hangup response bodies.
- Call IDs are limited to 255 UTF-8 bytes and reject whitespace, controls, path separators, ambiguous percent encoding, queries, fragments, and dot segments. No undocumented `rtc_` prefix rule is imposed.
- Hangup requests use bounded connect/read timeouts and never follow redirects.
- Duplicate termination races are controlled by database claims, expiring leases, and claim-token guarded finalization.
- User-facing errors are non-secret and operational logs use safe classifications and correlation IDs.
- Browser control remains disabled and independent from the voice feature flag.
- No second authentication system is introduced.

## 13. Privacy contract

Approved disclosure:

> BusinessBuilder does not store your raw microphone audio. During an active voice session, audio is transmitted securely to OpenAI for realtime processing. Completed transcripts may be saved in your BusinessBuilder conversation history.

Additional requirements:

- BusinessBuilder does not intentionally persist raw audio.
- The UI must not claim raw audio never leaves the device.
- Completed transcripts use existing canonical conversation storage.
- A user must explicitly click Start Voice before microphone access is requested.
- Microphone tracks end on every cleanup path.
- Transcript history remains visible and reviewable.
- Sensitive credentials must not be stored as memory or transcript metadata.
- Provisional transcript deltas are not canonical and are not persisted as completed messages.

## 14. UI state machine

Every asynchronous operation is tied to a monotonically increasing session generation/lifecycle token. A callback must be ignored if its token no longer matches the active generation. Terminal-state guards prevent stale callbacks from reviving a cleaned-up session.

| State | Allowed next states | Terminal | Retryable |
|---|---|---:|---:|
| `idle` | `requesting_microphone`, `exiting` | No | Yes |
| `requesting_microphone` | `connecting`, `error`, `stopping`, `exiting` | No | Yes |
| `connecting` | `connected`, `error`, `stopping`, `exiting` | No | Yes |
| `connected` | `listening`, `error`, `stopping`, `disconnected`, `exiting` | No | Yes |
| `listening` | `user_speaking`, `muted`, `stopping`, `disconnected`, `error`, `exiting` | No | Yes |
| `user_speaking` | `processing_transcript`, `muted`, `stopping`, `disconnected`, `error`, `exiting` | No | Yes |
| `processing_transcript` | `thinking`, `listening`, `stopping`, `disconnected`, `error`, `exiting` | No | Yes |
| `thinking` | `speaking`, `waiting_for_approval`, `completed`, `listening`, `stopping`, `disconnected`, `error`, `exiting` | No | Yes |
| `speaking` | `user_speaking`, `completed`, `waiting_for_approval`, `listening`, `muted`, `stopping`, `disconnected`, `error`, `exiting` | No | Yes |
| `muted` | `listening`, `speaking`, `waiting_for_approval`, `stopping`, `disconnected`, `error`, `exiting` | No | Yes |
| `waiting_for_approval` | `listening`, `user_speaking`, `muted`, `stopping`, `disconnected`, `error`, `exiting` | No | Yes |
| `completed` | `listening`, `user_speaking`, `muted`, `stopping`, `disconnected`, `error`, `exiting` | No | Yes |
| `disconnected` | `connecting` through explicit retry, `stopping`, `error`, `exiting` | No | Yes |
| `error` | `idle` through explicit retry, `stopping`, `exiting` | No | Yes when classified recoverable |
| `stopping` | `stopped`, `exiting` | No | No |
| `stopped` | `idle` through a new explicit Start Voice action, `exiting` | Yes for generation | Yes through new generation |
| `exiting` | navigation to `/command-center` | Yes | No |

No callback from an older generation may move `stopping`, `stopped`, or `exiting` back into an active state.

## 15. Failure contract

“Clean all” means the idempotent cleanup operation releases all applicable resources listed in section 10.

| Failure | UI state | User message | Retry | Text fallback | Resources | Local session record |
|---|---|---|---:|---:|---|---|
| Unsupported browser | `error` | Voice is not supported in this browser. Use text mode or a supported browser. | No in current browser | Yes | Clean any partial local resources | No record, or finalize `unsupported_browser` |
| Insecure context | `error` | Voice requires a secure HTTPS connection. Text mode is available. | After secure navigation | Yes | Clean all | No record, or finalize `insecure_context` |
| Permission denied | `error` | Microphone permission was denied. You can retry after allowing access or use text mode. | Yes | Yes | Stop any acquired tracks; clean all | Finalize `permission_denied` if created |
| Missing microphone | `error` | No microphone is available. Connect one or use text mode. | Yes after device change | Yes | Clean all | Finalize `microphone_unavailable` if created |
| Device disconnected | `error` | The microphone disconnected. Reconnect it and start voice again. | Yes | Yes | Clean all | Finalize `input_device_disconnected` |
| Autoplay blocked | `connected` or `error` after bounded prompt | Select Play to hear Builder, or continue in text mode. | Yes through user gesture | Yes | Keep session only during bounded recovery; otherwise clean all | Active during recovery; finalize `autoplay_blocked` on exit |
| Suspended `AudioContext` | current non-terminal state or `error` | Select Resume Voice, or continue in text mode. | Yes through user gesture | Yes | Resume once; otherwise clean audio resources/all | Active during recovery; finalize if terminated |
| SDP failure | `error` | Voice could not establish a secure connection. Text mode is available. | Yes | Yes | Clean all | Finalize `sdp_failure` |
| Login expiry | `error` then `stopping` | Your session expired. Sign in again to use voice. | After login | Yes after login | Clean all | Server finalizes `authentication_lost` |
| Active-session limit | `error` | Another voice session is active. End it or wait for expiry. | Yes after reconciliation | Yes | Clean attempted resources | Do not create duplicate; attempted record finalized if one exists |
| Upstream timeout | `error` | Voice took too long to connect. Text mode is available. | Yes | Yes | Clean all | Finalize `upstream_timeout` |
| Peer failed | `error` then `stopping` | The voice connection failed. Start again or use text mode. | Yes | Yes | Clean all | Finalize `peer_failed` |
| Temporary disconnection | `disconnected` during bounded grace | Voice connection interrupted. Reconnecting… | Automatic bounded retry | Yes | Retain only required resources during grace; clean all on expiry | Active during grace; finalize `peer_disconnected` on expiry |
| Data-channel close | `stopping` | The voice control channel closed. Start again or use text mode. | Yes | Yes | Clean all | Finalize `data_channel_closed` |
| Data-channel error | `error` then `stopping` | Voice encountered a connection error. Text mode is available. | Yes | Yes | Clean all | Finalize `data_channel_error` |
| Realtime error | Prior state if explicitly recoverable; otherwise `error` | Voice encountered an error. Text mode is available. | Classified | Yes | Clean all for terminal errors; clear affected response for recoverable errors | Active if recovered; otherwise finalize classified reason |
| Invalid session configuration | `error` then `stopping` | Voice configuration is unavailable. Text mode is available. | No until server fix | Yes | Clean all | Finalize `invalid_session_configuration` |
| Transcription failure | `listening` or retryable `error` | I could not transcribe that. Please try again or use text mode. | Yes | Yes | Clear failed turn; retain healthy session | Remains active |
| Empty transcript | `listening` | I did not hear a complete message. Please try again. | Yes | Yes | Drop empty turn only | Remains active |
| Duplicate item | No change | No duplicate user-facing message | Not applicable | Yes | Drop duplicate turn | Remains active; no duplicate message |
| Backend 409 | `thinking` then `listening` or result recovery | That message is already being processed. Please wait. | Poll/recover by idempotency policy | Yes | Retain current turn ownership until resolved; no duplicate speech | Remains active |
| Backend timeout | `error` or `listening` after failed turn | Builder took too long to respond. Your transcript remains visible; retry in text mode. | Yes with new explicit action | Yes | Mark turn failed; do not synthesize speech | Remains active unless policy terminates |
| Backend non-JSON response | `error` | Builder returned an unreadable response. Text mode is available. | Yes | Yes | Mark turn failed; no speech | Remains active unless repeated/terminal |
| Backend 5xx | `error` or `listening` after failed turn | Builder could not respond safely. Please retry or use text mode. | Yes | Yes | Mark turn failed; no speech | Remains active unless policy terminates |
| Missing canonical response | `error` | Builder did not return a complete response. Nothing was spoken or actioned. | Yes | Yes | Mark turn failed; no speech | Remains active |
| Interrupted output | `user_speaking` | No error message; keep canonical text visible | Automatic continuation by new turn | Yes | Cancel owned response and clear queued audio | Remains active |
| Page navigation | `stopping`/`exiting` | No blocking prompt required | New session after navigation | Yes | Best-effort clean all | Client best effort; server reconciliation authoritative |
| Idle timeout | `stopping` then `stopped` | Voice stopped after inactivity. Text mode remains available. | Yes through explicit start | Yes | Clean all | Finalize `idle_timeout` |
| Maximum-duration timeout | `stopping` then `stopped` | Voice reached its maximum duration. Start a new session or use text mode. | Yes subject to limits | Yes | Clean all | Finalize `max_duration_reached` |
| Server-enforced expiry | `stopping`/`disconnected` then `stopped` | The voice session expired. Start again or use text mode. | Yes subject to limits | Yes | Clean all when notified/detected | Server finalizes `server_expired` authoritatively |

For termination failures, local user-facing state remains sanitized. `429`, timeout, connection failure, and OpenAI `5xx` schedule bounded retries. `401`/`403`, redirects, unexpected successful statuses other than `200`, and other undocumented `4xx` responses become permanent safe classifications. Unknown or already-ended calls are not treated as successful without explicit official documentation.

## 16. Feature flag and rollout contract

Real voice remains disabled by default until Phase 2F passes.

Requirements:

- Use a server-side voice-runtime feature flag separate from browser-control configuration.
- Browser control remains disabled unless separately and explicitly authorized.
- The dedicated voice page may remain visually available while truthfully showing preview or unavailable state.
- Do not infer voice availability from `OPENAI_API_KEY` alone.
- The server decides availability and returns only non-secret capability state to the browser.
- A disabled flag must prevent session admission even if client code attempts the route directly.

Rollout gates:

- exact-event tests;
- transcript feedback-loop regression test;
- concurrent session-start test;
- server-expiry test;
- cleanup-path tests;
- microphone-denied test;
- `pagehide` test;
- backend-error tests;
- approval-authority test;
- privacy review;
- local browser review without production enablement;
- explicit deployment approval; and
- production smoke test only after that approval.
- verified liveness of the existing `businessbuilder-ai-worker` service and successful processing of a synthetic mocked termination path.

## 17. Phase 2B–2F implementation plan

### 17.1 Phase 2B — Secure current handshake

**Intended files**

- `app.py`
- server tests in the repository’s approved test location
- configuration documentation, if later authorized

**Behavior**

- Implement the current nested session schema.
- Pin the upstream endpoint.
- Correct `OpenAI-Safety-Identifier` and use server-secret HMAC derivation.
- Add server-side feature-flag, entitlement/preference, CSRF/origin, voice, content-type, transcript-limit, and admission checks.
- Make admission atomic and add periodic stale-session/server-expiry reconciliation without a Phase 2A schema change.
- Capture and validate the server-owned `Location` call ID, store it atomically during activation, and add durable upstream termination intent, leases, bounded retries, and existing-worker processing.

**Acceptance tests**

- Exact multipart handshake shape without real OpenAI calls.
- Host pinning and HTTPS rejection.
- API key never returned or logged.
- Safety identifier is stable, secret-keyed, and not the internal user ID.
- Concurrent admission cannot exceed the configured active limit on SQLite and PostgreSQL.
- Upstream timeout/failure finalizes the local record.
- Exact `201` creation and `Location` validation with no call ID exposed to the browser.
- Exact `200` hangup acceptance, sanitized failure classification, claim-token finalization, stale-lease recovery, retry exhaustion, and no HTTP inside database transactions.
- Expiry schedules termination for independent worker processing without sideband.
- Disabled feature flag rejects direct route use.
- One numeric canonical agent-message limit is documented and enforced.

**Rollback point**

- Disable the server-side voice-runtime flag; retain the Phase 1 visual page and existing text agent.

**Risks**

- Schema drift, database locking differences, proxy timeouts, incorrect CSRF integration, and abandoned usage records.

**Explicit stop gate**

- Stop before Phase 2C if current official schema verification, atomic admission on both databases, server expiry, or secret-handling tests fail.

### 17.2 Phase 2C — Dedicated page connection

**Intended files**

- `templates/voice_command_center.html`
- `static/js/voice-command-center.js`
- `static/js/realtime-voice.js` or one approved replacement module
- `static/css/voice-command-center.css` only for truthful runtime states
- client tests

**Behavior**

- Connect only from `/command-center/voice`.
- Require an explicit Start Voice click before `getUserMedia`.
- Implement generation-token state guards, peer/data-channel lifecycle, mute, stop, Escape, navigation, and shared cleanup.
- Replace Phase 1 disclosures only when runtime capability is actually enabled.
- Keep the page truthful when disabled.

**Acceptance tests**

- No permission prompt on page load.
- Permission-denied, missing-device, insecure-context, unsupported-browser, autoplay, and suspended-`AudioContext` paths.
- Stop/Escape/pagehide invoke the same idempotent cleanup.
- Late callbacks cannot revive a stopped generation.
- Disabled runtime remains a safe preview.

**Rollback point**

- Disable the voice-runtime flag and restore preview behavior without changing backend text operation.

**Risks**

- Browser device variance, autoplay policy, accessibility regressions, leaked media tracks, and duplicate event handlers.

**Explicit stop gate**

- Stop before Phase 2D if any cleanup path leaves a microphone/remote track, data channel, peer connection, `AudioContext`, animation frame, or active local session record.

### 17.3 Phase 2D — Exact serialized turn adapter

**Intended files**

- `static/js/realtime-voice.js` or approved dedicated event-adapter module
- `static/js/voice-command-center.js`
- `app.py` only for approved validation/idempotency changes
- event-contract and backend integration tests

**Behavior**

- Implement an exact-equality event switch.
- Track `item_id`, request ID, conversation ID, voice-session ID, timestamps, statuses, and session generation.
- Queue completed turns and process one backend request at a time.
- Preserve `/api/agent/message` ownership and idempotency.
- Never route assistant output captions into user turns.

**Acceptance tests**

- Every event in section 5 has a fixture.
- `response.output_audio_transcript.done` cannot call `/api/agent/message`.
- Repeated item/request IDs are deduplicated.
- Equal transcript text in distinct items remains two valid turns.
- Out-of-order completion and backend response fixtures preserve received-order speech.
- Backend 409, timeout, non-JSON, 5xx, empty, oversized, and failed transcription paths.

**Rollback point**

- Disable voice runtime; no migration of canonical conversation data is required.

**Risks**

- Feedback loops, race conditions, stale response ownership, duplicate persisted messages, and hidden assumptions about event order.

**Explicit stop gate**

- Stop before Phase 2E if the feedback-loop regression, serialized ordering, ownership, or idempotency suites are not deterministic.

### 17.4 Phase 2E — Output, captions, interruption and spoken exit

**Intended files**

- `static/js/realtime-voice.js` or approved output module
- `static/js/voice-command-center.js`
- `templates/voice_command_center.html`
- focused output/interruption/accessibility tests

**Behavior**

- Display canonical backend text exactly.
- Treat Realtime output captions as non-authoritative rendering aids.
- Render ordinary non-consequential canonical answers dynamically without changing their authority.
- Implement reviewed lifecycle phrase banks and the small controlled approval phrase bank.
- For consequential or approval-needed responses, use only an approved phrase semantically equivalent to the canonical approval-notification baseline; never freely paraphrase consequential details.
- Track active response IDs, clear queued audio, and prevent stale completion after interruption.
- Implement the exact spoken-exit allowlist and route every exit through shared cleanup.

**Acceptance tests**

- Approval responses use only reviewed semantically equivalent phrase-bank entries, state that execution is paused pending panel review, never imply action occurred, and never auto-continue.
- Ordinary dynamic speech remains grounded in the canonical response and cannot invent facts, actions, amounts, or conclusions.
- Lifecycle phrase selection avoids immediate repetition, preserves exact state meaning, does not speak over the user, and respects audio, caption, and reduced-motion settings.
- Authenticated greeting context is allowlisted and unavailable before Start Voice and session readiness.
- Saved voice selection is server-allowlisted with a safe fallback; arbitrary client voices, mid-session voice changes, and impersonation are rejected.
- Canonical visible text survives cancel, playback failure, and barge-in.
- Exact allowlist normalization tests, including negative substring cases.
- Known-response-only cancellation.
- Late response/output-buffer events cannot change a cancelled generation’s state.
- Button, Escape, spoken, navigation, timeout, and failure exits converge on identical cleanup.

**Rollback point**

- Disable spoken output and/or the full voice-runtime flag while preserving canonical text conversation history.

**Risks**

- Non-verbatim model speech, misleading captions, late audio, accidental exit matching, and inconsistent Realtime conversation state.

**Explicit stop gate**

- Stop before Phase 2F if consequential content can be freely summarized, approval wording can omit any required meaning, dynamic speech can invent or override canonical content, canonical text can be changed/lost, substring exit matching occurs, or cancelled output can resume.

### 17.5 Phase 2F — Failure recovery, tests and controlled rollout

**Intended files**

- all approved voice modules and server routes
- automated unit, integration, database-concurrency, and browser test files
- deployment/runbook documentation
- observability configuration that excludes sensitive data

**Behavior**

- Complete the failure matrix, cleanup recovery, server reconciliation, safe telemetry, rate-limit behavior, accessibility, privacy review, rollout flagging, and rollback runbook.
- Keep production disabled until explicit deployment approval.

**Acceptance tests**

- Every rollout gate in section 16.
- Full failure-matrix coverage.
- Phrase-bank repetition, canonical-authority, consequential-wording, greeting-context, voice-allowlist, and impersonation-negative coverage.
- Long-session expiry and stale-record reconciliation.
- No secret, SDP, raw-audio, safety-identifier, or full-sensitive-transcript logging.
- Cross-browser local review.
- Production smoke test only after explicit deployment approval.

**Rollback point**

- Server-side kill switch disables all new sessions; reconciliation ends active records; `/command-center/voice` returns to truthful preview/unavailable state; text mode remains operational.

**Risks**

- Environment-specific media behavior, production proxy/network differences, telemetry privacy, cost/rate-limit spikes, and incomplete rollback.

**Explicit stop gate**

- Do not deploy or enable production voice until all Phase 2F gates pass, privacy/security reviewers approve, and the user explicitly authorizes deployment.

## 18. Source grounding

Repository sources reviewed for this freeze:

- `app.py`: voice configuration, session accounting, unified-interface handshake, `/api/agent/message`, session start/end routes.
- `static/js/realtime-voice.js`: WebRTC lifecycle, legacy session update, event routing, backend turns, output, timers, and cleanup.
- `static/js/command-center.js` and `templates/command_center.html`: dormant legacy integration.
- `static/js/voice-command-center.js` and `templates/voice_command_center.html`: Phase 1 dedicated visual preview.

Current official OpenAI sources:

- [Realtime API with WebRTC](https://developers.openai.com/api/docs/guides/realtime-webrtc)
- [Realtime conversations and events](https://developers.openai.com/api/docs/guides/realtime-conversations)
- [Voice activity detection](https://developers.openai.com/api/docs/guides/realtime-vad)
- [Realtime transcription](https://developers.openai.com/api/docs/guides/realtime-transcription)
- [Webhooks and server-side controls](https://developers.openai.com/api/docs/guides/realtime-server-controls)
- [Create Realtime call](https://developers.openai.com/api/reference/resources/realtime/subresources/calls/methods/create)
- [Hang up Realtime call](https://developers.openai.com/api/reference/resources/realtime/subresources/calls/methods/hangup)
- [Realtime SIP hangup guidance](https://developers.openai.com/api/docs/guides/realtime-sip#hang-up-the-call)
- [Voice-agent architecture](https://developers.openai.com/api/docs/guides/voice-agents)
- [`gpt-realtime-2.1` model](https://developers.openai.com/api/docs/models/gpt-realtime-2.1)

The official documentation is authoritative for OpenAI model availability, field names, and event semantics. Phase 2B must re-check the live schema before implementation because these interfaces may evolve after this freeze.

## 19. Frozen decisions summary

- Architecture: hybrid/chained; BusinessBuilder backend is authoritative.
- Realtime tools: none.
- Backend-turn event: exact `conversation.item.input_audio_transcription.completed` only.
- Ordinary speech: dynamic only when non-consequential and grounded in the canonical `/api/agent/message` response; visible canonical text remains authoritative.
- Lifecycle speech: small reviewed phrase banks implemented in Phase 2E without false state claims or avoidable repetition.
- Approval speech: small controlled phrase bank semantically equivalent to the canonical notification; execution remains paused and the canonical UI and approval card remain authoritative.
- Voice identity: saved server-allowlisted voice with safe fallback, no mid-session change, and no impersonation; one consistent BusinessBuilder personality.
- Spoken exit: exact normalized six-phrase allowlist.
- Cleanup: one idempotent operation for every exit/failure path.
- Upstream call identity: server-owned `Location` extraction only; never browser supplied or browser returned.
- Termination: local `ended` is distinct from `pending`, `in_progress`, `accepted`, and `failed_permanent`; only exact upstream `200` means accepted initiation.
- Scheduling: expiry is database-only and the existing worker performs leased, bounded, at-least-once hangup attempts; production worker liveness is a rollout gate.
- Sideband: not required for Phase 2B.2B hangup and remains outside this phase.
- Security: server-only key, pinned HTTPS endpoint, documented safety header, secret HMAC identifier, same-origin/CSRF, bounded input, non-sensitive logs.
- Privacy: raw microphone audio is not intentionally stored by BusinessBuilder but is transmitted to OpenAI during an active session.
- Availability: dedicated server-side voice-runtime flag, disabled by default and independent of browser control.
- Rollout: no production enablement before Phase 2F and explicit deployment approval.
