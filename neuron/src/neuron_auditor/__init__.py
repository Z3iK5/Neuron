# SPDX-License-Identifier: Apache-2.0
"""neuron_auditor — an audit-logging bot for a Matrix homeserver.

The auditor joins rooms and streams every event it sees to a durable **sink**
(the local filesystem as JSON Lines, and/or an S3-compatible bucket) so there is
a tamper-evident record of communications for compliance.

Phase 4 targets **unencrypted** rooms: it records events as the homeserver
delivers them over ``/sync``. Reading *encrypted* room content requires E2EE
(megolm key handling), which is the dedicated Phase 5 — until then, encrypted
messages are recorded as undecryptable envelopes rather than silently dropped.
"""

from neuron_auditor.config import AuditorSettings
from neuron_auditor.core import Auditor
from neuron_auditor.sinks import FileSink, S3Sink, build_audit_record

__all__ = ["Auditor", "AuditorSettings", "FileSink", "S3Sink", "build_audit_record"]
