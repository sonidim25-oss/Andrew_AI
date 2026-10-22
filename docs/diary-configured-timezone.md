Diary dates now use the configured Controller.timezone_name for saving an
entry and requiring today's entry before sleep/wake_at. Toronto remains the
default. Other timezone hardcodes are outside this change (P3-18 is partial).

Runtime change: two expressions in controller.py. Three midnight-boundary
regressions cover saving the configured date, accepting its diary for sleep,
and rejecting the previous Toronto day's diary. Lifecycle and coordinated
installer checks are also run before packaging.

The deployment baseline is installed cleanup package 1512974, including the
daily spending alert and diary retry fixes. Package reconstruction matches
installed adoption digests. Exactly controller.py changes in the controller
package; the runner package, backend, worker image and schemas are unchanged.
The root rollout requires the owner pause, checks snapshot disk headroom,
runs required acceptance gates and preserves owner controls without waking.
