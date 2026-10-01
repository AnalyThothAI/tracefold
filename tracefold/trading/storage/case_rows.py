"""Typed SQL projections from the case's owned forecast and label documents."""

ASSESSMENT_ROWS_SQL = """
SELECT c.case_id,c.program_sha,a.route,a.status,a.forecast,a.drivers,a.notes,
       a.input_tokens,a.output_tokens,a.started_at_ms,a.ended_at_ms
FROM trading_cases c CROSS JOIN LATERAL jsonb_to_record(c.assessment) AS a(
  route text,status text,forecast jsonb,drivers jsonb,notes jsonb,input_tokens int,output_tokens int,
  started_at_ms bigint,ended_at_ms bigint)
WHERE c.assessment IS NOT NULL
"""
ACTION_ROWS_SQL = """
SELECT c.case_id,c.program_sha,a.policy_id,a.policy_version,a.calibrator_version,a.action,a.reason,
       a.expected_r,a.publish_status,a.signal_id,a.decided_at_ms
FROM trading_cases c CROSS JOIN LATERAL jsonb_to_recordset(c.policy_decisions) AS a(
  policy_id text,policy_version text,calibrator_version text,action text,reason text,expected_r numeric,
  publish_status text,signal_id text,decided_at_ms bigint)
"""
PAPER_ROWS_SQL = """
SELECT c.case_id,p.key AS side,l.geometry_version,l.status,l.outcome,l.reason,l.anchor_at_ms,l.exit_at_ms,
       l.anchor_price,l.exit_price,l.gross_bps,l.cost_bps,l.net_r,l.labeled_at_ms
FROM trading_cases c CROSS JOIN LATERAL jsonb_each(c.paper_legs) p
CROSS JOIN LATERAL jsonb_to_record(p.value) AS l(
  geometry_version text,status text,outcome text,reason text,anchor_at_ms bigint,exit_at_ms bigint,
  anchor_price numeric,exit_price numeric,gross_bps numeric,cost_bps numeric,net_r numeric,labeled_at_ms bigint)
"""
