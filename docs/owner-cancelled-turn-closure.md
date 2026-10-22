# Closing owner-cancelled autonomous turns

An owner /pause or /sleep can revoke controller authority while a provider request
is in flight. Its settled result may reach the controller after the backend run
has stopped, leaving an admitted checkpoint that blocks the next turn and model
changes.

`close_superseded` runs on dispatch passes, including while paused, and before a
new turn, including before the agent sets its active model-turn marker. It scans the latest attempt for up to 64 autonomous bindings with a newer owner
revision, no active session/cycle/model turn, matching agent/conversation/configuration,
and no restore barrier or unrelated autonomy error. It imports exact settled provider evidence (including the
existing uncertain-provider hold), obtains yield-only authority and acknowledges
an `owner_input_pending` terminal. It never resumes inference or tool execution.
Saved tool calls are materialized as prepared records before yield, then projected
as cancelled with `executed:false`, preserving a valid conversation for the next
turn. Unknown effects remain blocked.

Caught failures revoke restricted scopes. Persistent evidence/yielding scopes
from an interrupted process can be reacquired, and a committed owner-yield
terminal can finish acknowledgement on a later pass. A lease still owned by a
backend worker must expire or be released before a new claim succeeds.

Regression tests use the real patched backend: sleep/pause during provider call,
transport failure at import/yield/ack, persistent evidence/yield scopes after
coordinator restart, and a cancelled tool-call response followed by a fresh turn.
Unit coverage refuses closure on current owner revision, active original session
or cycle, identity/configuration mismatch, and database/file restore fences.

The rollout keeps production paused, binds the four changed controller modules
to the installed c24bd8e release, and requires the installer full acceptance
(Node, core, scripts, pristine protocol compatibility and distinct UID probe).
After enable, verify no pending scope and a terminal, acknowledged, settled,
claim-free backend checkpoint before running the separately guarded Luna switch.
The closure runs while paused; waking before closure is unnecessary.

Scope: autonomous owner-cancelled turns. Owner-reply cancellations and confirmed
non-runner tools lacking a supported import remain conservatively blocked.
Held Telegram messages are not replayed by this change or the model switch.

A newer never-admitted controller attempt must not hide the actual backend
checkpoint. Only exact matching backend bindings are closed. A never-fenced,
effect-free revoked stub may be skipped on a binding/admission mismatch; its
records are preserved. Arbitrary held attempts are never auto-revoked.
