# Owner-selected ElevenLabs voice

Selected voice ID: `pvY1pikBdoI4SB62vEVo`.
Model remains `elevenlabs/eleven-v3`, through the existing OpenRouter account.
OpenRouter documents support for public ElevenLabs voice IDs in its `voice`
field. Availability of this particular voice still requires the live preview;
private account voices may require the owner's ElevenLabs key through BYOK.

`MediaPolicy` now accepts the existing seven named voices or a 20-character ASCII
alphanumeric ElevenLabs voice ID in owner configuration (`ANDREW_TTS_VOICE`).
The model cannot select another voice per job. Saved jobs retain their original
voice and model. The compiled default stays George for existing installations;
the owner setup selects this voice through a final systemd EnvironmentFile.

The setup launcher requires sudo, the exact installed release, Andrew paused
and idle, cleared restore barriers, and snapshot headroom. Current controller
baseline is `167b77ca...`, runner `5902ee5a...`, backend `cd367258...`. The new
backend is reused, not replaced. The personality, memory, timezone, budget and
recovery fixes are retained. Only controller `media.py` changes in the runtime
package; proposed controller digest is
`31bd1654d313c5ff20cd8be5833922a1fbfdbf4cc6570ef55f1539a02b10e581`.

Before activation, a short bilingual preview runs under `andrew-control` with
the existing encrypted systemd vault credential. No key is printed. Its
isolated SQLite receipt commits admission before the paid call; repeated runs
reuse completed audio and refuse interrupted/uncertain attempts. Audio and
receipt survive restart. The preview is separate from Andrew's live media queue
and does not send a Telegram message or wake him. This is an explicit owner
sample charged to the shared OpenRouter balance, outside autonomous media work.

The setup checks the audio digest and decodes metadata using ffprobe, then
copies the sample for the owner to
`/home/daniel/andrew-selected-voice-20261023.mp3`. This proves the selected ID was
requested and playable audio was received; it does not independently identify
the speaker or replace the owner's judgment of the voice.

`--preview-only` stops after the sample. Otherwise the setup runs privileged
package acceptance, applies/enables the code, backs up the two voice config
files, writes the selected ID to the final EnvironmentFile, and restarts only
the controller. It checks the actual controller process environment and owner
pause. Failed config verification restores the previous voice config and
restarts the controller. Installed code is retained; no database rollback is
attempted. Prior installation records require inspection before retrying.

Voice configuration paths:
- `/etc/andrew/owner-voice.conf`
- `/etc/systemd/system/andrew-control.service.d/zzzz-owner-voice.conf`

The setup leaves Andrew paused, retains billing/reservation state during code
rollout, and does not replay messages or clear historical holds. The live
preview and activation have not been run from the unprivileged Codex shell.
