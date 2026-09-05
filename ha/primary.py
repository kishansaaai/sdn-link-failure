"""Both primary and backup use the same fenced election implementation."""
from ha.backup import LeaderElection

PrimaryController = LeaderElection
