# Licensing

## ROVOps code
This repository is provided under the license chosen by the maintainers.
See the top-level `LICENSE` file when present.

## Third-party / AGPL considerations
QGroundControl and Cockpit are GPL/AGPL. Exposing an AGPL program as a
network service can trigger source-disclosure obligations. ROVOps is designed
to broker MAVLink and telemetry without bundling a heavy GCS UI by default, so
integrating with an existing GCS keeps licensing obligations with that GCS.

Decide and document your license direction before external distribution.
