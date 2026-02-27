import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  forceEosRun,
  getEosOutputsCurrent,
  getEosOutputSignals,
  getEosOutputsTimeline,
  getEosRunContext,
  getEosRunPredictionSeries,
  getEosRunPlausibility,
  getEosRunDetail,
  getEosRunPlan,
  getEosRuns,
  getEosRunSolution,
  getEosRuntime,
  getSetupExport,
  getSetupFields,
  getSetupLayout,
  getSetupReadiness,
  getStatus,
  mutateSetupEntity,
  patchSetupFields,
  postSetupImport,
  putEosAutoRunPreset,
  refreshEosPredictions,
} from "./api";
import { OutputChartsPanel } from "./outputCharts";
import type {
  EosAutoRunPreset,
  EosPredictionRefreshScope,
  EosOutputCurrentItem,
  EosOutputSignalItem,
  EosOutputSignalsBundle,
  EosOutputTimelineItem,
  EosRunPlausibility,
  EosRunPlan,
  EosRunPredictionSeries,
  EosRunDetail,
  EosRunSolution,
  EosRunSummary,
  EosRuntime,
  SetupEntityMutatePayload,
  SetupField,
  SetupLayout,
  SetupReadiness,
  StatusResponse,
} from "./types";

type DraftState = {
  value: string;
  dirty: boolean;
  saving: boolean;
  error: string | null;
};

type RunPredictionMetrics = {
  horizonHours: number | null;
  pointCount: number | null;
  intervalMinutes: number | null;
  minPriceCt: number | null;
  maxPriceCt: number | null;
  desiredPredictionHours: number | null;
  desiredHistoricHours: number | null;
  desiredOptimizationHours: number | null;
  effectivePredictionHours: number | null;
  effectiveHistoricHours: number | null;
  effectiveOptimizationHours: number | null;
};

type RunTargetMetrics = {
  desiredPredictionHours: number | null;
  desiredHistoricHours: number | null;
  desiredOptimizationHours: number | null;
  effectivePredictionHours: number | null;
  effectiveHistoricHours: number | null;
  effectiveOptimizationHours: number | null;
};

const AUTOSAVE_MS = 1500;
const PREDICTION_HOURS_FIELD_ID = "param.prediction.hours";
const PREDICTION_HISTORIC_HOURS_FIELD_ID = "param.prediction.historic_hours";
const OPTIMIZATION_HOURS_FIELD_ID = "param.optimization.hours";
const OPTIMIZATION_HORIZON_HOURS_FIELD_ID = "param.optimization.horizon_hours";
const DETAILS_OPEN_STATE_STORAGE_KEY = "eos-webapp.details-open.v1";
const PREDICTION_HISTORIC_MAX_HOURS = 24 * 7 * 4; // 4 Wochen
const RUN_METRICS_RETRY_INTERVAL_MS = 15000;
const RUN_METRICS_RETRY_MAX_ATTEMPTS = 6;
const REFRESH_ALL_POLL_MS = 15000;
const RUN_DETAILS_POLL_MS = 15000;
const OUTPUT_SIGNALS_POLL_MS = 5000;
const RUN_CENTER_MINIMAL = false;
const AUTO_RUN_PRESET_OPTIONS: Array<{ value: EosAutoRunPreset; label: string }> = [
  { value: "off", label: "off" },
  { value: "15m", label: "15min" },
  { value: "30m", label: "30min" },
  { value: "60m", label: "60min" },
];

function loadDetailsOpenState(): Record<string, boolean> {
  if (typeof window === "undefined") {
    return {};
  }
  try {
    const raw = window.localStorage.getItem(DETAILS_OPEN_STATE_STORAGE_KEY);
    if (!raw) {
      return {};
    }
    const parsed = JSON.parse(raw);
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      return {};
    }
    const state: Record<string, boolean> = {};
    for (const [key, value] of Object.entries(parsed)) {
      if (typeof value === "boolean") {
        state[key] = value;
      }
    }
    return state;
  } catch {
    return {};
  }
}

function toInputString(field: SetupField): string {
  const value = field.current_value;
  if (value === null || value === undefined) {
    return "";
  }
  if (Array.isArray(value)) {
    return value.map((item) => String(item)).join(", ");
  }
  if (field.value_type === "number") {
    const numericValue =
      typeof value === "number"
        ? value
        : typeof value === "string"
          ? Number(value.trim())
          : null;
    if (numericValue !== null && Number.isFinite(numericValue)) {
      const maxDecimals = field.group === "live" || field.unit === "kW" ? 3 : 6;
      return String(Number(numericValue.toFixed(maxDecimals)));
    }
  }
  return String(value);
}

function toSubmitValue(field: SetupField, raw: string): unknown {
  if (field.value_type === "string_list") {
    return raw
      .split(",")
      .map((item) => item.trim())
      .filter((item) => item !== "");
  }
  return raw;
}

function formatTimestamp(value: string | null): string {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString();
}

function formatDuration(startedAt: string | null, finishedAt: string | null, nowMs?: number): string {
  if (!startedAt) {
    return "-";
  }
  const start = new Date(startedAt);
  if (Number.isNaN(start.getTime())) {
    return "-";
  }
  const fallbackNow = nowMs === undefined ? Date.now() : nowMs;
  const endMs = finishedAt ? new Date(finishedAt).getTime() : fallbackNow;
  if (Number.isNaN(endMs)) {
    return "-";
  }
  const seconds = Math.max(0, Math.round((endMs - start.getTime()) / 1000));
  if (seconds < 60) {
    return `${seconds}s`;
  }
  const minutes = Math.floor(seconds / 60);
  const restSeconds = seconds % 60;
  if (minutes < 60) {
    return `${minutes}m ${restSeconds}s`;
  }
  const hours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;
  return `${hours}h ${restMinutes}m`;
}

function toFiniteNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string") {
    const text = value.trim();
    if (text === "") {
      return null;
    }
    const parsed = Number(text);
    if (Number.isFinite(parsed)) {
      return parsed;
    }
  }
  return null;
}

function asObject(value: unknown): Record<string, unknown> | null {
  if (value === null || value === undefined || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}

function toTimestampMs(value: unknown): number | null {
  if (typeof value !== "string") {
    return null;
  }
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : null;
}

function median(numbers: number[]): number | null {
  if (numbers.length === 0) {
    return null;
  }
  const sorted = [...numbers].sort((left, right) => left - right);
  const middle = Math.floor(sorted.length / 2);
  if (sorted.length % 2 === 0) {
    return (sorted[middle - 1] + sorted[middle]) / 2;
  }
  return sorted[middle];
}

function extractRunPredictionMetrics(solutionPayload: unknown): RunPredictionMetrics | null {
  const payload = asObject(solutionPayload);
  const prediction = asObject(payload?.prediction);
  const data = prediction?.data;
  const rows: Array<{ tsMs: number; priceCt: number | null }> = [];

  if (Array.isArray(data)) {
    for (const item of data) {
      const row = asObject(item);
      if (!row) {
        continue;
      }
      const tsMs = toTimestampMs(row.date_time);
      if (tsMs === null) {
        continue;
      }
      const priceCt = (() => {
        const directCt = toFiniteNumber(row.elec_price_ct_per_kwh ?? row.electricity_price_ct_per_kwh);
        if (directCt !== null) {
          return directCt;
        }
        const eurPerKwh = toFiniteNumber(row.elec_price_amt_kwh ?? row.elecprice_marketprice_kwh);
        if (eurPerKwh !== null) {
          return eurPerKwh * 100;
        }
        const eurPerWh = toFiniteNumber(row.elecprice_marketprice_wh);
        if (eurPerWh !== null) {
          return eurPerWh * 100000;
        }
        return null;
      })();
      rows.push({ tsMs, priceCt });
    }
  } else {
    const objectData = asObject(data);
    if (!objectData) {
      return null;
    }
    for (const [key, value] of Object.entries(objectData)) {
      const row = asObject(value);
      if (!row) {
        continue;
      }
      const tsMs = toTimestampMs(row.date_time ?? key);
      if (tsMs === null) {
        continue;
      }
      const priceCt = (() => {
        const directCt = toFiniteNumber(row.elec_price_ct_per_kwh ?? row.electricity_price_ct_per_kwh);
        if (directCt !== null) {
          return directCt;
        }
        const eurPerKwh = toFiniteNumber(row.elec_price_amt_kwh ?? row.elecprice_marketprice_kwh);
        if (eurPerKwh !== null) {
          return eurPerKwh * 100;
        }
        const eurPerWh = toFiniteNumber(row.elecprice_marketprice_wh);
        if (eurPerWh !== null) {
          return eurPerWh * 100000;
        }
        return null;
      })();
      rows.push({ tsMs, priceCt });
    }
  }

  if (rows.length === 0) {
    return null;
  }

  rows.sort((left, right) => left.tsMs - right.tsMs);
  const intervalsMs = rows
    .slice(1)
    .map((row, index) => row.tsMs - rows[index].tsMs)
    .filter((value) => value > 0 && value <= 1000 * 60 * 60 * 12);
  const medianIntervalMs = median(intervalsMs);
  const intervalMinutes =
    medianIntervalMs !== null && Number.isFinite(medianIntervalMs) && medianIntervalMs > 0
      ? medianIntervalMs / (1000 * 60)
      : null;
  const horizonHours =
    medianIntervalMs !== null
      ? (rows[rows.length - 1].tsMs - rows[0].tsMs + medianIntervalMs) / (1000 * 60 * 60)
      : null;
  const pricedRows = rows
    .map((row) => row.priceCt)
    .filter((value): value is number => value !== null && Number.isFinite(value));

  return {
    horizonHours,
    pointCount: rows.length,
    intervalMinutes,
    minPriceCt: pricedRows.length > 0 ? Math.min(...pricedRows) : null,
    maxPriceCt: pricedRows.length > 0 ? Math.max(...pricedRows) : null,
    desiredPredictionHours: null,
    desiredHistoricHours: null,
    desiredOptimizationHours: null,
    effectivePredictionHours: null,
    effectiveHistoricHours: null,
    effectiveOptimizationHours: null,
  };
}

function extractRunTargetMetrics(contextPayload: unknown): RunTargetMetrics | null {
  const root = asObject(contextPayload);
  if (!root) {
    return null;
  }
  const parameterPayload = asObject(root.parameter_payload_json);
  const runtimeSnapshot = asObject(root.runtime_config_snapshot_json);
  const assembledInput = asObject(root.assembled_eos_input_json);
  const assembledRuntime = asObject(assembledInput?.runtime_config);

  const desiredSource = parameterPayload ?? runtimeSnapshot ?? assembledRuntime;
  const effectiveSource = runtimeSnapshot ?? assembledRuntime;

  const desired = extractTargetHours(desiredSource);
  const effective = extractTargetHours(effectiveSource);
  if (
    desired.predictionHours === null &&
    desired.historicHours === null &&
    desired.optimizationHours === null &&
    effective.predictionHours === null &&
    effective.historicHours === null &&
    effective.optimizationHours === null
  ) {
    return null;
  }

  return {
    desiredPredictionHours: desired.predictionHours,
    desiredHistoricHours: desired.historicHours,
    desiredOptimizationHours: desired.optimizationHours,
    effectivePredictionHours: effective.predictionHours,
    effectiveHistoricHours: effective.historicHours,
    effectiveOptimizationHours: effective.optimizationHours,
  };
}

function extractTargetHours(source: Record<string, unknown> | null): {
  predictionHours: number | null;
  historicHours: number | null;
  optimizationHours: number | null;
} {
  const prediction = asObject(source?.prediction);
  const optimization = asObject(source?.optimization);
  return {
    predictionHours: toFiniteNumber(prediction?.hours),
    historicHours: toFiniteNumber(prediction?.historic_hours),
    optimizationHours:
      toFiniteNumber(optimization?.horizon_hours) ??
      toFiniteNumber(optimization?.hours),
  };
}

function mergeRunMetrics(
  predictionMetrics: RunPredictionMetrics | null,
  targetMetrics: RunTargetMetrics | null,
): RunPredictionMetrics | null {
  if (predictionMetrics === null && targetMetrics === null) {
    return null;
  }

  return {
    horizonHours: predictionMetrics?.horizonHours ?? null,
    pointCount: predictionMetrics?.pointCount ?? null,
    intervalMinutes: predictionMetrics?.intervalMinutes ?? null,
    minPriceCt: predictionMetrics?.minPriceCt ?? null,
    maxPriceCt: predictionMetrics?.maxPriceCt ?? null,
    desiredPredictionHours: targetMetrics?.desiredPredictionHours ?? null,
    desiredHistoricHours: targetMetrics?.desiredHistoricHours ?? null,
    desiredOptimizationHours: targetMetrics?.desiredOptimizationHours ?? null,
    effectivePredictionHours: targetMetrics?.effectivePredictionHours ?? null,
    effectiveHistoricHours: targetMetrics?.effectiveHistoricHours ?? null,
    effectiveOptimizationHours: targetMetrics?.effectiveOptimizationHours ?? null,
  };
}

function formatRunMetricsLabel(
  run: EosRunSummary,
  metrics: RunPredictionMetrics | null | undefined,
  fallbackHorizonHours: number,
): string {
  if (metrics === undefined) {
    return "Kennzahlen werden geladen...";
  }

  const parts: string[] = [];
  if (run.trigger_source === "prediction_refresh") {
    parts.push("Prediction-Refresh");
  }
  if (metrics === null) {
    parts.push(`Ziel ${fallbackHorizonHours}h`);
    return parts.join(" | ");
  }

  const desiredTargetHours =
    metrics.desiredPredictionHours ??
    metrics.desiredOptimizationHours ??
    metrics.effectivePredictionHours ??
    metrics.effectiveOptimizationHours ??
    fallbackHorizonHours;
  parts.push(`Ziel ${Math.max(1, Math.round(desiredTargetHours))}h`);

  if (metrics.horizonHours !== null) {
    parts.push(`Effektiv ${metrics.horizonHours.toFixed(1)}h`);
  }

  const effectiveConfiguredHours =
    metrics.effectivePredictionHours ??
    metrics.effectiveOptimizationHours;
  if (
    effectiveConfiguredHours !== null &&
    Math.max(1, Math.round(effectiveConfiguredHours)) !== Math.max(1, Math.round(desiredTargetHours))
  ) {
    parts.push(`Cap ${Math.max(1, Math.round(effectiveConfiguredHours))}h`);
  }

  const historicHours = metrics.desiredHistoricHours ?? metrics.effectiveHistoricHours;
  if (historicHours !== null) {
    parts.push(`Hist ${Math.max(1, Math.round(historicHours))}h`);
  }
  if (metrics.pointCount !== null) {
    parts.push(`${metrics.pointCount} Punkte`);
  }
  if (metrics.minPriceCt !== null && metrics.maxPriceCt !== null) {
    parts.push(`${metrics.minPriceCt.toFixed(2)}-${metrics.maxPriceCt.toFixed(2)} ct/kWh`);
  }
  return parts.join(" | ");
}

function runStatusChipClass(statusValue: string): string {
  const status = statusValue.toLowerCase();
  if (status === "success" || status === "ok") {
    return "chip-ok";
  }
  if (status === "running") {
    return "chip-warning";
  }
  if (status === "partial") {
    return "chip-warning";
  }
  return "chip-danger";
}

function runSourceLabel(triggerSource: string): string {
  if (triggerSource === "force_run") {
    return "Force";
  }
  if (triggerSource === "automatic") {
    return "Auto";
  }
  if (triggerSource === "prediction_refresh") {
    return "Prediction";
  }
  return triggerSource;
}

function formatSignedKw(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "-";
  }
  const abs = Math.abs(value);
  const formatted = abs >= 10 ? abs.toFixed(2) : abs.toFixed(3);
  const compact = formatted.replace(/\.?0+$/, "");
  if (value > 0) {
    return `+${compact}`;
  }
  if (value < 0) {
    return `-${compact}`;
  }
  return "0";
}

