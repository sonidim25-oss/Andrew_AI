# Flexible personality tendencies rollout

The six optional tendencies and the owner's provenance clarification update
`LettaAgent.system_prompt` in `appserver.py`. They are owner-suggested starting
guidance, not established personality facts or self-discoveries. Andrew may
revise, reject, or replace them; rest, silence, uncertainty, and changing direction
remain valid. The existing dated personality revision mechanism remains in use.
No new behavior tracking or wake obligations are introduced.

The deployment branch includes the installed P2-10 and runner pre-grant recovery
fixes. Reconstruction of the enabled installation matches controller
`680776f2e8d7f0adf75d84605fba866a9b048d3514ebb52e307d6d611271aade`
and runner `5902ee5a385e07b97823c7065208115d0a4d2e347cd308b5e6161ce0636f60c6`.
Only controller `appserver.py` changes. Proposed controller digest:
`2c43ab5d69c4c01d8ead58729ab35d750f67eb634f0868975fa7db3e1c37993b`.
Runner and backend packages are unchanged. Uninstalled memory/timezone packages
are not included.

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
baseline pins. The pending memory/timezone rollout also needs its own updated
baseline before use.
