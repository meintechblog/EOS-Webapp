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
  advanced: boolean;
  item_key: string | null;
  category_id: string | null;
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

export type SetupEntityType = "pv_plane" | "electric_vehicle" | "home_appliance" | "home_appliance_window";
export type SetupEntityAction = "add" | "remove";

export type SetupCategoryItem = {
  item_key: string;
  label: string;
  entity_type: SetupEntityType | null;
  parent_item_key: string | null;
  deletable: boolean;
  base_object: boolean;
  required_count: number;
  invalid_required_count: number;
  fields: SetupField[];
};

export type SetupCategory = {
  category_id: string;
  title: string;
  description: string | null;
  requirement_label: "MUSS" | "KANN" | "MUSS/KANN";
  repeatable: boolean;
  add_entity_type: SetupEntityType | null;
  default_open: boolean;
  required_count: number;
  invalid_required_count: number;
  item_limit: number | null;
  items: SetupCategoryItem[];
};

export type SetupLayout = {
  generated_at: string;
  invalid_required_total: number;
  categories: SetupCategory[];
};

export type SetupEntityMutatePayload = {
  action: SetupEntityAction;
  entity_type: SetupEntityType;
  item_key?: string;
  clone_from_item_key?: string;
  parent_item_key?: string;
};

export type SetupEntityMutateResponse = {
  status: "saved" | "rejected";
  message: string;
  warnings: string[];
  layout: SetupLayout;
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
  config?: {
    eos_visualize_safe_horizon_hours?: number | null;
  };
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

export type EosAutoRunPreset = "off" | "15m" | "30m" | "60m";

export type CollectorStatus = {
  running: boolean;
  poll_seconds: number;
  last_poll_ts: string | null;
  last_successful_sync_ts: string | null;
  last_observed_eos_run_datetime: string | null;
  force_run_in_progress: boolean;
  last_force_request_ts: string | null;
  last_error: string | null;
  auto_run_preset: EosAutoRunPreset;
  auto_run_enabled: boolean;
  auto_run_interval_minutes: number | null;
  aligned_scheduler_enabled: boolean;
  aligned_scheduler_minutes: string;
  aligned_scheduler_delay_seconds: number;
  aligned_scheduler_next_due_ts: string | null;
  aligned_scheduler_last_trigger_ts: string | null;
  aligned_scheduler_last_skip_reason: string | null;
  price_backfill_last_check_ts: string | null;
  price_backfill_last_attempt_ts: string | null;
  price_backfill_last_success_ts: string | null;
  price_backfill_last_status: string | null;
  price_backfill_last_history_hours: number | null;
  price_backfill_cooldown_until_ts: string | null;
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

export type EosAutoRunUpdateResponse = {
  preset: EosAutoRunPreset;
  applied_slots: number[];
  runtime: EosRuntime;
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

export type EosRunContext = {
  run_id: number;
  parameter_profile_id: number | null;
  parameter_revision_id: number | null;
  parameter_payload_json: Record<string, unknown> | unknown[];
  mappings_snapshot_json: Record<string, unknown> | unknown[];
  live_state_snapshot_json: Record<string, unknown> | unknown[];
  runtime_config_snapshot_json: Record<string, unknown> | unknown[];
  assembled_eos_input_json: Record<string, unknown> | unknown[];
  created_at: string;
};

export type EosRunPredictionSeriesPoint = {
  date_time: string;
  elec_price_ct_per_kwh: number | null;
  pv_ac_kw: number | null;
  pv_dc_kw: number | null;
  load_kw: number | null;
};

export type EosRunPredictionSeries = {
  run_id: number;
  source: string;
  points: EosRunPredictionSeriesPoint[];
};

export type DataSignalSeriesResolution = "raw" | "5m" | "1h" | "1d";

export type DataSignalSeriesPoint = {
  ts: string;
  value_num: number | null;
  value_text: string | null;
  value_bool: boolean | null;
  value_json: Record<string, unknown> | unknown[] | null;
  quality_status: string | null;
  source_type: string | null;
  run_id: number | null;
  min_num: number | null;
  max_num: number | null;
  avg_num: number | null;
  sum_num: number | null;
  count_num: number | null;
  last_num: number | null;
};

export type DataSignalSeries = {
  signal_key: string;
  resolution: DataSignalSeriesResolution;
  points: DataSignalSeriesPoint[];
};

export type EosOutputCurrentItem = {
  run_id: number;
  resource_id: string;
  actuator_id: string | null;
  operation_mode_id: string | null;
  operation_mode_factor: number | null;
  requested_power_kw: number | null;
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
  requested_power_kw: number | null;
  execution_time: string | null;
  starts_at: string | null;
  ends_at: string | null;
  source_instruction: Record<string, unknown>;
  deduped: boolean;
};

export type EosOutputSignalItem = {
  signal_key: string;
  label: string;
  resource_id: string | null;
  requested_power_kw: number | null;
  unit: "kW";
  operation_mode_id: string | null;
  operation_mode_factor: number | null;
  effective_at: string | null;
  run_id: number | null;
  json_path_value: string;
  last_fetch_ts: string | null;
  last_fetch_client: string | null;
  fetch_count: number;
  status: string;
};

export type EosOutputSignalsBundle = {
  central_http_path: string;
  run_id: number | null;
  fetched_at: string;
  signals: Record<string, EosOutputSignalItem>;
};

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
