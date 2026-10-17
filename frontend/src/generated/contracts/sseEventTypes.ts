/** GENERATED FILE — do not edit. Regenerate with `npm run codegen`.
 *  Source of truth: a backend enum (see scripts/export_contract_schemas.py).
 */

export type SseEventType =
  | 'thinking'
  | 'part.updated'
  | 'token'
  | 'token_retract'
  | 'thinking_token'
  | 'heartbeat'
  | 'error'
  | 'result'
  | 'timeout'
  | 'limit_reached'
  | 'cancelled'
  | 'run_snapshot'
  | 'viz'
  | 'filter'
  | 'ontology'
  | 'compaction_start'
  | 'compaction_end'
  | 'reasoning';
