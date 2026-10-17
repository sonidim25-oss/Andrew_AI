# Owner input and unbounded model output

The October 17 16:30 incident contained an autonomous provider-response failure
followed by a blocked continuation when processing waiting owner input. The two
failure notices did not represent two paid provider requests. Research revision 5
was saved at 16:29:12; the owner input was added to the saved conversation only
at 16:30:37. The diagnostic cannot recover the rejected response's exact shape.

Autonomous turns now identify their owner to the gateway. Before each provider
dispatch, the gateway checks durable owner input. If input is waiting, it ends
that autonomous turn with a non-retryable `owner_input_pending` scheduling signal.
The current provider request and tool results finish normally before this next
request boundary. There is no concurrent model conversation or interrupted tool
execution. Letta's real protocol is tested to yield without a provider dispatch,
then accept the waiting owner turn on the same saved conversation.

A yielded cycle is recorded as stopped without setting an autonomy error or
discarding the active session. The normal owner conversation idle period governs
when autonomous work resumes. Startup clears the transient autonomous-turn marker.

After a session failure, incoming chat is saved as pending, with an explanation
that no model call was made. The gateway also refuses new dispatches while the
error is set. Existing uncertain inputs and provider reservations remain held;
installation never authorizes their replay.

The subsequent 17:07 owner turn kept making paid model requests and executing
tools until the owner cancelled it at 17:25. It was not a silent provider timeout.
Completed reply inputs were correctly marked processed; interrupted requests were
held, and the old status counted these held requests as queued.

Owner replies now have an explicit model mode distinct from autonomous research.
After three provider rounds in an owner turn, the next request uses
`tool_choice: none` and asks for a direct text answer with the findings so far.
It does not truncate a response, interrupt an executing tool, or limit autonomous
work. The count resets for each direct or queued owner reply. The pinned-protocol
regression deliberately requests tools until text is required, then checks that
the owner receives a reply and sleeping messages leave the pending queue.

Status reports pending, processing and held inputs separately. It also shows the
current reply phase and the age of its last update. These are local snapshots,
not claims that an idle or interrupted model is thinking. Old held requests stay
held; there is no evidence-based reason to mark them all answered or replay them.

Agent model settings clear the former output-token setting. The gateway removes
both `max_tokens` and `max_completion_tokens`, including values supplied by the
runtime. The direct provider reply path also omits its old 512-token cap. Provider
model limits still apply. The 6,000-token value retained in configuration is named
`OUTPUT_TOKEN_COST_ESTIMATE` and is used only for provisional cost accounting when
usage is missing. It is never sent as a generation limit. Reported actual usage
replaces provisional accounting, including responses above 6,000 output tokens.

Deploy using `sudo python3 scripts/install-autonomy-media.py --apply` from this
checkout. The installer tests the merged deployed code before changing services
and preserves error state and uncertain messages. These changes are not installed
until that command succeeds. No wake, replay, or model request is part of deployment.
