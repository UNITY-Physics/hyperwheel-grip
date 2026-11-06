# Dev Pi test tools

For the dev Pi only, never a site Pi. They need the Hyperwheel source (`src/hyperwheel`)
next to them, and a venv with the pinned runtime wheels installed offline.

## Bad-network test

`netem.sh` shapes traffic leaving `eth0` (the scanner cable, and so the Pi's internet) in timed
phases with `tc netem`, then puts `eth0` back as it was. The hotspot (`wlan0`), and SSH over
it, are never touched. `netem_upload.py` meanwhile uploads synthetic studies to GRIP's mock and
prints what the Upload Monitor and `hyperwheel doctor` say whenever it changes.

1. Start the uploader first (it waits for the phases):
   `venv/bin/python -u netem_upload.py 60 --conf e2e-netem`
   The `--conf` folder holds a `site.toml` (its own `state_db` and `staging_dir`, `[rrdf]
   enabled = false`, the scanner's address and `known_hosts`) and a `routing.toml` pointing at
   GRIP's mock, with the mock's key in the site's `keys_dir`. None of these go in git.
2. Then, in your own terminal (sudo needs a password):
   `ssh -t hyperwheel-dev 'sudo bash ~/hw-scp-test/netem.sh'` for every phase (about 22
   minutes), or a list of phases, e.g. `... netem.sh lossy` (3 minutes) or `... netem.sh lossy:5`.
   `bash netem.sh --dry-run lossy` shows the plan without changing anything.
3. To undo at once: `sudo tc qdisc del dev eth0 root`.

Phases: `baseline` (2 min), `throttled` (2 Mbit/s, 200 ms; 4 min), `lossy` (2 Mbit/s,
300 ms, 15% loss; 3 min), `latency` (1.5 s; 3 min), `cut` (all outgoing packets dropped;
3 min), `slow` (256 kbit/s, 600 ms; 3 min), `cut-short` (1 min), `normal` (3 min).
