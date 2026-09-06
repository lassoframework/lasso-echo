-- support_tickets.is_test (2026-09-05)
--
-- Marks a ticket as one of OUR OWN test probes rather than client work, so it is excluded
-- from reports, metrics and the intake poll forever (agent/slack_convo/testdata.py is the
-- single predicate; this column is its durable form).
--
-- Written after the eight [phase4-audit] arming probes of 2026-09-05 sat in the live #fixer
-- channel looking exactly like unhandled client tickets. The heuristics in testdata.py can
-- recognise those specific rows; this column is how a probe says so about itself, with no
-- guessing at all.
--
-- Additive, defaulted, and nullable-free: no existing row changes meaning, and every reader
-- that does not know about it is unaffected. Applied to the live database 2026-09-05; this
-- file is the record of that, and the definition for any environment rebuilt from scratch.
alter table public.support_tickets
  add column if not exists is_test boolean not null default false;

comment on column public.support_tickets.is_test is
  'True when this ticket is one of our own test probes, never client work. Excluded from '
  'reports, metrics and the intake poll (agent/slack_convo/testdata.py).';
