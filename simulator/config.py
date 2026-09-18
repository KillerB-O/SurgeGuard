RANDOM_SEED = 42

FACILITY_ID = "WH-01"

NORMAL_DEMAND_WU_PER_HOUR = 32
SURGE_1_DEMAND_WU_PER_HOUR = 62
SURGE_2_DEMAND_WU_PER_HOUR = 91
SURGE_3_DEMAND_WU_PER_HOUR = 127

FULFILLMENT_BASELINE_WU_PER_HOUR = 52

# The largest work-unit value CommerceSimulator can put on a single order, i.e.
# the top of WORK_UNIT_DISTRIBUTION. It bounds how much unspent capacity an idle
# station may carry, so a floor that has had nothing to do cannot bank an hour of
# throughput and discharge it the moment work arrives.
#
# This is the SIMULATOR's own ceiling, deliberately not the backend's
# catalog/work_units.json `max_work_units` (4.0). The two differ because the
# backend reclassifies each order from its attributes rather than trusting the
# number the producer sent, so coupling this to the backend's figure would tie
# the floor's carry to a value the simulator never produces.
MAX_ORDER_WORK_UNITS = 2.5

# Events posted at once on the Simulator -> n8n -> backend path. A tick of a
# real surge is a few hundred independent events, and a fresh connection per
# event costs about 256ms against 72ms on a kept-alive one, so a day-long surge
# spent most of its time in TCP and n8n handshakes rather than in the scenario.
# Events within a tick are independent -- distinct orders, one status step each
# -- and each carries its own idempotency key, so order between them carries no
# meaning. Kept modest: the point is to stop paying setup costs serially, not to
# see how hard n8n can be pushed.
EVENT_CONCURRENCY = 8
