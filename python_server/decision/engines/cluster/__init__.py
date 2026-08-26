"""Smart-money cluster — Gecko new pools with enough unique buyers, 5m hold.

The scan loop still runs inside ``decision.trenches`` (shared tape + open helper).
This package is the isolated home for the cluster book identity.
"""

LANE_ID = "cluster"
LABEL = "Cluster"
STRATEGY = "cluster"
