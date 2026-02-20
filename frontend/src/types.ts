export type SetupFieldGroup = "mandatory" | "optional" | "live";
export type SetupFieldValueType = "number" | "string" | "select" | "string_list";
export type SetupFieldSource = "ui" | "http" | "import" | "system";

export type SetupField = {
  field_id: string;
  group: SetupFieldGroup;
  label: string;
  required: boolean;
  value_type: SetupFieldValueType;
  unit: string | null;
  options: string[];
  current_value: unknown;
  valid: boolean;
  missing: boolean;
  dirty: boolean;
  last_source: SetupFieldSource | null;
  last_update_ts: string | null;
  http_path_template: string;
  http_override_active: boolean;
  http_override_last_ts: string | null;
  error: string | null;
};

export type SetupFieldUpdatePayload = {
  field_id: string;
  value: unknown;
  source: SetupFieldSource;
  ts?: string | number | null;
  timestamp?: string | number | null;
};

export type SetupFieldUpdateResult = {
  field_id: string;
  status: "saved" | "rejected";
  error: string | null;
  field: SetupField;
};

export type SetupFieldPatchResponse = {
  results: SetupFieldUpdateResult[];
};

export type SetupReadinessItem = {
  field_id: string;
  required: boolean;
  status: "ok" | "warning" | "blocked";
  message: string;
};

export type SetupReadiness = {
  readiness_level: "ready" | "degraded" | "blocked";
  blockers_count: number;
  warnings_count: number;
  items: SetupReadinessItem[];
};

export type SetupExportPackageV2 = {
  format: "eos-webapp.inputs-setup.v2";
  exported_at: string;
  payload: Record<string, unknown>;
};

export type SetupImportResponse = {
  applied: boolean;
  message: string;
  warnings: string[];
};

export type StatusResponse = {
  status: string;
  timestamp: string;
  setup?: {
    readiness_level?: string;
    blockers_count?: number;
    warnings_count?: number;
  };
  eos?: {
    health_ok?: boolean;
    last_run_datetime?: string | null;
  };
};

export type CollectorStatus = {
  running: boolean;
  poll_seconds: number;
  last_poll_ts: string | null;
  last_successful_sync_ts: string | null;
  last_observed_eos_run_datetime: string | null;
  force_run_in_progress: boolean;
  last_force_request_ts: string | null;
  last_error: string | null;
  aligned_scheduler_enabled: boolean;
  aligned_scheduler_minutes: string;
  aligned_scheduler_delay_seconds: number;
  aligned_scheduler_next_due_ts: string | null;
  aligned_scheduler_last_trigger_ts: string | null;
  aligned_scheduler_last_skip_reason: string | null;
};

export type EosRuntime = {
  eos_base_url: string;
  health_ok: boolean;
  health_payload: Record<string, unknown> | null;
  config_payload: Record<string, unknown> | null;
  collector: CollectorStatus;
};

export type EosForceRunResponse = {
  run_id: number;
  status: string;
  message: string;
};

export type EosPredictionRefreshScope = "all" | "pv" | "prices" | "load";

export type EosPredictionRefreshResponse = {
  run_id: number;
  scope: EosPredictionRefreshScope;
  status: string;
  message: string;
};

export type EosRunSummary = {
  id: number;
  trigger_source: string;
  run_mode: string;
  eos_last_run_datetime: string | null;
  status: string;
  started_at: string;
  finished_at: string | null;
  error_text: string | null;
  created_at: string;
};

export type EosRunDetail = EosRunSummary & {
  artifact_summary: Record<string, number>;
};

export type EosRunPlan = {
  run_id: number;
  payload_json: Record<string, unknown> | unknown[] | null;
  valid_from: string | null;
  valid_until: string | null;
  instructions: Array<Record<string, unknown>>;
};

export type EosRunSolution = {
  run_id: number;
  payload_json: Record<string, unknown> | unknown[] | null;
};

export type EosOutputCurrentItem = {
  run_id: number;
  resource_id: string;
  actuator_id: string | null;
  operation_mode_id: string | null;
  operation_mode_factor: number | null;
  effective_at: string | null;
  source_instruction: Record<string, unknown>;
  safety_status: string;
};

export type EosOutputTimelineItem = {
  run_id: number;
  instruction_id: number;
  instruction_index: number;
  resource_id: string;
  actuator_id: string | null;
  instruction_type: string;
  operation_mode_id: string | null;
  operation_mode_factor: number | null;
  execution_time: string | null;
  starts_at: string | null;
  ends_at: string | null;
  source_instruction: Record<string, unknown>;
  deduped: boolean;
};

export type OutputDispatchEvent = {
  id: number;
  run_id: number | null;
  resource_id: string | null;
  execution_time: string | null;
  dispatch_kind: string;
  target_url: string | null;
  request_payload_json: Record<string, unknown> | unknown[];
  status: "sent" | "blocked" | "failed" | "retrying" | "skipped_no_target" | string;
  http_status: number | null;
  error_text: string | null;
  idempotency_key: string;
  created_at: string;
};

export type OutputDispatchForceResponse = {
  status: string;
  message: string;
  run_id: number | null;
  queued_resources: string[];
};

export type OutputTarget = {
  id: number;
  resource_id: string;
  webhook_url: string;
  method: "POST" | "PUT" | "PATCH" | string;
  headers_json: Record<string, unknown> | unknown[];
  enabled: boolean;
  timeout_seconds: number;
  retry_max: number;
  payload_template_json: Record<string, unknown> | unknown[] | null;
  updated_at: string;
};

export type OutputTargetCreatePayload = {
  resource_id: string;
  webhook_url: string;
  method?: string;
  headers_json?: Record<string, unknown> | unknown[];
  enabled?: boolean;
  timeout_seconds?: number;
  retry_max?: number;
  payload_template_json?: Record<string, unknown> | unknown[] | null;
};

export type OutputTargetUpdatePayload = Partial<OutputTargetCreatePayload>;

export type EosRunPlausibilityFinding = {
  level: "ok" | "warn" | "error" | string;
  code: string;
  message: string;
  details: Record<string, unknown> | null;
};

export type EosRunPlausibility = {
  run_id: number;
  status: "ok" | "warn" | "error" | string;
  findings: EosRunPlausibilityFinding[];
};
