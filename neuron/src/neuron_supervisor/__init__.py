"""neuron_supervisor — a privileged moderation bot for a stock Synapse.

The supervisor keeps a bot account promoted to room-admin in every room (using
the Synapse Admin API's ``make_room_admin``) so that an operator can always
moderate any room — even one where all human admins have left or demoted
themselves. It performs membership moderation (kick/ban) as the bot via the
Client-Server API, and content moderation (redacting a user's messages) via the
Admin API.

Phase 3 targets **unencrypted** rooms. Reading/moderating encrypted content
requires E2EE support, which arrives in a later phase.
"""

from neuron_supervisor.config import SupervisorSettings
from neuron_supervisor.core import Supervisor

__all__ = ["Supervisor", "SupervisorSettings"]
