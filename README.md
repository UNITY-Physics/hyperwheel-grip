# Hyperwheel (GRIP edition)

Upload pipeline for the UNITY network. It runs on a Raspberry Pi 5 attached to
each Hyperfine Swoop scanner, receives the scanner's DICOM, pulls the raw
research data (RRDF) and uploads both, unchanged, to the right GRIP project.
Local files are deleted only after GRIP confirms each one.

Status: in development. See [docs/design.md](docs/design.md).

## Development

```
python3 -m venv .venv
.venv/bin/pip install -r requirements/dev.lock
.venv/bin/pip install -e . --no-deps
.venv/bin/python -m pytest
```

Integration tests against GRIP's hosted mock API are skipped unless
`HYPERWHEEL_GRIP_MOCK_BASE` and `HYPERWHEEL_GRIP_MOCK_KEY_FILE` are set; use
synthetic data only.

No secrets in this repository: keys, passwords and site configuration live on
each Pi under `/etc/hyperwheel`, root-only.

## Licence

Hyperwheel is licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE)
(<https://polyformproject.org/licenses/noncommercial/1.0.0>): free for
noncommercial use, including research and use by charities, educational and
public research organisations, and government institutions. The required notice
is at the top of `LICENSE`.

Releases also ship Orthanc and its libraries (Debian packages, each under its
own licence) and Orthanc Explorer 2, which is AGPL-3.0 and ships with its
licence and the exact source it was built from.

