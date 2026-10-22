# Flexible personality tendencies rollout

The six optional tendencies and the owner's provenance clarification update
`LettaAgent.system_prompt` in `appserver.py`. They are owner-suggested starting
guidance, not established personality facts or self-discoveries. Andrew may
revise, reject, or replace them; rest, silence, uncertainty, and changing direction
remain valid. The existing dated personality revision mechanism remains in use.
No new behavior tracking or wake obligations are introduced.

The deployment branch includes the installed P2-10, runner pre-grant recovery,
memory-revision and timezone fixes (`02a6ee4`). Reconstruction of the enabled installation matches controller
`4aee0fc2f9452db9ec643a7e88c55369ea0a4e444b339e55412a9a05d0b08518`
and runner `5902ee5a385e07b97823c7065208115d0a4d2e347cd308b5e6161ce0636f60c6`.
Only controller `appserver.py` changes. Proposed controller digest:
`167b77cab9b70b2372a36c066f9bee190757b8202195bf125828889309f923a1`.
Runner and backend packages are unchanged. All installed memory/timezone changes
are retained; the runtime delta remains only the approved guidance in appserver.py.

`scripts/tendencies_deployment.py` reuses the existing guarded rollout. It pins
this baseline and the source commit, requires pause and idle state, checks restore
barriers and snapshot headroom, and runs privileged candidate acceptance before
applying. Passing core evidence is reused only for identical inputs. Complete
saved acceptance is checked on rerun; a previous installation record requires
inspection. Owner state, billing, reservations, installed digests, and services
are verified before and after the relevant deployment steps.

The ordinary startup refresh sends the new system prompt to the existing agent;
no replacement agent, personality revision, or historical owner message is
created by this change. New-agent creation receives the same prompt. Deployment
keeps Andrew paused and does not replay messages or clear existing errors.
Behavior during future autonomous wakes remains an observation, not a tested
outcome or required activity target.

The frozen owner launcher is
`sudo /home/daniel/deploy-andrew-flexible-tendencies-20261022.sh`.
Keep Andrew paused until postdeployment verification has passed. If another
release changes adoption first, rebuild this package rather than weaken its
baseline pins. The previous personality launcher refused before acceptance or
installation because memory/timezone had been installed in the meantime. This
package replaces it with an exact reconstruction of that new baseline.