function outputSignalStatusChipClass(statusValue: string): string {
  const status = statusValue.toLowerCase();
  if (status === "ok") {
    return "chip-ok";
  }
  if (status === "missing_max_power") {
    return "chip-warning";
  }
  return "chip-neutral";
}

function shouldRetryRunMetricsLoad(
  run: EosRunSummary | undefined,
  metrics: RunPredictionMetrics | null | undefined,
): boolean {
  if (metrics !== null) {
    return false;
  }
  if (!run) {
    return false;
  }
  if (run.trigger_source === "prediction_refresh") {
    return false;
  }
  if (run.status === "running") {
    return false;
  }
  return true;
}

function buildRunHints(run: EosRunDetail | null, plan: EosRunPlan | null, solution: EosRunSolution | null): string[] {
  if (!run) {
    return [];
  }

  const hints: string[] = [];
  const errorText = (run.error_text ?? "").toLowerCase();
  const hasPlan = plan?.payload_json !== null && plan?.payload_json !== undefined;
  const hasSolution = solution?.payload_json !== null && solution?.payload_json !== undefined;

  if (run.trigger_source === "automatic") {
    hints.push("Automatische Runs werden erkannt, wenn EOS `energy-management.last_run_datetime` verändert.");
  } else if (run.trigger_source === "force_run") {
    hints.push("Force-Run setzt das EMS-Intervall kurzzeitig auf 1s (`pulse_then_legacy`) und wartet auf einen neuen EOS-Lauf.");
  } else if (run.trigger_source === "prediction_refresh") {
    hints.push("Prediction-Refresh aktualisiert nur Vorhersagedaten (PV/Preis/Load) und erzeugt absichtlich keinen Plan/Solution.");
  }

  if (run.status === "partial" && !hasPlan && run.trigger_source !== "prediction_refresh") {
    hints.push("Für diesen Run wurde kein Plan geliefert. Prüfe, ob EOS in `OPTIMIZATION` läuft und ob Vorhersagen vollständig sind.");
  }
  if (run.status === "partial" && !hasSolution && run.trigger_source !== "prediction_refresh") {
    hints.push("Für diesen Run wurde keine Solution geliefert.");
  }

  if (errorText.includes("did you configure automatic optimization")) {
    hints.push("EOS meldet fehlende automatische Optimierung. `ems.mode=OPTIMIZATION` und gültige Prediction-Daten prüfen.");
  }
  if (errorText.includes("unsupported fill method: linear")) {
    hints.push("EOS konnte einen Prediction-Key nicht numerisch interpolieren (`Unsupported fill method: linear`). Bei PVForecastImport müssen `pvforecast_ac_power` und `pvforecast_dc_power` numerische Arrays sein.");
  }
  if (errorText.includes("provider pvforecastakkudoktor fails on update")) {
    hints.push("PVForecastAkkudoktor konnte nicht aktualisieren. Prüfe Plane-Parameter (insb. `surface_tilt > 0`) oder nutze PVForecastImport.");
  }
  if (errorText.includes("legacy optimize fallback failed")) {
    hints.push("Fallback wurde versucht, konnte aber nicht abgeschlossen werden. Siehe `Fehlertext` unten.");
  }

  return hints;
}

type RunPipelineStep = {
  label: string;
  ok: boolean;
};

function buildRunPipelineSteps(
  run: EosRunDetail | null,
  plan: EosRunPlan | null,
  solution: EosRunSolution | null,
): RunPipelineStep[] {
  if (!run) {
    return [];
  }
  const summary = run.artifact_summary ?? {};
  const hasPlan = Boolean(plan?.payload_json) || Number(summary.plan ?? 0) > 0;
  const hasSolution = Boolean(solution?.payload_json) || Number(summary.solution ?? 0) > 0;

  if (run.trigger_source === "prediction_refresh") {
    return [
      { label: "1) Prediction-Refresh ausgelöst", ok: Number(summary.prediction_refresh ?? 0) > 0 },
      { label: "2) Prediction Keys gelesen", ok: Number(summary.prediction_keys ?? 0) > 0 },
      { label: "3) Prediction Serien gelesen", ok: Number(summary.prediction_series ?? 0) > 0 },
      { label: "4) Plan/Solution (nicht Teil dieses Run-Typs)", ok: true },
    ];
  }

  return [
    { label: "1) EOS Health erfasst", ok: Number(summary.health ?? 0) > 0 },
    { label: "2) Prediction Keys gelesen", ok: Number(summary.prediction_keys ?? 0) > 0 },
    { label: "3) Prediction Serien gelesen", ok: Number(summary.prediction_series ?? 0) > 0 },
    { label: "4) Plan erzeugt", ok: hasPlan },
    { label: "5) Solution erzeugt", ok: hasSolution },
  ];
}

function prettyJson(value: unknown): string {
  if (value === null || value === undefined) {
    return "-";
  }
  try {
    return JSON.stringify(convertJsonToUiUnits(value), null, 2);
  } catch {
    return String(value);
  }
}

function convertJsonToUiUnits(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map((item) => convertJsonToUiUnits(item));
  }
  if (value !== null && typeof value === "object") {
    const source = value as Record<string, unknown>;
    const converted: Record<string, unknown> = {};
    for (const [rawKey, rawChild] of Object.entries(source)) {
      const { key, factor } = remapJsonUnitKey(rawKey);
      const child = convertJsonToUiUnits(rawChild);
      converted[key] = factor === 1 ? child : scaleJsonNumericValue(child, factor);
    }
    return converted;
  }
  return value;
}

function remapJsonUnitKey(rawKey: string): { key: string; factor: number } {
  const lower = rawKey.toLowerCase();

  if (lower.includes("euro_pro_wh")) {
    return { key: rawKey.replace(/euro_pro_wh/gi, "ct_pro_kwh"), factor: 100000 };
  }
  if (lower.includes("eur_per_kwh")) {
    return { key: rawKey.replace(/eur_per_kwh/gi, "ct_per_kwh"), factor: 100 };
  }
  if (lower.includes("price") && lower.endsWith("_wh")) {
    return { key: rawKey.replace(/_wh$/i, "_ct_per_kwh"), factor: 100000 };
  }
  if (lower.includes("price") && lower.endsWith("_kwh")) {
    return { key: rawKey.replace(/_kwh$/i, "_ct_per_kwh"), factor: 100 };
  }
  if (lower.endsWith("_wh")) {
    return { key: rawKey.replace(/_wh$/i, "_kwh"), factor: 0.001 };
  }
  if (lower.endsWith("_w")) {
    return { key: rawKey.replace(/_w$/i, "_kw"), factor: 0.001 };
  }
  return { key: rawKey, factor: 1 };
}

