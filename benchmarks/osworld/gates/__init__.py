"""Automatic gates of the X22 confirmatory campaign.

Three independent layers, two of them deterministic. They exist because at the measured throughput
(144 Astra runs a day) a misconfiguration found after two days has already produced ~290 runs to
throw away, and an arm that cannot win any more burns a day before anyone notices.

  contract.py    Gate A -- before a group is launched: does the configuration on disk match the
                 frozen lock? Exit non-zero and nothing is launched.
  monitor.py     Gate B -- after every run: 24 provenance invariants plus rate alarms on a sliding
                 window. A violation writes STOP and a reason; a halt is never lifted
                 automatically, because it means a human has to diagnose something.
  sequential.py  Gate C -- only at the pre-registered looks: efficacy against the frozen
                 boundaries, arithmetic futility, harm, and the append-only look register.
  calibrate.py   E7 -- computes the look boundaries and the power table from the per-task
                 distributions already on disk, and writes them into the lock once.
"""
