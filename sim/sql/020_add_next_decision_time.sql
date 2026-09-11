-- V(s) needs hour_of_day/day_of_week for the NEXT state too (order density varies a lot by
-- both), not just the current one -- derived from this timestamp at training time rather than
-- storing two more redundant integer columns.
alter table training_transitions add column next_decision_time timestamptz;