function scaleJsonNumericValue(value: unknown, factor: number): unknown {
  if (typeof value === "number" && Number.isFinite(value)) {
    return roundForDisplay(value * factor);
  }
  if (Array.isArray(value)) {
    return value.map((item) =>
      typeof item === "number" && Number.isFinite(item) ? roundForDisplay(item * factor) : item,
    );
  }
  return value;
}

function roundForDisplay(value: number): number {
  return Number(value.toFixed(6));
}

function fieldStatusLabel(field: SetupField, draft: DraftState | undefined): string {
  if (draft?.dirty) {
    return "ungespeichert";
  }
  if (field.missing) {
    return "fehlend";
  }
  if (!field.valid) {
    return "ungültig";
  }
  return "gespeichert";
}

function fieldStatusClass(field: SetupField, draft: DraftState | undefined): string {
  if (draft?.dirty) {
    return "chip-warning";
  }
  if (field.missing || !field.valid) {
    return "chip-danger";
  }
  return "chip-ok";
}

export default function App() {
  const [fields, setFields] = useState<SetupField[]>([]);
  const [detailsOpenState, setDetailsOpenState] = useState<Record<string, boolean>>(() => loadDetailsOpenState());
  const [setupLayout, setSetupLayout] = useState<SetupLayout | null>(null);
  const [readiness, setReadiness] = useState<SetupReadiness | null>(null);
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [runtime, setRuntime] = useState<EosRuntime | null>(null);
  const [runs, setRuns] = useState<EosRunSummary[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const [selectedRunDetail, setSelectedRunDetail] = useState<EosRunDetail | null>(null);
  const [plan, setPlan] = useState<EosRunPlan | null>(null);
  const [solution, setSolution] = useState<EosRunSolution | null>(null);
  const [selectedRunPredictionSeries, setSelectedRunPredictionSeries] = useState<EosRunPredictionSeries | null>(null);
  const [outputCurrent, setOutputCurrent] = useState<EosOutputCurrentItem[]>([]);
  const [outputTimeline, setOutputTimeline] = useState<EosOutputTimelineItem[]>([]);
  const [outputSignalsBundle, setOutputSignalsBundle] = useState<EosOutputSignalsBundle | null>(null);
  const [plausibility, setPlausibility] = useState<EosRunPlausibility | null>(null);
  const [importText, setImportText] = useState("");
  const [importFeedback, setImportFeedback] = useState<string | null>(null);
  const [runtimeFeedback, setRuntimeFeedback] = useState<{ type: "success" | "error"; message: string } | null>(null);
  const [globalError, setGlobalError] = useState<string | null>(null);
  const [isForcingRun, setIsForcingRun] = useState(false);
  const [isRefreshingPrediction, setIsRefreshingPrediction] = useState<EosPredictionRefreshScope | null>(null);
  const [isSavingAutoRunPreset, setIsSavingAutoRunPreset] = useState(false);
  const [isSavingHorizon, setIsSavingHorizon] = useState(false);
  const [isMutatingSetupEntity, setIsMutatingSetupEntity] = useState(false);
  const [setupMutationFeedback, setSetupMutationFeedback] = useState<string | null>(null);
  const [horizonHoursDraft, setHorizonHoursDraft] = useState("48");
  const [horizonFeedback, setHorizonFeedback] = useState<{ type: "success" | "error"; message: string } | null>(null);
  const [runNowMs, setRunNowMs] = useState<number>(() => Date.now());
  const [runMetricsById, setRunMetricsById] = useState<Record<number, RunPredictionMetrics | null>>({});

  const [drafts, setDrafts] = useState<Record<string, DraftState>>({});
  const draftsRef = useRef(drafts);
  const fieldsRef = useRef(fields);
  const timersRef = useRef<Record<string, number>>({});
  const runMetricsLoadingRef = useRef<Set<number>>(new Set());
  const runMetricsRetryStateRef = useRef<Map<number, { attempts: number; lastAttemptMs: number }>>(new Map());

  useEffect(() => {
    draftsRef.current = drafts;
  }, [drafts]);

  useEffect(() => {
    fieldsRef.current = fields;
  }, [fields]);

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    try {
      window.localStorage.setItem(DETAILS_OPEN_STATE_STORAGE_KEY, JSON.stringify(detailsOpenState));
    } catch {
      // ignore localStorage write errors
    }
  }, [detailsOpenState]);

  const setDetailsOpen = useCallback((key: string, open: boolean) => {
    setDetailsOpenState((current) => {
      if (current[key] === open) {
        return current;
      }
      return {
        ...current,
        [key]: open,
      };
    });
  }, []);

  const loadSetup = useCallback(async () => {
    const [fieldsData, layoutData, readinessData, statusData] = await Promise.all([
      getSetupFields(),
      getSetupLayout(),
      getSetupReadiness(),
      getStatus(),
    ]);
    setFields(fieldsData);
    setSetupLayout(layoutData);
    setReadiness(readinessData);
    setStatus(statusData);
  }, []);

  const loadRunCenter = useCallback(async () => {
    const [runtimeData, runsData] = await Promise.all([
      getEosRuntime(),
      getEosRuns(),
    ]);
    setRuntime(runtimeData);
    setRuns(runsData);
    if (runsData.length === 0) {
      setSelectedRunId(null);
      setSelectedRunDetail(null);
      setPlan(null);
      setSolution(null);
      setSelectedRunPredictionSeries(null);
      setOutputCurrent([]);
      setOutputTimeline([]);
      setOutputSignalsBundle(null);
      setPlausibility(null);
      return;
    }
    if (RUN_CENTER_MINIMAL) {
      setSelectedRunId(runsData[0].id);
      return;
    }
    setSelectedRunId((current) => {
      if (current === null) {
        return runsData[0].id;
      }
      const exists = runsData.some((run) => run.id === current);
      return exists ? current : runsData[0].id;
    });
  }, []);

  const loadRunDetails = useCallback(async (runId: number) => {
    const [detailData, planData, solutionData, currentData, timelineData, plausibilityData, predictionSeriesData] = await Promise.all([
      getEosRunDetail(runId),
      getEosRunPlan(runId),
      getEosRunSolution(runId),
      getEosOutputsCurrent(runId),
      getEosOutputsTimeline({ runId }),
      getEosRunPlausibility(runId),
      getEosRunPredictionSeries(runId).catch(() => null),
    ]);
    setSelectedRunDetail(detailData);
    setPlan(planData);
    setSolution(solutionData);
    setSelectedRunPredictionSeries(predictionSeriesData);
    setOutputCurrent(currentData);
    setOutputTimeline(timelineData);
    setPlausibility(plausibilityData);
  }, []);

  const loadOutputSignals = useCallback(async () => {
    const signalsData = await getEosOutputSignals();
    setOutputSignalsBundle(signalsData);
  }, []);

  const refreshAll = useCallback(async () => {
    try {
      await Promise.all([loadSetup(), loadRunCenter()]);
      setGlobalError(null);
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : String(error));
    }
  }, [loadRunCenter, loadSetup]);

  useEffect(() => {
    void refreshAll();
    const interval = window.setInterval(() => {
      void refreshAll();
    }, REFRESH_ALL_POLL_MS);
    return () => {
      window.clearInterval(interval);
      for (const timer of Object.values(timersRef.current)) {
        window.clearTimeout(timer);
      }
    };
  }, [refreshAll]);

  useEffect(() => {
    if (selectedRunId === null) {
      return;
    }
    void loadRunDetails(selectedRunId).catch((error: unknown) => {
      setGlobalError(error instanceof Error ? error.message : String(error));
    });
  }, [selectedRunId, loadRunDetails]);

  useEffect(() => {
    if (selectedRunId === null) {
      return;
    }
    const interval = window.setInterval(() => {
      void loadRunDetails(selectedRunId).catch((error: unknown) => {
        setGlobalError(error instanceof Error ? error.message : String(error));
      });
    }, RUN_DETAILS_POLL_MS);
    return () => {
      window.clearInterval(interval);
    };
  }, [selectedRunId, loadRunDetails]);

  useEffect(() => {
    void loadOutputSignals().catch((error: unknown) => {
      setGlobalError(error instanceof Error ? error.message : String(error));
    });
    const interval = window.setInterval(() => {
      void loadOutputSignals().catch((error: unknown) => {
        setGlobalError(error instanceof Error ? error.message : String(error));
      });
    }, OUTPUT_SIGNALS_POLL_MS);
    return () => {
      window.clearInterval(interval);
    };
  }, [loadOutputSignals]);

  const saveField = useCallback(async (fieldId: string) => {
    const field = fieldsRef.current.find((item) => item.field_id === fieldId);
    const draft = draftsRef.current[fieldId];
    if (!field || !draft || !draft.dirty) {
      return;
    }

    setDrafts((current) => ({
      ...current,
      [fieldId]: {
        ...current[fieldId],
        saving: true,
      },
    }));

    try {
      const payloadValue = toSubmitValue(field, draft.value);
      const response = await patchSetupFields([
        {
          field_id: fieldId,
          value: payloadValue,
          source: "ui",
        },
      ]);
      const result = response.results[0];
      if (!result) {
        throw new Error("No result from backend");
      }

      setFields((current) => current.map((item) => (item.field_id === fieldId ? result.field : item)));
      setDrafts((current) => ({
        ...current,
        [fieldId]: {
          value: toInputString(result.field),
          dirty: result.status !== "saved",
          saving: false,
          error: result.error,
        },
      }));

      const [readinessData, layoutData] = await Promise.all([
        getSetupReadiness(),
        getSetupLayout(),
      ]);
      setReadiness(readinessData);
      setSetupLayout(layoutData);
      setGlobalError(null);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setDrafts((current) => ({
        ...current,
        [fieldId]: {
          ...(current[fieldId] ?? { value: "" }),
          dirty: true,
          saving: false,
          error: message,
        },
      }));
      setGlobalError(message);
    }
  }, []);

  const scheduleSave = useCallback(
    (fieldId: string) => {
      const existing = timersRef.current[fieldId];
      if (existing) {
        window.clearTimeout(existing);
      }
      timersRef.current[fieldId] = window.setTimeout(() => {
        void saveField(fieldId);
      }, AUTOSAVE_MS);
    },
    [saveField],
  );

  const flushSave = useCallback(
    (fieldId: string) => {
      const existing = timersRef.current[fieldId];
      if (existing) {
        window.clearTimeout(existing);
      }
      void saveField(fieldId);
    },
    [saveField],
  );

  const handleFieldChange = useCallback(
    (field: SetupField, nextValue: string) => {
      setDrafts((current) => ({
        ...current,
        [field.field_id]: {
          value: nextValue,
          dirty: true,
          saving: current[field.field_id]?.saving ?? false,
          error: null,
        },
      }));
      scheduleSave(field.field_id);
    },
    [scheduleSave],
  );

  const exportSetup = useCallback(async () => {
    try {
      const pkg = await getSetupExport();
      const blob = new Blob([JSON.stringify(pkg, null, 2)], { type: "application/json" });
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "eos-inputs-setup.v2.json";
      link.click();
      window.URL.revokeObjectURL(url);
      setImportFeedback("Export erstellt.");
      setGlobalError(null);
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : String(error));
    }
  }, []);

  const applyImport = useCallback(async () => {
    try {
      const parsed = JSON.parse(importText) as Record<string, unknown>;
      const result = await postSetupImport(parsed);
      setImportFeedback(
        result.warnings.length > 0
          ? `${result.message} | Warnungen: ${result.warnings.join(" | ")}`
          : result.message,
      );
      await refreshAll();
      setGlobalError(null);
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : String(error));
    }
  }, [importText, refreshAll]);

  const triggerForceRun = useCallback(async () => {
    setIsForcingRun(true);
    setRuntimeFeedback(null);
    try {
      const response = await forceEosRun();
      setRuntimeFeedback({ type: "success", message: `Force Run gestartet (#${response.run_id}).` });
      await loadRunCenter();
      setGlobalError(null);
    } catch (error) {
      setRuntimeFeedback({
        type: "error",
        message: error instanceof Error ? error.message : String(error),
      });
      setGlobalError(error instanceof Error ? error.message : String(error));
    } finally {
      setIsForcingRun(false);
    }
  }, [loadRunCenter]);

  const triggerPredictionRefresh = useCallback(
    async (scope: EosPredictionRefreshScope) => {
      setIsRefreshingPrediction(scope);
      setRuntimeFeedback(null);
      try {
        const response = await refreshEosPredictions(scope);
        const scopeLabel = scope === "pv" ? "PV" : scope === "prices" ? "Preis" : scope === "all" ? "All" : "Load";
        setRuntimeFeedback({
          type: "success",
          message: `${scopeLabel} Refresh gestartet (#${response.run_id}).`,
        });
        await loadRunCenter();
        setGlobalError(null);
      } catch (error) {
        setRuntimeFeedback({
          type: "error",
          message: error instanceof Error ? error.message : String(error),
        });
        setGlobalError(error instanceof Error ? error.message : String(error));
      } finally {
        setIsRefreshingPrediction(null);
      }
    },
    [loadRunCenter],
  );

  const handleSetupEntityMutation = useCallback(
    async (payload: SetupEntityMutatePayload) => {
      setIsMutatingSetupEntity(true);
      try {
        const result = await mutateSetupEntity(payload);
        setSetupLayout(result.layout);
        const warningText = result.warnings.length > 0 ? ` | Hinweise: ${result.warnings.join(" | ")}` : "";
        setSetupMutationFeedback(`${result.message}${warningText}`);
        await loadSetup();
        if (result.status !== "saved") {
          setGlobalError(result.message);
          return;
        }
        setGlobalError(null);
      } catch (error) {
        setGlobalError(error instanceof Error ? error.message : String(error));
      } finally {
        setIsMutatingSetupEntity(false);
      }
    },
    [loadSetup],
  );

  const predictionHoursField = useMemo(
    () => fields.find((field) => field.field_id === PREDICTION_HOURS_FIELD_ID) ?? null,
    [fields],
  );
  const predictionHistoricHoursField = useMemo(
    () => fields.find((field) => field.field_id === PREDICTION_HISTORIC_HOURS_FIELD_ID) ?? null,
    [fields],
  );
  const optimizationHoursField = useMemo(
    () => fields.find((field) => field.field_id === OPTIMIZATION_HOURS_FIELD_ID) ?? null,
    [fields],
  );
  const optimizationHorizonHoursField = useMemo(
    () => fields.find((field) => field.field_id === OPTIMIZATION_HORIZON_HOURS_FIELD_ID) ?? null,
    [fields],
  );

  const runtimePredictionHours = useMemo(() => {
    const payload = runtime?.config_payload;
    if (payload === null || payload === undefined || typeof payload !== "object" || Array.isArray(payload)) {
      return null;
    }
    const prediction = (payload as Record<string, unknown>).prediction;
    if (prediction === null || prediction === undefined || typeof prediction !== "object" || Array.isArray(prediction)) {
      return null;
    }
    return toFiniteNumber((prediction as Record<string, unknown>).hours);
  }, [runtime]);

  const runtimeOptimizationHours = useMemo(() => {
    const payload = runtime?.config_payload;
    if (payload === null || payload === undefined || typeof payload !== "object" || Array.isArray(payload)) {
      return null;
    }
    const optimization = (payload as Record<string, unknown>).optimization;
    if (
      optimization === null ||
      optimization === undefined ||
      typeof optimization !== "object" ||
      Array.isArray(optimization)
    ) {
      return null;
    }
    const optimizationRecord = optimization as Record<string, unknown>;
    return (
      toFiniteNumber(optimizationRecord.horizon_hours) ??
      toFiniteNumber(optimizationRecord.hours)
    );
  }, [runtime]);

  const configuredPredictionHours = useMemo(
    () => toFiniteNumber(predictionHoursField?.current_value),
    [predictionHoursField],
  );
  const configuredOptimizationHours = useMemo(
    () =>
      toFiniteNumber(optimizationHorizonHoursField?.current_value) ??
      toFiniteNumber(optimizationHoursField?.current_value),
    [optimizationHorizonHoursField, optimizationHoursField],
  );
  const configuredHorizonHours = useMemo(() => {
    const configuredHours = configuredPredictionHours ?? configuredOptimizationHours;
    if (configuredHours === null) {
      return null;
    }
    return Math.max(1, Math.round(configuredHours));
  }, [configuredOptimizationHours, configuredPredictionHours]);
  const safeHorizonCapHours = useMemo(() => {
    const capHours = toFiniteNumber(status?.config?.eos_visualize_safe_horizon_hours);
    if (capHours === null || capHours <= 0) {
      return null;
    }
    return Math.max(1, Math.round(capHours));
  }, [status]);
  const currentPredictionHours = useMemo(() => {
    const hours = runtimePredictionHours ?? configuredPredictionHours;
    if (hours === null) {
      return null;
    }
    const normalized = Math.max(1, Math.round(hours));
    return safeHorizonCapHours === null ? normalized : Math.min(normalized, safeHorizonCapHours);
  }, [configuredPredictionHours, runtimePredictionHours, safeHorizonCapHours]);
  const currentOptimizationHours = useMemo(() => {
    const hours = runtimeOptimizationHours ?? configuredOptimizationHours;
    if (hours === null) {
      return null;
    }
    const normalized = Math.max(1, Math.round(hours));
    return safeHorizonCapHours === null ? normalized : Math.min(normalized, safeHorizonCapHours);
  }, [configuredOptimizationHours, runtimeOptimizationHours, safeHorizonCapHours]);

  const hasRunningRuns = useMemo(() => runs.some((run) => run.status === "running"), [runs]);

  useEffect(() => {
    if (!hasRunningRuns) {
      setRunNowMs(Date.now());
      return;
    }
    const interval = window.setInterval(() => {
      setRunNowMs(Date.now());
    }, 1000);
    return () => window.clearInterval(interval);
  }, [hasRunningRuns]);

  useEffect(() => {
    if (isSavingHorizon) {
      return;
    }
    const fallbackHours = 48;
    const targetHours = Math.round(configuredHorizonHours ?? currentPredictionHours ?? currentOptimizationHours ?? fallbackHours);
    setHorizonHoursDraft(String(Math.max(1, targetHours)));
  }, [configuredHorizonHours, currentOptimizationHours, currentPredictionHours, isSavingHorizon]);

  const baseHorizonHours = useMemo(
    () => Math.max(1, Math.round(currentPredictionHours ?? currentOptimizationHours ?? 48)),
    [currentOptimizationHours, currentPredictionHours],
  );
  const hasConfiguredHorizonMismatch = useMemo(() => {
    if (
      configuredPredictionHours !== null &&
      Math.max(1, Math.round(configuredPredictionHours)) !== baseHorizonHours
    ) {
      return true;
    }
    if (
      configuredOptimizationHours !== null &&
      Math.max(1, Math.round(configuredOptimizationHours)) !== baseHorizonHours
    ) {
      return true;
    }
    return false;
  }, [baseHorizonHours, configuredOptimizationHours, configuredPredictionHours]);

  const configuredBaseHorizonHours = useMemo(
    () => Math.max(1, Math.round(configuredHorizonHours ?? baseHorizonHours)),
    [baseHorizonHours, configuredHorizonHours],
  );
  const selectedHorizonHours = useMemo(
    () => Math.max(1, Math.round(toFiniteNumber(horizonHoursDraft) ?? configuredBaseHorizonHours)),
    [configuredBaseHorizonHours, horizonHoursDraft],
  );
  const horizonOptions = useMemo(() => {
    const base = [48, 72, 96];
    const merged = new Set<number>([...base, selectedHorizonHours, configuredBaseHorizonHours]);
    return Array.from(merged)
      .map((value) => Math.max(1, Math.round(value)))
      .sort((left, right) => left - right);
  }, [configuredBaseHorizonHours, selectedHorizonHours]);
  const horizonControlDisabled =
    (predictionHoursField === null &&
      optimizationHoursField === null &&
      optimizationHorizonHoursField === null) ||
    isSavingHorizon ||
    isForcingRun ||
    isRefreshingPrediction !== null;

  const applyPredictionHorizon = useCallback(async (requestedHours?: number) => {
    if (
      predictionHoursField === null &&
      predictionHistoricHoursField === null &&
      optimizationHoursField === null &&
      optimizationHorizonHoursField === null
    ) {
      setHorizonFeedback({ type: "error", message: "Horizon-Felder sind aktuell nicht verfugbar." });
      return;
    }

    const requestedTargetHours = Math.max(1, Math.round(requestedHours ?? selectedHorizonHours));
    const targetHours = requestedTargetHours;
    const historicTargetHours = Math.max(
      targetHours,
      PREDICTION_HISTORIC_MAX_HOURS,
    );
    const updates: Array<{ field_id: string; value: unknown; source: "ui" }> = [];
    if (predictionHoursField !== null) {
      updates.push({
        field_id: PREDICTION_HOURS_FIELD_ID,
        value: targetHours,
        source: "ui",
      });
    }
    if (predictionHistoricHoursField !== null) {
      updates.push({
        field_id: PREDICTION_HISTORIC_HOURS_FIELD_ID,
        value: historicTargetHours,
        source: "ui",
      });
    }
    if (optimizationHorizonHoursField !== null) {
      updates.push({
        field_id: OPTIMIZATION_HORIZON_HOURS_FIELD_ID,
        value: targetHours,
        source: "ui",
      });
    } else if (optimizationHoursField !== null) {
      updates.push({
        field_id: OPTIMIZATION_HOURS_FIELD_ID,
        value: targetHours,
        source: "ui",
      });
    }

    if (updates.length === 0) {
      setHorizonFeedback({ type: "error", message: "Keine passenden Horizon-Felder verfugbar." });
      return;
    }

    setIsSavingHorizon(true);
    setHorizonFeedback(null);
    setHorizonHoursDraft(String(targetHours));
    try {
      const response = await patchSetupFields(updates);
      const rejected = response.results.filter((item) => item.status !== "saved");
      if (rejected.length > 0) {
        const details = rejected
          .map((item) => `${item.field_id}: ${item.error ?? "save failed"}`)
          .join(" | ");
        throw new Error(details);
      }
      await Promise.all([loadSetup(), loadRunCenter()]);
      const capMessage =
        safeHorizonCapHours !== null && requestedTargetHours > safeHorizonCapHours
          ? ` Safety-Cap bleibt bei ${safeHorizonCapHours}h.`
          : "";
      setHorizonFeedback({
        type: "success",
        message: `Vorschau-Horizont gespeichert: ${targetHours}h.${capMessage}`,
      });
    } catch (error) {
      setHorizonFeedback({
        type: "error",
        message: error instanceof Error ? error.message : String(error),
      });
    } finally {
      setIsSavingHorizon(false);
    }
  }, [
    loadRunCenter,
    loadSetup,
    optimizationHorizonHoursField,
    optimizationHoursField,
    predictionHoursField,
    predictionHistoricHoursField,
    safeHorizonCapHours,
    selectedHorizonHours,
  ]);

  const handleHorizonSelectChange = useCallback(
    (rawValue: string) => {
      const parsed = Math.max(1, Math.round(toFiniteNumber(rawValue) ?? configuredBaseHorizonHours));
      setHorizonHoursDraft(String(parsed));
      setHorizonFeedback(null);
      if (parsed === configuredBaseHorizonHours && !hasConfiguredHorizonMismatch) {
        return;
      }
      void applyPredictionHorizon(parsed);
    },
    [applyPredictionHorizon, configuredBaseHorizonHours, hasConfiguredHorizonMismatch],
  );

  const autoRunPresetValue = useMemo<EosAutoRunPreset>(() => {
    const preset = runtime?.collector.auto_run_preset;
    if (preset === "off" || preset === "15m" || preset === "30m" || preset === "60m") {
      return preset;
    }
    return "off";
  }, [runtime]);

  const runtimeBusy = useMemo(
    () => isForcingRun || isRefreshingPrediction !== null || Boolean(runtime?.collector.force_run_in_progress),
    [isForcingRun, isRefreshingPrediction, runtime],
  );

  const autoRunControlDisabled = isSavingAutoRunPreset;

  const applyAutoRunPreset = useCallback(
    async (preset: EosAutoRunPreset) => {
      setIsSavingAutoRunPreset(true);
      setRuntimeFeedback(null);
      try {
        const response = await putEosAutoRunPreset(preset);
        setRuntime(response.runtime);
        const presetLabel =
          AUTO_RUN_PRESET_OPTIONS.find((option) => option.value === response.preset)?.label ?? response.preset;
        setRuntimeFeedback({ type: "success", message: `Auto-Run gespeichert: ${presetLabel}.` });
        setGlobalError(null);
      } catch (error) {
        setRuntimeFeedback({
          type: "error",
          message: error instanceof Error ? error.message : String(error),
        });
        setGlobalError(error instanceof Error ? error.message : String(error));
      } finally {
        setIsSavingAutoRunPreset(false);
      }
    },
    [],
  );

  const handleAutoRunPresetChange = useCallback(
    (value: string) => {
      const nextPreset = value as EosAutoRunPreset;
      if (nextPreset === autoRunPresetValue) {
        return;
      }
      void applyAutoRunPreset(nextPreset);
    },
    [applyAutoRunPreset, autoRunPresetValue],
  );

  const runStats = useMemo(() => {
    const total = runs.length;
    const automatic = runs.filter((run) => run.trigger_source === "automatic").length;
    const forced = runs.filter((run) => run.trigger_source === "force_run").length;
    const prediction = runs.filter((run) => run.trigger_source === "prediction_refresh").length;
    const running = runs.filter((run) => run.status === "running").length;
    const success = runs.filter((run) => run.status === "success").length;
    const partial = runs.filter((run) => run.status === "partial").length;
    const failed = runs.filter((run) => run.status === "failed").length;
    return { total, automatic, forced, prediction, running, success, partial, failed };
  }, [runs]);

  const runMetricCandidateIds = useMemo(
    () => {
      const ids = runs.slice(0, 40).map((run) => run.id);
      if (selectedRunId !== null && !ids.includes(selectedRunId)) {
        ids.push(selectedRunId);
      }
      return ids;
    },
    [runs, selectedRunId],
  );

  const runSummaryById = useMemo(() => {
    const byId = new Map<number, EosRunSummary>();
    for (const run of runs) {
      byId.set(run.id, run);
    }
    return byId;
  }, [runs]);

  useEffect(() => {
    const existingRunIds = new Set(runs.map((run) => run.id));
    for (const runId of runMetricsRetryStateRef.current.keys()) {
      if (!existingRunIds.has(runId)) {
        runMetricsRetryStateRef.current.delete(runId);
      }
    }
  }, [runs]);

  useEffect(() => {
    const nowMs = Date.now();
    const missingIds = runMetricCandidateIds.filter(
      (runId) => {
        if (runMetricsLoadingRef.current.has(runId)) {
          return false;
        }
        const metrics = runMetricsById[runId];
        if (metrics === undefined) {
          return true;
        }
        if (!shouldRetryRunMetricsLoad(runSummaryById.get(runId), metrics)) {
          return false;
        }
        const retryState = runMetricsRetryStateRef.current.get(runId);
        if (!retryState) {
          return true;
        }
        if (retryState.attempts >= RUN_METRICS_RETRY_MAX_ATTEMPTS) {
          return false;
        }
        return nowMs - retryState.lastAttemptMs >= RUN_METRICS_RETRY_INTERVAL_MS;
      },
    );
    if (missingIds.length === 0) {
      return;
    }

    let cancelled = false;
    for (const runId of missingIds) {
      runMetricsLoadingRef.current.add(runId);
      const previousRetry = runMetricsRetryStateRef.current.get(runId);
      runMetricsRetryStateRef.current.set(runId, {
        attempts: (previousRetry?.attempts ?? 0) + 1,
        lastAttemptMs: nowMs,
      });
    }

    void (async () => {
      const loaded = await Promise.all(
        missingIds.map(async (runId) => {
          try {
            const [solutionResult, contextResult] = await Promise.allSettled([
              getEosRunSolution(runId),
              getEosRunContext(runId),
            ]);
            const solutionPayload =
              solutionResult.status === "fulfilled" ? solutionResult.value.payload_json : null;
            const predictionMetrics = extractRunPredictionMetrics(solutionPayload);
            const targetMetrics =
              contextResult.status === "fulfilled" ? extractRunTargetMetrics(contextResult.value) : null;
            return [runId, mergeRunMetrics(predictionMetrics, targetMetrics)] as const;
          } catch {
            return [runId, null] as const;
          }
        }),
      );
      if (!cancelled) {
        setRunMetricsById((current) => {
          const next: Record<number, RunPredictionMetrics | null> = { ...current };
          for (const [runId, metrics] of loaded) {
            next[runId] = metrics;
            if (metrics !== null) {
              runMetricsRetryStateRef.current.delete(runId);
            }
          }
          return next;
        });
      }
      for (const runId of missingIds) {
        runMetricsLoadingRef.current.delete(runId);
      }
    })();

    return () => {
      cancelled = true;
      for (const runId of missingIds) {
        runMetricsLoadingRef.current.delete(runId);
      }
    };
  }, [runMetricCandidateIds, runMetricsById, runSummaryById]);

  const runHints = useMemo(
    () => buildRunHints(selectedRunDetail, plan, solution),
    [selectedRunDetail, plan, solution],
  );
  const runPipelineSteps = useMemo(
    () => buildRunPipelineSteps(selectedRunDetail, plan, solution),
    [selectedRunDetail, plan, solution],
  );

  const visibleOutputCurrent = useMemo(() => outputCurrent, [outputCurrent]);
  const visibleOutputTimeline = useMemo(() => outputTimeline, [outputTimeline]);
  const effectiveOutputRunId = useMemo(() => {
    const runIds: number[] = [];
    for (const row of visibleOutputCurrent) {
      if (typeof row.run_id === "number") {
        runIds.push(row.run_id);
      }
    }
    if (runIds.length === 0) {
      for (const row of visibleOutputTimeline) {
        if (typeof row.run_id === "number") {
          runIds.push(row.run_id);
          break;
        }
      }
    }
    if (runIds.length === 0 && plausibility && typeof plausibility.run_id === "number") {
      runIds.push(plausibility.run_id);
    }
    if (runIds.length === 0) {
      return null;
    }
    return runIds[0];
  }, [visibleOutputCurrent, visibleOutputTimeline, plausibility]);
  const outputFallbackActive = useMemo(() => {
    if (selectedRunId === null || effectiveOutputRunId === null) {
      return false;
    }
    return selectedRunId !== effectiveOutputRunId;
  }, [selectedRunId, effectiveOutputRunId]);
  const visibleOutputSignals = useMemo<EosOutputSignalItem[]>(() => {
    if (!outputSignalsBundle?.signals) {
      return [];
    }
    return Object.values(outputSignalsBundle.signals).sort((left, right) =>
      left.signal_key.localeCompare(right.signal_key),
    );
  }, [outputSignalsBundle]);
  const outputSignalsCentralUrl = useMemo(() => {
    const path = outputSignalsBundle?.central_http_path ?? "/eos/get/outputs";
    if (typeof window === "undefined") {
      return path;
    }
    return `${window.location.origin}${path}`;
  }, [outputSignalsBundle]);
  const outputSignalsCentralJsonUrl = useMemo(() => {
    const path = outputSignalsBundle?.central_http_path ?? "/eos/get/outputs";
    if (typeof window === "undefined") {
      return `${path}?format=json`;
    }
    return `${window.location.origin}${path}?format=json`;
  }, [outputSignalsBundle]);
  const outputSignalsFetchSummary = useMemo(() => {
    if (visibleOutputSignals.length === 0) {
      return null;
    }
    let latestFetchTs: string | null = null;
    let latestFetchClient: string | null = null;
    let latestFetchMs = -1;
    let maxFetchCount = 0;

    for (const signal of visibleOutputSignals) {
      const fetchMs = toTimestampMs(signal.last_fetch_ts);
      if (fetchMs !== null && fetchMs >= latestFetchMs) {
        latestFetchMs = fetchMs;
        latestFetchTs = signal.last_fetch_ts;
        latestFetchClient = signal.last_fetch_client;
      }
      if (Number.isFinite(signal.fetch_count)) {
        maxFetchCount = Math.max(maxFetchCount, Math.trunc(signal.fetch_count));
      }
    }

    return {
      latestFetchTs,
      latestFetchClient,
      maxFetchCount,
    };
  }, [visibleOutputSignals]);

  return (
    <div className="app-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">EOS Webapp</p>
          <h1>Inputs & Setup | Run-Center | Outputs</h1>
        </div>
        <div className="status-strip">
          <span className={`chip ${readiness?.readiness_level === "ready" ? "chip-ok" : "chip-danger"}`}>
            readiness: {readiness?.readiness_level ?? "-"}
          </span>
          <span className="chip chip-neutral">blockers: {readiness?.blockers_count ?? "-"}</span>
          <span className="chip chip-neutral">EOS: {status?.eos?.health_ok ? "ok" : "offline"}</span>
        </div>
      </header>

      {globalError ? <div className="error-banner">{globalError}</div> : null}

      <div className="app-grid">
        <section className="pane">
          <h2>Inputs & Setup</h2>
          <p className="pane-copy">
            Pflichtkategorien sind als <strong>MUSS</strong> markiert, optionale als <strong>KANN</strong>. Änderungen werden automatisch gespeichert.
          </p>
          <p className="meta-text">
            Neue optionale Einträge werden per Klonen angelegt. Falls keine Quelle vorhanden ist, verwendet das Backend ein Template-Fallback.
            Basisobjekte (z. B. Plane #1) sind nicht löschbar.
          </p>

          <div className="panel">
            <h3>Kategorien</h3>
            <SetupCategoriesView
              layout={setupLayout}
              drafts={drafts}
              onChange={handleFieldChange}
              onBlur={flushSave}
              onMutateEntity={handleSetupEntityMutation}
              mutatingEntity={isMutatingSetupEntity}
              detailsOpenState={detailsOpenState}
              onDetailsToggle={setDetailsOpen}
            />
            {setupMutationFeedback ? <p className="meta-text">{setupMutationFeedback}</p> : null}
          </div>

          <div className="panel">
            <h3>Settings Import / Export (Inputs & Setup)</h3>
            <div className="actions-row">
              <button type="button" onClick={exportSetup}>Export</button>
              <button type="button" className="secondary" onClick={applyImport}>Import anwenden</button>
            </div>
            <textarea
              value={importText}
              onChange={(event) => setImportText(event.target.value)}
              rows={7}
              placeholder='{"format":"eos-webapp.inputs-setup.v2","payload":{...}}'
            />
            {importFeedback ? <p className="meta-text">{importFeedback}</p> : null}
          </div>
        </section>

        <section className="pane">
          <h2>Run-Center</h2>
          {!RUN_CENTER_MINIMAL ? (
            <p className="pane-copy">Runtime, Run-Typen, Historie und Fehleranalyse in Anwendersprache.</p>
          ) : null}

          <div className="panel panel-highlight">
            <h3>Vorschau-Horizont</h3>
            <div className="run-horizon-row">
              <select
                value={horizonHoursDraft}
                onChange={(event) => handleHorizonSelectChange(event.target.value)}
                disabled={horizonControlDisabled}
              >
                {horizonOptions.map((hours) => (
                  <option key={hours} value={String(hours)}>
                    {hours}h
                  </option>
                ))}
              </select>
            </div>
            {horizonFeedback ? (
              <p className={horizonFeedback.type === "error" ? "field-error" : "meta-text"}>
                {horizonFeedback.message}
              </p>
            ) : null}
          </div>

          {!RUN_CENTER_MINIMAL ? (
            <>
              <div className="panel">
                <h3>Run-Steuerung & Runtime</h3>
                <div className="run-control-block">
                  <label className="meta-text" htmlFor="auto-run-preset">Auto-Run</label>
                  <select
                    id="auto-run-preset"
                    value={autoRunPresetValue}
                    onChange={(event) => handleAutoRunPresetChange(event.target.value)}
                    disabled={autoRunControlDisabled}
                  >
                    {AUTO_RUN_PRESET_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="chip-row">
                  <span className={`chip ${runtime?.health_ok ? "chip-ok" : "chip-danger"}`}>
                    EOS: {runtime?.health_ok ? "ok" : "offline"}
                  </span>
                  <span className={`chip ${autoRunPresetValue === "off" ? "chip-neutral" : "chip-ok"}`}>
                    Auto: {AUTO_RUN_PRESET_OPTIONS.find((option) => option.value === autoRunPresetValue)?.label ?? autoRunPresetValue}
                  </span>
                  <span className={`chip ${runtimeBusy ? "chip-warning" : "chip-ok"}`}>
                    Busy: {runtimeBusy ? "running" : "idle"}
                  </span>
                </div>
                <div className="run-actions-grid">
                  <button
                    type="button"
                    className="run-action-button"
                    onClick={triggerForceRun}
                    disabled={isForcingRun || isRefreshingPrediction !== null}
                  >
                    <span className="run-action-title">{isForcingRun ? "Force Run..." : "Force Run"}</span>
                    <span className="run-action-subline">voller Lauf</span>
                  </button>
                  <button
                    type="button"
                    className="run-action-button secondary"
                    onClick={() => void triggerPredictionRefresh("pv")}
                    disabled={isRefreshingPrediction !== null || isForcingRun}
                  >
                    <span className="run-action-title">{isRefreshingPrediction === "pv" ? "PV Refresh..." : "PV Refresh"}</span>
                    <span className="run-action-subline">nur PV</span>
                  </button>
                  <button
                    type="button"
                    className="run-action-button secondary"
                    onClick={() => void triggerPredictionRefresh("prices")}
                    disabled={isRefreshingPrediction !== null || isForcingRun}
                  >
                    <span className="run-action-title">{isRefreshingPrediction === "prices" ? "Preis Refresh..." : "Preis Refresh"}</span>
                    <span className="run-action-subline">nur Preise</span>
                  </button>
                  <button
                    type="button"
                    className="run-action-button secondary"
                    onClick={() => void triggerPredictionRefresh("all")}
                    disabled={isRefreshingPrediction !== null || isForcingRun}
                  >
                    <span className="run-action-title">{isRefreshingPrediction === "all" ? "All Refresh..." : "All Refresh"}</span>
                    <span className="run-action-subline">alle Prognosen</span>
                  </button>
                </div>
                {runtimeFeedback ? (
                  <p className={runtimeFeedback.type === "error" ? "field-error" : "meta-text"}>
                    {runtimeFeedback.message}
                  </p>
                ) : null}
              </div>

          <div className="panel">
            <h3>Run-Übersicht</h3>
            <div className="chip-row">
              <span className="chip chip-neutral">gesamt: {runStats.total}</span>
              <span className="chip chip-neutral">auto: {runStats.automatic}</span>
              <span className="chip chip-neutral">force: {runStats.forced}</span>
              <span className="chip chip-neutral">prediction: {runStats.prediction}</span>
              <span className={`chip ${runStats.running > 0 ? "chip-warning" : "chip-neutral"}`}>running: {runStats.running}</span>
              <span className="chip chip-ok">success: {runStats.success}</span>
              <span className="chip chip-warning">partial: {runStats.partial}</span>
              <span className="chip chip-danger">failed: {runStats.failed}</span>
            </div>
          </div>

          <div className="panel">
            <h3>Run-Historie</h3>
            <div className="run-list">
              {runs.length === 0 ? <p>Keine Runs vorhanden.</p> : null}
              {runs.map((run) => {
                const metrics = runMetricsById[run.id];
                const metricLabel = formatRunMetricsLabel(run, metrics, baseHorizonHours);
                return (
                  <button
                    key={run.id}
                    type="button"
                    className={`run-item${selectedRunId === run.id ? " run-item-active" : ""}${run.status === "running" ? " run-item-running" : ""}`}
                    onClick={() => setSelectedRunId(run.id)}
                  >
                    <span>#{run.id}</span>
                    <span>{runSourceLabel(run.trigger_source)}</span>
                    <span className={`chip ${runStatusChipClass(run.status)}`}>{run.status}</span>
                    <span className="run-item-main">
                      <span>{formatTimestamp(run.started_at)}</span>
                      <span className="run-item-metric">{metricLabel}</span>
                    </span>
                    <span className="run-item-duration">{formatDuration(run.started_at, run.finished_at, runNowMs)}</span>
                  </button>
                );
              })}
            </div>
          </div>

          <div className="panel">
            <h3>Analyse des ausgewählten Runs</h3>
            {selectedRunDetail === null ? (
              <p>Kein Run ausgewählt.</p>
            ) : (
              <>
                <ul className="plain-list">
                  <li>Run ID: <strong>{selectedRunDetail.id}</strong></li>
                  <li>Quelle: <strong>{runSourceLabel(selectedRunDetail.trigger_source)}</strong></li>
                  <li>Mode: <strong>{selectedRunDetail.run_mode}</strong></li>
                  <li>Status: <span className={`chip ${runStatusChipClass(selectedRunDetail.status)}`}>{selectedRunDetail.status}</span></li>
                  <li>Dauer: <strong>{formatDuration(selectedRunDetail.started_at, selectedRunDetail.finished_at, runNowMs)}</strong></li>
                  <li>EOS last_run_datetime: <strong>{formatTimestamp(selectedRunDetail.eos_last_run_datetime)}</strong></li>
                </ul>
                <div className="meta-text">
                  Artefakte: <code>{JSON.stringify(selectedRunDetail.artifact_summary)}</code>
                </div>
                {runPipelineSteps.length > 0 ? (
                  <ul className="plain-list">
                    {runPipelineSteps.map((step) => (
                      <li key={step.label}>
                        <span className={`chip ${step.ok ? "chip-ok" : "chip-warning"}`}>
                          {step.ok ? "ok" : "offen"}
                        </span>{" "}
                        {step.label}
                      </li>
                    ))}
                  </ul>
                ) : null}
                {runHints.length > 0 ? (
                  <ul className="plain-list">
                    {runHints.map((hint) => (
                      <li key={hint}>{hint}</li>
                    ))}
                  </ul>
                ) : null}
                {selectedRunDetail.error_text ? (
                  <div className="field-error">Fehlertext: {selectedRunDetail.error_text}</div>
                ) : null}
              </>
            )}
          </div>
            </>
          ) : null}
        </section>

        <section className="pane">
          <h2>Outputs</h2>
          <p className="pane-copy">
            Konkrete Ausführung aus EOS-Plan: aktive Entscheidungen, Zustandswechsel und HTTP-Pull-Signale.
          </p>
          {outputFallbackActive ? (
            <p className="meta-text">
              Hinweis: Fur den ausgewahlten Run #{selectedRunId} sind keine verwertbaren Entscheidungen vorhanden.
              Die Output-Anzeige verwendet daher Run #{effectiveOutputRunId}.
            </p>
          ) : null}

          <OutputChartsPanel
            runId={selectedRunId}
            timeline={visibleOutputTimeline}
            current={visibleOutputCurrent}
            configPayload={runtime?.config_payload ?? null}
            solutionPayload={solution?.payload_json ?? null}
            predictionSeries={selectedRunPredictionSeries}
          />

          <details
            className="panel panel-collapsible"
            open={detailsOpenState["outputs.current"] ?? true}
            onToggle={(event) =>
              setDetailsOpen("outputs.current", (event.currentTarget as HTMLDetailsElement).open)
            }
          >
            <summary className="panel-summary collapse-summary"><strong>Aktive Entscheidungen jetzt</strong></summary>
            {visibleOutputCurrent.length === 0 ? (
              <p>Keine aktive Entscheidung verfügbar.</p>
            ) : (
              <div className="data-table outputs-current-table">
                <div className="table-head">
                  <span>Resource</span>
                  <span>Mode</span>
                  <span>Faktor</span>
                  <span>Soll-Leistung (kW)</span>
                  <span>Effective</span>
                  <span>Safety</span>
                </div>
                {visibleOutputCurrent.map((item) => (
                  <div key={`${item.resource_id}-${item.effective_at ?? "na"}`} className="table-row">
                    <span>{item.resource_id}</span>
                    <span>{item.operation_mode_id ?? "-"}</span>
                    <span>{item.operation_mode_factor ?? "-"}</span>
                    <span>{formatSignedKw(item.requested_power_kw)}</span>
                    <span>{formatTimestamp(item.effective_at)}</span>
                    <span className={`chip ${item.safety_status === "ok" ? "chip-ok" : "chip-warning"}`}>
                      {item.safety_status}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </details>

          <details
            className="panel panel-collapsible"
            open={detailsOpenState["outputs.timeline"] ?? true}
            onToggle={(event) =>
              setDetailsOpen("outputs.timeline", (event.currentTarget as HTMLDetailsElement).open)
            }
          >
            <summary className="panel-summary collapse-summary"><strong>Nächste Zustandswechsel</strong></summary>
            {visibleOutputTimeline.length === 0 ? (
              <p>Keine Timeline-Einträge.</p>
            ) : (
              <div className="data-table">
                <div className="table-head">
                  <span>Zeit</span>
                  <span>Resource</span>
                  <span>Typ</span>
                  <span>Mode</span>
                  <span>Faktor</span>
                </div>
                {visibleOutputTimeline.slice(0, 20).map((item) => (
                  <div key={`${item.instruction_id}-${item.execution_time ?? "na"}`} className="table-row">
                    <span>{formatTimestamp(item.execution_time ?? item.starts_at)}</span>
                    <span>{item.resource_id}</span>
                    <span>{item.instruction_type}</span>
                    <span>{item.operation_mode_id ?? "-"}</span>
                    <span>{item.operation_mode_factor ?? "-"}</span>
                  </div>
                ))}
              </div>
            )}
          </details>

          <details
            className="panel panel-collapsible"
            open={detailsOpenState["outputs.signals"] ?? true}
            onToggle={(event) =>
              setDetailsOpen("outputs.signals", (event.currentTarget as HTMLDetailsElement).open)
            }
          >
            <summary className="panel-summary collapse-summary"><strong>Output-Signale (HTTP Pull)</strong></summary>
            <p className="meta-text">
              Loxone URL: <code>{outputSignalsCentralUrl}</code>
            </p>
            <p className="meta-text">
              Stand: <strong>{formatTimestamp(outputSignalsBundle?.fetched_at ?? null)}</strong>
            </p>
            <p className="meta-text">
              Letzter Abruf (URL): <strong>{formatTimestamp(outputSignalsFetchSummary?.latestFetchTs ?? null)}</strong>
              {" "} | Letzte Quelle: <strong>{outputSignalsFetchSummary?.latestFetchClient ?? "-"}</strong>
              {" "} | Abrufe: <strong>{outputSignalsFetchSummary?.maxFetchCount ?? 0}</strong>
            </p>
            <p className="meta-text">
              JSON Debug: <code>{outputSignalsCentralJsonUrl}</code>
            </p>
            {visibleOutputSignals.length === 0 ? (
              <p>Keine abrufbaren Output-Signale.</p>
            ) : (
              <div className="output-signal-grid">
                {visibleOutputSignals.map((signal) => (
                  <div key={signal.signal_key} className="output-signal-card">
                    <div className="output-signal-head">
                      <strong>{signal.signal_key}</strong>
                      <span className={`chip ${outputSignalStatusChipClass(signal.status)}`}>{signal.status}</span>
                    </div>
                    <div className="meta-text">
                      Label: <strong>{signal.label}</strong>
                    </div>
                    <div className="output-signal-value">{formatSignedKw(signal.requested_power_kw)} kW</div>
                    <div className="meta-text">
                      JSON-Pfad: <code>{signal.json_path_value}</code>
                    </div>
                    <div className="meta-text">
                      Loxone-Befehlskennung: <code>{signal.signal_key}:\v</code>
                    </div>
                    <div className="meta-text">
                      Resource: <strong>{signal.resource_id ?? "-"}</strong> | Run:{" "}
                      <strong>{signal.run_id === null ? "-" : `#${signal.run_id}`}</strong>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </details>

          <details
            className="panel panel-collapsible"
            open={detailsOpenState["outputs.plausibility"] ?? true}
            onToggle={(event) =>
              setDetailsOpen("outputs.plausibility", (event.currentTarget as HTMLDetailsElement).open)
            }
          >
            <summary className="panel-summary collapse-summary"><strong>Plausibilität</strong></summary>
            {plausibility === null ? (
              <p>Keine Plausibilitätsdaten.</p>
            ) : (
              <>
                <p className="meta-text">
                  Run {plausibility.run_id} | Status:{" "}
                  <span className={`chip ${plausibility.status === "ok" ? "chip-ok" : plausibility.status === "warn" ? "chip-warning" : "chip-danger"}`}>
                    {plausibility.status}
                  </span>
                </p>
                <ul className="plain-list">
                  {plausibility.findings.map((finding) => (
                    <li key={`${finding.code}-${finding.message}`}>
                      <span className={`chip ${finding.level === "ok" ? "chip-ok" : finding.level === "warn" ? "chip-warning" : "chip-danger"}`}>
                        {finding.level}
                      </span>{" "}
                      <strong>{finding.code}</strong>: {finding.message}
                    </li>
                  ))}
                </ul>
              </>
            )}
          </details>

          <div className="panel">
            <details
              open={detailsOpenState["outputs.plan_json"] ?? false}
              onToggle={(event) =>
                setDetailsOpen("outputs.plan_json", (event.currentTarget as HTMLDetailsElement).open)
              }
            >
              <summary className="collapse-summary"><strong>Plan (JSON)</strong></summary>
              <pre>{prettyJson(plan?.payload_json ?? null)}</pre>
            </details>
            <details
              open={detailsOpenState["outputs.solution_json"] ?? false}
              onToggle={(event) =>
                setDetailsOpen("outputs.solution_json", (event.currentTarget as HTMLDetailsElement).open)
              }
            >
              <summary className="collapse-summary"><strong>Solution (JSON)</strong></summary>
              <pre>{prettyJson(solution?.payload_json ?? null)}</pre>
            </details>
          </div>
        </section>
      </div>
    </div>
  );
}

type FieldListProps = {
  fields: SetupField[];
  drafts: Record<string, DraftState>;
  onChange: (field: SetupField, value: string) => void;
  onBlur: (fieldId: string) => void;
  detailsOpenState: Record<string, boolean>;
  onDetailsToggle: (key: string, open: boolean) => void;
};

type SetupCategoriesViewProps = {
  layout: SetupLayout | null;
  drafts: Record<string, DraftState>;
  onChange: (field: SetupField, value: string) => void;
  onBlur: (fieldId: string) => void;
  onMutateEntity: (payload: SetupEntityMutatePayload) => void;
  mutatingEntity: boolean;
  detailsOpenState: Record<string, boolean>;
  onDetailsToggle: (key: string, open: boolean) => void;
};

function SetupCategoriesView({
  layout,
  drafts,
  onChange,
  onBlur,
  onMutateEntity,
  mutatingEntity,
  detailsOpenState,
  onDetailsToggle,
}: SetupCategoriesViewProps) {
  const [categoryCloneSelection, setCategoryCloneSelection] = useState<Record<string, string>>({});
  const [windowCloneSelection, setWindowCloneSelection] = useState<Record<string, string>>({});
  const categories = layout?.categories ?? [];

  if (!layout) {
    return <p>Lade Kategorien...</p>;
  }

  if (categories.length === 0) {
    return <p>Keine Kategorien.</p>;
  }

  return (
    <div className="setup-category-list">
      {categories.map((category) => {
        const addEntityType = category.add_entity_type;
        const topLevelItems = category.items.filter((item) => item.parent_item_key === null);
        const repeatableCandidates = topLevelItems.filter(
          (item) => item.entity_type === addEntityType,
        );
        const categoryStatusClass = category.invalid_required_count > 0 ? "chip-danger" : "chip-ok";
        const canAddInCategory =
          category.repeatable &&
          addEntityType !== null &&
          (category.item_limit === null || repeatableCandidates.length < category.item_limit);

        const selectedCategoryClone =
          categoryCloneSelection[category.category_id] ??
          (repeatableCandidates[0]?.item_key ?? "__template__");

        return (
          <details
            key={category.category_id}
            className="setup-category"
            open={detailsOpenState[`setup.category.${category.category_id}`] ?? category.default_open}
            onToggle={(event) => {
              const detailsElement = event.currentTarget as HTMLDetailsElement;
              onDetailsToggle(`setup.category.${category.category_id}`, detailsElement.open);
            }}
          >
            <summary className="setup-category-summary collapse-summary">
              <span className="setup-category-title">{category.title}</span>
              <span className="chip-row">
                <span className={`chip ${category.requirement_label === "KANN" ? "chip-neutral" : "chip-warning"}`}>
                  {category.requirement_label}
                </span>
                <span className={`chip ${categoryStatusClass}`}>
                  {category.invalid_required_count > 0
                    ? `${category.invalid_required_count} Pflichtfelder ungultig`
                    : "Pflichtfelder ok"}
                </span>
              </span>
            </summary>

            {category.description ? <p className="meta-text">{category.description}</p> : null}

            {category.repeatable && addEntityType ? (
              <div className="setup-category-actions">
                {repeatableCandidates.length > 1 ? (
                  <select
                    value={selectedCategoryClone}
                    onChange={(event) =>
                      setCategoryCloneSelection((current) => ({
                        ...current,
                        [category.category_id]: event.target.value,
                      }))
                    }
                    disabled={mutatingEntity || !canAddInCategory}
                  >
                    {repeatableCandidates.map((candidate) => (
                      <option key={candidate.item_key} value={candidate.item_key}>
                        Klonen: {candidate.label}
                      </option>
                    ))}
                    <option value="__template__">Template-Fallback</option>
                  </select>
                ) : null}
                <button
                  type="button"
                  className="secondary"
                  disabled={mutatingEntity || !canAddInCategory}
                  onClick={() => {
                    const payload: SetupEntityMutatePayload = {
                      action: "add",
                      entity_type: addEntityType,
                    };
                    if (repeatableCandidates.length === 1) {
                      payload.clone_from_item_key = repeatableCandidates[0].item_key;
                    } else if (repeatableCandidates.length > 1 && selectedCategoryClone !== "__template__") {
                      payload.clone_from_item_key = selectedCategoryClone;
                    }
                    onMutateEntity(payload);
                  }}
                >
                  + Hinzufugen
                </button>
                {category.item_limit !== null ? (
                  <span className="meta-text">
                    Kapazitat: {repeatableCandidates.length}/{category.item_limit}
                  </span>
                ) : null}
              </div>
            ) : null}

            {topLevelItems.length === 0 ? (
              <p className="meta-text">Keine Eintrage vorhanden.</p>
            ) : (
              <div className="setup-item-list">
                {topLevelItems.map((item) => {
                  const windowItems = category.items.filter(
                    (candidate) =>
                      candidate.parent_item_key === item.item_key &&
                      candidate.entity_type === "home_appliance_window",
                  );
                  const baseFields = item.fields.filter((field) => !field.advanced);
                  const advancedFields = item.fields.filter((field) => field.advanced);
                  const selectedWindowClone =
                    windowCloneSelection[item.item_key] ?? (windowItems[0]?.item_key ?? "__template__");
                  const canAddWindow = windowItems.length < 96;
                  const itemEntityType = item.entity_type;

                  return (
                    <div key={item.item_key} className="setup-item-card">
                      <div className="panel-head">
                        <h4>{item.label}</h4>
                        <div className="chip-row">
                          <span className={`chip ${item.base_object ? "chip-neutral" : "chip-warning"}`}>
                            {item.base_object ? "Basis" : "Optional"}
                          </span>
                          <span className={`chip ${item.invalid_required_count > 0 ? "chip-danger" : "chip-ok"}`}>
                            {item.invalid_required_count > 0 ? "ungultig" : "ok"}
                          </span>
                          {item.deletable && itemEntityType ? (
                            <button
                              type="button"
                              className="secondary"
                              disabled={mutatingEntity}
                              onClick={() =>
                                onMutateEntity({
                                  action: "remove",
                                  entity_type: itemEntityType,
                                  item_key: item.item_key,
                                  parent_item_key: item.parent_item_key ?? undefined,
                                })
                              }
                            >
                              Loschen
                            </button>
                          ) : null}
                        </div>
                      </div>

                      {baseFields.length > 0 ? (
                        <FieldList
                          fields={baseFields}
                          drafts={drafts}
                          onChange={onChange}
                          onBlur={onBlur}
                          detailsOpenState={detailsOpenState}
                          onDetailsToggle={onDetailsToggle}
                        />
                      ) : null}

                      {advancedFields.length > 0 ? (
                        <details
                          className="setup-advanced"
                          open={detailsOpenState[`setup.item.${item.item_key}.advanced`] ?? false}
                          onToggle={(event) =>
                            onDetailsToggle(
                              `setup.item.${item.item_key}.advanced`,
                              (event.currentTarget as HTMLDetailsElement).open,
                            )
                          }
                        >
                          <summary className="collapse-summary">Advanced</summary>
                          <FieldList
                            fields={advancedFields}
                            drafts={drafts}
                            onChange={onChange}
                            onBlur={onBlur}
                            detailsOpenState={detailsOpenState}
                            onDetailsToggle={onDetailsToggle}
                          />
                        </details>
                      ) : null}

                      {item.entity_type === "home_appliance" ? (
                        <div className="setup-window-editor">
                          <div className="setup-window-head">
                            <strong>Zeitfenster</strong>
                            <div className="actions-inline">
                              {windowItems.length > 1 ? (
                                <select
                                  value={selectedWindowClone}
                                  onChange={(event) =>
                                    setWindowCloneSelection((current) => ({
                                      ...current,
                                      [item.item_key]: event.target.value,
                                    }))
                                  }
                                  disabled={mutatingEntity || !canAddWindow}
                                >
                                  {windowItems.map((windowItem) => (
                                    <option key={windowItem.item_key} value={windowItem.item_key}>
                                      Klonen: {windowItem.label}
                                    </option>
                                  ))}
                                  <option value="__template__">Template-Fallback</option>
                                </select>
                              ) : null}
                              <button
                                type="button"
                                className="secondary"
                                disabled={mutatingEntity || !canAddWindow}
                                onClick={() => {
                                  const payload: SetupEntityMutatePayload = {
                                    action: "add",
                                    entity_type: "home_appliance_window",
                                    parent_item_key: item.item_key,
                                  };
                                  if (windowItems.length === 1) {
                                    payload.clone_from_item_key = windowItems[0].item_key;
                                  } else if (windowItems.length > 1 && selectedWindowClone !== "__template__") {
                                    payload.clone_from_item_key = selectedWindowClone;
                                  }
                                  onMutateEntity(payload);
                                }}
                              >
                                + Zeitfenster
                              </button>
                            </div>
                          </div>

                          {windowItems.length === 0 ? (
                            <p className="meta-text">Keine Zeitfenster vorhanden.</p>
                          ) : (
                            <div className="data-table compact">
                              <div className="table-head">
                                <span>Fenster</span>
                                <span>Startzeit</span>
                                <span>Dauer</span>
                                <span>Status</span>
                                <span>Aktion</span>
                              </div>
                              {windowItems.map((windowItem) => {
                                const startField =
                                  windowItem.fields.find((field) => field.field_id.endsWith(".start_time")) ?? null;
                                const durationField =
                                  windowItem.fields.find((field) => field.field_id.endsWith(".duration_h")) ?? null;
                                const startDraft = startField ? drafts[startField.field_id] : undefined;
                                const durationDraft = durationField ? drafts[durationField.field_id] : undefined;
                                const isWindowInvalid = windowItem.invalid_required_count > 0;

                                return (
                                  <div key={windowItem.item_key} className="table-row">
                                    <span className="truncate">{windowItem.label}</span>
                                    <span>
                                      {startField ? (
                                        <FieldInput
                                          field={startField}
                                          value={startDraft ? startDraft.value : toInputString(startField)}
                                          onChange={onChange}
                                          onBlur={onBlur}
                                        />
                                      ) : (
                                        "-"
                                      )}
                                    </span>
                                    <span>
                                      {durationField ? (
                                        <FieldInput
                                          field={durationField}
                                          value={durationDraft ? durationDraft.value : toInputString(durationField)}
                                          onChange={onChange}
                                          onBlur={onBlur}
                                        />
                                      ) : (
                                        "-"
                                      )}
                                    </span>
                                    <span>
                                      <span className={`chip ${isWindowInvalid ? "chip-danger" : "chip-ok"}`}>
                                        {isWindowInvalid ? "ungultig" : "ok"}
                                      </span>
                                    </span>
                                    <span>
                                      <button
                                        type="button"
                                        className="secondary"
                                        disabled={mutatingEntity}
                                        onClick={() =>
                                          onMutateEntity({
                                            action: "remove",
                                            entity_type: "home_appliance_window",
                                            item_key: windowItem.item_key,
                                            parent_item_key: item.item_key,
                                          })
                                        }
                                      >
                                        Loschen
                                      </button>
                                    </span>
                                  </div>
                                );
                              })}
                            </div>
                          )}
                        </div>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            )}
          </details>
        );
      })}
    </div>
  );
}

function FieldList({
  fields,
  drafts,
  onChange,
  onBlur,
  detailsOpenState,
  onDetailsToggle,
}: FieldListProps) {
  if (fields.length === 0) {
    return <p>Keine Felder.</p>;
  }

  const providerField = fields.find((field) => field.field_id === "param.pvforecast.provider");
  const providerValue = providerField
    ? (drafts[providerField.field_id]?.value ?? toInputString(providerField))
    : "";

  return (
    <div className="field-list">
      {fields.map((field) => {
        const draft = drafts[field.field_id];
        const currentInput = draft ? draft.value : toInputString(field);
        const statusLabel = fieldStatusLabel(field, draft);
        const statusClass = fieldStatusClass(field, draft);
        const isCritical = field.required && (field.missing || !field.valid || Boolean(draft?.dirty));

        const azimuthWorkaroundHint =
          field.field_id === "param.pvforecast.planes.0.surface_azimuth" &&
          providerValue === "PVForecastAkkudoktor"
            ? "Akkudoktor-Kompatibilität aktiv: 180° (Süden) wird intern minimal auf 179.9° übertragen, um einen bekannten Provider-Edge-Case zu vermeiden."
            : null;
        const isFeedInTariffProviderField = field.field_id === "param.feedintariff.provider";
        const providerHelpDetailsKey = `setup.field.${field.field_id}.help`;

        return (
          <div key={field.field_id} className={`setup-field${isCritical ? " setup-field-critical" : ""}`}>
            <div className="field-head">
              <div>
                <strong>{field.label}</strong>
              </div>
              <div className="chip-row">
                <span className={`chip ${field.required ? "chip-danger" : "chip-neutral"}`}>
                  {field.required ? "Pflicht" : "Optional"}
                </span>
                <span className={`chip ${statusClass}`}>{statusLabel}</span>
                <span className={`chip ${field.http_override_active ? "chip-ok" : "chip-neutral"}`}>
                  HTTP {field.http_override_active ? "aktiv" : "inaktiv"}
                </span>
              </div>
            </div>

            <div className="field-control">
              <FieldInput field={field} value={currentInput} onChange={onChange} onBlur={onBlur} />
              {field.unit ? <span className="unit-tag">{field.unit}</span> : null}
            </div>
            {isFeedInTariffProviderField ? (
              <details
                className="field-help"
                open={detailsOpenState[providerHelpDetailsKey] ?? false}
                onToggle={(event) =>
                  onDetailsToggle(providerHelpDetailsKey, (event.currentTarget as HTMLDetailsElement).open)
                }
              >
                <summary className="collapse-summary">Was bedeutet dieser Provider?</summary>
                <div className="field-help-content">
                  <p>Hier stellst du ein, woher EOS den Einspeisepreis bekommt.</p>
                  <p>
                    <code>FeedInTariffFixed</code>: konstanter Einspeisetarif (fester Wert in ct/kWh).
                  </p>
                  <p>
                    <code>FeedInTariffImport</code>: Zeitreihe mit variablen Einspeisepreisen aus importierten Daten
                    (JSON/Datei), z. B. für Spot- oder Direktvermarktungsszenarien.
                  </p>
                  <p>
                    Wichtig: <code>Import</code> bedeutet hier Import der Preisdaten in EOS, nicht Netzbezug.
                  </p>
                </div>
              </details>
            ) : null}

            <div className="meta-text">
              HTTP-Pfad: <code>{field.http_path_template}</code>
            </div>
            {azimuthWorkaroundHint ? <div className="field-info">{azimuthWorkaroundHint}</div> : null}
            <div className="meta-text">
              Quelle: <strong>{field.last_source ?? "-"}</strong> | Letztes Update: <strong>{formatTimestamp(field.last_update_ts)}</strong>
            </div>
            <div className="meta-text">
              Letzter HTTP-Trigger: <strong>{formatTimestamp(field.http_override_last_ts)}</strong>
            </div>
            {draft?.saving ? <div className="meta-text">speichert...</div> : null}
            {draft?.error ? <div className="field-error">{draft.error}</div> : null}
            {!draft?.error && field.error ? <div className="field-error">{field.error}</div> : null}
          </div>
        );
      })}
    </div>
  );
}

type FieldInputProps = {
  field: SetupField;
  value: string;
  onChange: (field: SetupField, value: string) => void;
  onBlur: (fieldId: string) => void;
};

function FieldInput({ field, value, onChange, onBlur }: FieldInputProps) {
  const isWindowStartTime = /\.home_appliances\.\d+\.time_windows\.windows\.\d+\.start_time$/.test(field.field_id);
  const isWindowDuration = /\.home_appliances\.\d+\.time_windows\.windows\.\d+\.duration_h$/.test(field.field_id);

  if (isWindowStartTime) {
    return (
      <input
        type="time"
        step={60}
        value={value}
        onChange={(event) => onChange(field, event.target.value)}
        onBlur={() => onBlur(field.field_id)}
      />
    );
  }

  if (field.value_type === "select") {
    return (
      <select
        value={value}
        onChange={(event) => onChange(field, event.target.value)}
        onBlur={() => onBlur(field.field_id)}
      >
        <option value="">Bitte wählen...</option>
        {field.options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    );
  }

  if (field.value_type === "string_list") {
    return (
      <input
        type="text"
        value={value}
        onChange={(event) => onChange(field, event.target.value)}
        onBlur={() => onBlur(field.field_id)}
        placeholder="wert1, wert2, wert3"
      />
    );
  }

  if (field.value_type === "number") {
    return (
      <input
        type="number"
        step={isWindowDuration ? "0.25" : "any"}
        min={isWindowDuration ? "0" : undefined}
        value={value}
        onChange={(event) => onChange(field, event.target.value)}
        onBlur={() => onBlur(field.field_id)}
      />
    );
  }

  return (
    <input
      type="text"
      value={value}
      onChange={(event) => onChange(field, event.target.value)}
      onBlur={() => onBlur(field.field_id)}
    />
  );
}
