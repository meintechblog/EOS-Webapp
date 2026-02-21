import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  createOutputTarget,
  forceEosOutputDispatch,
  forceEosRun,
  getEosOutputEvents,
  getEosOutputsCurrent,
  getEosOutputsTimeline,
  getEosRunContext,
  getEosRunPlausibility,
  getEosRunDetail,
  getEosRunPlan,
  getEosRuns,
  getEosRunSolution,
  getEosRuntime,
  getOutputTargets,
  getSetupExport,
  getSetupFields,
  getSetupReadiness,
  getStatus,
  patchSetupFields,
  postSetupImport,
  refreshEosPredictions,
  updateOutputTarget,
} from "./api";
import { OutputChartsPanel } from "./outputCharts";
import type {
  EosPredictionRefreshScope,
  EosOutputCurrentItem,
  EosOutputTimelineItem,
  EosRunPlausibility,
  EosRunPlan,
  EosRunDetail,
  EosRunSolution,
  EosRunSummary,
  EosRuntime,
  OutputDispatchEvent,
  OutputTarget,
  SetupField,
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
  targetPredictionHours: number | null;
  targetHistoricHours: number | null;
  targetOptimizationHours: number | null;
};

type RunTargetMetrics = {
  targetPredictionHours: number | null;
  targetHistoricHours: number | null;
  targetOptimizationHours: number | null;
};

const AUTOSAVE_MS = 1500;
const PREDICTION_HOURS_FIELD_ID = "param.prediction.hours";
const PREDICTION_HISTORIC_HOURS_FIELD_ID = "param.prediction.historic_hours";
const OPTIMIZATION_HOURS_FIELD_ID = "param.optimization.hours";
const OPTIMIZATION_HORIZON_HOURS_FIELD_ID = "param.optimization.horizon_hours";

function toInputString(field: SetupField): string {
  const value = field.current_value;
  if (value === null || value === undefined) {
    return "";
  }
  if (Array.isArray(value)) {
    return value.map((item) => String(item)).join(", ");
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
    targetPredictionHours: null,
    targetHistoricHours: null,
    targetOptimizationHours: null,
  };
}

function extractRunTargetMetrics(contextPayload: unknown): RunTargetMetrics | null {
  const root = asObject(contextPayload);
  if (!root) {
    return null;
  }
  const runtimeSnapshot = asObject(root.runtime_config_snapshot_json);
  const assembledInput = asObject(root.assembled_eos_input_json);
  const source = runtimeSnapshot ?? assembledInput;
  if (!source) {
    return null;
  }

  const prediction = asObject(source.prediction);
  const optimization = asObject(source.optimization);
  const targetPredictionHours = toFiniteNumber(prediction?.hours);
  const targetHistoricHours = toFiniteNumber(prediction?.historic_hours);
  const targetOptimizationHours =
    toFiniteNumber(optimization?.horizon_hours) ??
    toFiniteNumber(optimization?.hours);

  if (targetPredictionHours === null && targetHistoricHours === null && targetOptimizationHours === null) {
    return null;
  }

  return {
    targetPredictionHours,
    targetHistoricHours,
    targetOptimizationHours,
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
    targetPredictionHours: targetMetrics?.targetPredictionHours ?? null,
    targetHistoricHours: targetMetrics?.targetHistoricHours ?? null,
    targetOptimizationHours: targetMetrics?.targetOptimizationHours ?? null,
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

  const targetHours =
    metrics.targetPredictionHours ??
    metrics.targetOptimizationHours ??
    fallbackHorizonHours;
  parts.push(`Ziel ${Math.max(1, Math.round(targetHours))}h`);

  if (metrics.horizonHours !== null) {
    parts.push(`Effektiv ${metrics.horizonHours.toFixed(1)}h`);
  }
  if (metrics.targetHistoricHours !== null) {
    parts.push(`Hist ${Math.max(1, Math.round(metrics.targetHistoricHours))}h`);
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

function dispatchStatusChipClass(statusValue: string): string {
  const status = statusValue.toLowerCase();
  if (status === "sent") {
    return "chip-ok";
  }
  if (status === "blocked" || status === "retrying" || status === "skipped_no_target") {
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

function normalizeResourceId(resourceId: string | null | undefined): string {
  return (resourceId ?? "").trim().toLowerCase();
}

function isHomeApplianceResourceId(resourceId: string | null | undefined): boolean {
  return /^homeappliance\d+$/.test(normalizeResourceId(resourceId));
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
  const [readiness, setReadiness] = useState<SetupReadiness | null>(null);
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [runtime, setRuntime] = useState<EosRuntime | null>(null);
  const [runs, setRuns] = useState<EosRunSummary[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const [selectedRunDetail, setSelectedRunDetail] = useState<EosRunDetail | null>(null);
  const [plan, setPlan] = useState<EosRunPlan | null>(null);
  const [solution, setSolution] = useState<EosRunSolution | null>(null);
  const [outputCurrent, setOutputCurrent] = useState<EosOutputCurrentItem[]>([]);
  const [outputTimeline, setOutputTimeline] = useState<EosOutputTimelineItem[]>([]);
  const [outputEvents, setOutputEvents] = useState<OutputDispatchEvent[]>([]);
  const [outputTargets, setOutputTargets] = useState<OutputTarget[]>([]);
  const [plausibility, setPlausibility] = useState<EosRunPlausibility | null>(null);
  const [isForcingDispatch, setIsForcingDispatch] = useState(false);
  const [dispatchMessage, setDispatchMessage] = useState<string | null>(null);
  const [targetEditId, setTargetEditId] = useState<number | null>(null);
  const [targetForm, setTargetForm] = useState({
    resource_id: "",
    webhook_url: "",
    method: "POST",
    enabled: true,
    timeout_seconds: "10",
    retry_max: "2",
    headers_json: "{}",
    payload_template_json: "",
  });
  const [importText, setImportText] = useState("");
  const [importFeedback, setImportFeedback] = useState<string | null>(null);
  const [runtimeMessage, setRuntimeMessage] = useState<string | null>(null);
  const [globalError, setGlobalError] = useState<string | null>(null);
  const [isForcingRun, setIsForcingRun] = useState(false);
  const [isRefreshingPrediction, setIsRefreshingPrediction] = useState<EosPredictionRefreshScope | null>(null);
  const [isSavingHorizon, setIsSavingHorizon] = useState(false);
  const [horizonHoursDraft, setHorizonHoursDraft] = useState("48");
  const [runNowMs, setRunNowMs] = useState<number>(() => Date.now());
  const [runSourceFilter, setRunSourceFilter] = useState<"all" | "automatic" | "force_run" | "prediction_refresh">("all");
  const [runStatusFilter, setRunStatusFilter] = useState<"all" | "running" | "success" | "partial" | "failed">("all");
  const [runMetricsById, setRunMetricsById] = useState<Record<number, RunPredictionMetrics | null>>({});

  const [drafts, setDrafts] = useState<Record<string, DraftState>>({});
  const draftsRef = useRef(drafts);
  const fieldsRef = useRef(fields);
  const timersRef = useRef<Record<string, number>>({});
  const runMetricsLoadingRef = useRef<Set<number>>(new Set());

  useEffect(() => {
    draftsRef.current = drafts;
  }, [drafts]);

  useEffect(() => {
    fieldsRef.current = fields;
  }, [fields]);

  const loadSetup = useCallback(async () => {
    const [fieldsData, readinessData, statusData] = await Promise.all([
      getSetupFields(),
      getSetupReadiness(),
      getStatus(),
    ]);
    setFields(fieldsData);
    setReadiness(readinessData);
    setStatus(statusData);
  }, []);

  const loadRunCenter = useCallback(async () => {
    const [runtimeData, runsData, targetsData] = await Promise.all([
      getEosRuntime(),
      getEosRuns(),
      getOutputTargets(),
    ]);
    setRuntime(runtimeData);
    setRuns(runsData);
    setOutputTargets(targetsData);
    if (runsData.length === 0) {
      setSelectedRunId(null);
      setSelectedRunDetail(null);
      setPlan(null);
      setSolution(null);
      setOutputCurrent([]);
      setOutputTimeline([]);
      setOutputEvents([]);
      setPlausibility(null);
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
    const [detailData, planData, solutionData, currentData, timelineData, eventsData, plausibilityData] = await Promise.all([
      getEosRunDetail(runId),
      getEosRunPlan(runId),
      getEosRunSolution(runId),
      getEosOutputsCurrent(runId),
      getEosOutputsTimeline({ runId }),
      getEosOutputEvents({ runId, limit: 200 }),
      getEosRunPlausibility(runId),
    ]);
    setSelectedRunDetail(detailData);
    setPlan(planData);
    setSolution(solutionData);
    setOutputCurrent(currentData);
    setOutputTimeline(timelineData);
    setOutputEvents(eventsData);
    setPlausibility(plausibilityData);
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
    }, 15000);
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

      const readinessData = await getSetupReadiness();
      setReadiness(readinessData);
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
    try {
      const response = await forceEosRun();
      setRuntimeMessage(`Force-Run gestartet (run_id=${response.run_id}).`);
      await loadRunCenter();
      setGlobalError(null);
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : String(error));
    } finally {
      setIsForcingRun(false);
    }
  }, [loadRunCenter]);

  const triggerPredictionRefresh = useCallback(
    async (scope: EosPredictionRefreshScope) => {
      setIsRefreshingPrediction(scope);
      try {
        const response = await refreshEosPredictions(scope);
        setRuntimeMessage(
          `Prediction-Refresh (${scope}) gestartet (run_id=${response.run_id}). Nach Abschluss kann ein Force-Run gestartet werden.`,
        );
        await loadRunCenter();
        setGlobalError(null);
      } catch (error) {
        setGlobalError(error instanceof Error ? error.message : String(error));
      } finally {
        setIsRefreshingPrediction(null);
      }
    },
    [loadRunCenter],
  );

  const triggerForceDispatch = useCallback(async () => {
    setIsForcingDispatch(true);
    try {
      const response = await forceEosOutputDispatch();
      setDispatchMessage(response.message);
      if (selectedRunId !== null) {
        await loadRunDetails(selectedRunId);
      } else {
        await loadRunCenter();
      }
      setGlobalError(null);
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : String(error));
    } finally {
      setIsForcingDispatch(false);
    }
  }, [loadRunCenter, loadRunDetails, selectedRunId]);

  const submitOutputTarget = useCallback(async () => {
    try {
      const timeoutSeconds = Number.parseInt(targetForm.timeout_seconds, 10);
      const retryMax = Number.parseInt(targetForm.retry_max, 10);
      const headersJson = targetForm.headers_json.trim() ? JSON.parse(targetForm.headers_json) : {};
      const payloadTemplateJson = targetForm.payload_template_json.trim()
        ? JSON.parse(targetForm.payload_template_json)
        : null;

      const payload = {
        resource_id: targetForm.resource_id.trim(),
        webhook_url: targetForm.webhook_url.trim(),
        method: targetForm.method.trim().toUpperCase(),
        enabled: targetForm.enabled,
        timeout_seconds: Number.isFinite(timeoutSeconds) ? timeoutSeconds : 10,
        retry_max: Number.isFinite(retryMax) ? retryMax : 2,
        headers_json: headersJson,
        payload_template_json: payloadTemplateJson,
      };

      if (targetEditId === null) {
        await createOutputTarget(payload);
      } else {
        await updateOutputTarget(targetEditId, payload);
      }

      setTargetEditId(null);
      setTargetForm({
        resource_id: "",
        webhook_url: "",
        method: "POST",
        enabled: true,
        timeout_seconds: "10",
        retry_max: "2",
        headers_json: "{}",
        payload_template_json: "",
      });
      const targets = await getOutputTargets();
      setOutputTargets(targets);
      setDispatchMessage("Output Target gespeichert.");
      setGlobalError(null);
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : String(error));
    }
  }, [targetEditId, targetForm]);

  const editOutputTarget = useCallback((target: OutputTarget) => {
    setTargetEditId(target.id);
    setTargetForm({
      resource_id: target.resource_id,
      webhook_url: target.webhook_url,
      method: target.method,
      enabled: target.enabled,
      timeout_seconds: String(target.timeout_seconds),
      retry_max: String(target.retry_max),
      headers_json: JSON.stringify(target.headers_json ?? {}, null, 2),
      payload_template_json: target.payload_template_json
        ? JSON.stringify(target.payload_template_json, null, 2)
        : "",
    });
  }, []);

  const toggleOutputTargetEnabled = useCallback(
    async (target: OutputTarget) => {
      try {
        await updateOutputTarget(target.id, { enabled: !target.enabled });
        const targets = await getOutputTargets();
        setOutputTargets(targets);
        if (selectedRunId !== null) {
          await loadRunDetails(selectedRunId);
        }
        setGlobalError(null);
      } catch (error) {
        setGlobalError(error instanceof Error ? error.message : String(error));
      }
    },
    [loadRunDetails, selectedRunId],
  );

  const groupedFields = useMemo(() => {
    const mandatory = fields.filter((field) => field.group === "mandatory");
    const optional = fields.filter((field) => field.group === "optional");
    const live = fields.filter((field) => field.group === "live");
    return { mandatory, optional, live };
  }, [fields]);

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

  const currentPredictionHours = useMemo(
    () => toFiniteNumber(predictionHoursField?.current_value) ?? runtimePredictionHours,
    [predictionHoursField, runtimePredictionHours],
  );
  const currentPredictionHistoricHours = useMemo(
    () => toFiniteNumber(predictionHistoricHoursField?.current_value),
    [predictionHistoricHoursField],
  );
  const currentOptimizationHours = useMemo(
    () =>
      toFiniteNumber(optimizationHorizonHoursField?.current_value) ??
      toFiniteNumber(optimizationHoursField?.current_value) ??
      runtimeOptimizationHours,
    [optimizationHorizonHoursField, optimizationHoursField, runtimeOptimizationHours],
  );

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
    const targetHours = Math.round(currentPredictionHours ?? currentOptimizationHours ?? fallbackHours);
    setHorizonHoursDraft(String(Math.max(1, targetHours)));
  }, [currentOptimizationHours, currentPredictionHours, isSavingHorizon]);

  const baseHorizonHours = useMemo(
    () => Math.max(1, Math.round(currentPredictionHours ?? currentOptimizationHours ?? 48)),
    [currentOptimizationHours, currentPredictionHours],
  );

  const selectedHorizonHours = useMemo(
    () => Math.max(1, Math.round(toFiniteNumber(horizonHoursDraft) ?? baseHorizonHours)),
    [horizonHoursDraft, baseHorizonHours],
  );
  const horizonOptions = useMemo(() => {
    const base = [48, 72, 96];
    const merged = new Set<number>([...base, selectedHorizonHours]);
    return Array.from(merged).sort((left, right) => left - right);
  }, [selectedHorizonHours]);
  const horizonDirty = useMemo(() => {
    return selectedHorizonHours !== baseHorizonHours;
  }, [baseHorizonHours, selectedHorizonHours]);
  const horizonControlDisabled =
    (predictionHoursField === null &&
      optimizationHoursField === null &&
      optimizationHorizonHoursField === null) ||
    isSavingHorizon ||
    isForcingRun ||
    isRefreshingPrediction !== null;
  const hasOptimizationHorizonControl =
    optimizationHoursField !== null || optimizationHorizonHoursField !== null;

  const applyPredictionHorizon = useCallback(async () => {
    if (
      predictionHoursField === null &&
      predictionHistoricHoursField === null &&
      optimizationHoursField === null &&
      optimizationHorizonHoursField === null
    ) {
      setGlobalError("Horizon-Felder sind aktuell nicht verfugbar.");
      return;
    }

    const targetHours = selectedHorizonHours;
    const historicBaselineHours = Math.max(
      840,
      Math.round(currentPredictionHistoricHours ?? 0),
    );
    const historicTargetHours = Math.max(
      targetHours,
      Math.min(2160, Math.max(historicBaselineHours, targetHours * 10)),
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
      setGlobalError("Keine passenden Horizon-Felder verfugbar.");
      return;
    }

    setIsSavingHorizon(true);
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
      setRuntimeMessage(
        `Vorschau-Horizont gesetzt: prediction=${targetHours}h, historic=${historicTargetHours}h.`,
      );
      setGlobalError(null);
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : String(error));
    } finally {
      setIsSavingHorizon(false);
    }
  }, [
    loadRunCenter,
    loadSetup,
    optimizationHorizonHoursField,
    optimizationHoursField,
    predictionHoursField,
    currentPredictionHistoricHours,
    predictionHistoricHoursField,
    selectedHorizonHours,
  ]);

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

  const filteredRuns = useMemo(() => {
    return runs.filter((run) => {
      if (runSourceFilter !== "all" && run.trigger_source !== runSourceFilter) {
        return false;
      }
      if (runStatusFilter !== "all" && run.status !== runStatusFilter) {
        return false;
      }
      return true;
    });
  }, [runs, runSourceFilter, runStatusFilter]);

  const runMetricCandidateIds = useMemo(
    () => {
      const ids = filteredRuns.slice(0, 40).map((run) => run.id);
      if (selectedRunId !== null && !ids.includes(selectedRunId)) {
        ids.push(selectedRunId);
      }
      return ids;
    },
    [filteredRuns, selectedRunId],
  );

  useEffect(() => {
    const missingIds = runMetricCandidateIds.filter(
      (runId) =>
        runMetricsById[runId] === undefined &&
        !runMetricsLoadingRef.current.has(runId),
    );
    if (missingIds.length === 0) {
      return;
    }

    let cancelled = false;
    for (const runId of missingIds) {
      runMetricsLoadingRef.current.add(runId);
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
  }, [runMetricCandidateIds, runMetricsById]);

  const runHints = useMemo(
    () => buildRunHints(selectedRunDetail, plan, solution),
    [selectedRunDetail, plan, solution],
  );
  const runPipelineSteps = useMemo(
    () => buildRunPipelineSteps(selectedRunDetail, plan, solution),
    [selectedRunDetail, plan, solution],
  );

  const configuredHomeApplianceResourceIds = useMemo(() => {
    const configPayload = runtime?.config_payload;
    if (configPayload === null || typeof configPayload !== "object" || Array.isArray(configPayload)) {
      return new Set<string>();
    }

    const devicesValue = (configPayload as Record<string, unknown>).devices;
    if (devicesValue === null || typeof devicesValue !== "object" || Array.isArray(devicesValue)) {
      return new Set<string>();
    }

    const devices = devicesValue as Record<string, unknown>;
    const homeAppliances = Array.isArray(devices.home_appliances) ? devices.home_appliances : [];
    const maxHomeAppliancesRaw = devices.max_home_appliances;
    const parsedMaxHomeAppliances =
      typeof maxHomeAppliancesRaw === "number"
        ? maxHomeAppliancesRaw
        : typeof maxHomeAppliancesRaw === "string" && maxHomeAppliancesRaw.trim() !== ""
          ? Number(maxHomeAppliancesRaw)
          : null;
    const maxHomeAppliances =
      parsedMaxHomeAppliances !== null && Number.isFinite(parsedMaxHomeAppliances)
        ? Math.max(0, Math.floor(parsedMaxHomeAppliances))
        : null;

    if (maxHomeAppliances === 0) {
      return new Set<string>();
    }

    let configuredCount = 0;
    for (const item of homeAppliances) {
      if (item === null || typeof item !== "object" || Array.isArray(item)) {
        continue;
      }
      const deviceId = (item as Record<string, unknown>).device_id;
      if (typeof deviceId === "string" && deviceId.trim() !== "") {
        configuredCount += 1;
      }
    }

    if (configuredCount === 0) {
      return new Set<string>();
    }

    const usableCount = maxHomeAppliances === null ? configuredCount : Math.min(configuredCount, maxHomeAppliances);
    const resourceIds = new Set<string>();
    for (let index = 1; index <= usableCount; index += 1) {
      resourceIds.add(`homeappliance${index}`);
    }
    return resourceIds;
  }, [runtime]);

  const enabledHomeApplianceTargets = useMemo(() => {
    const resources = new Set<string>();
    for (const target of outputTargets) {
      if (!target.enabled || !isHomeApplianceResourceId(target.resource_id)) {
        continue;
      }
      resources.add(normalizeResourceId(target.resource_id));
    }
    return resources;
  }, [outputTargets]);

  const showHomeApplianceOutputs = useMemo(() => {
    if (configuredHomeApplianceResourceIds.size === 0) {
      return false;
    }
    for (const resourceId of configuredHomeApplianceResourceIds) {
      if (enabledHomeApplianceTargets.has(resourceId)) {
        return true;
      }
    }
    return false;
  }, [configuredHomeApplianceResourceIds, enabledHomeApplianceTargets]);

  const isVisibleOutputResource = useCallback(
    (resourceId: string | null | undefined): boolean => {
      if (!isHomeApplianceResourceId(resourceId)) {
        return true;
      }
      return showHomeApplianceOutputs;
    },
    [showHomeApplianceOutputs],
  );

  const visibleOutputCurrent = useMemo(
    () => outputCurrent.filter((item) => isVisibleOutputResource(item.resource_id)),
    [outputCurrent, isVisibleOutputResource],
  );

  const visibleOutputTimeline = useMemo(
    () => outputTimeline.filter((item) => isVisibleOutputResource(item.resource_id)),
    [outputTimeline, isVisibleOutputResource],
  );

  const visibleOutputEvents = useMemo(
    () =>
      outputEvents.filter((event) => {
        if (event.resource_id === null) {
          return true;
        }
        return isVisibleOutputResource(event.resource_id);
      }),
    [outputEvents, isVisibleOutputResource],
  );

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
            Pflichtfelder sind rot, bis sie gültig gespeichert sind. Änderungen werden automatisch gespeichert.
          </p>

          <div className="panel">
            <h3>Pflichtfelder</h3>
            <FieldList fields={groupedFields.mandatory} drafts={drafts} onChange={handleFieldChange} onBlur={flushSave} />
          </div>

          <div className="panel">
            <h3>Optionale Felder</h3>
            <FieldList fields={groupedFields.optional} drafts={drafts} onChange={handleFieldChange} onBlur={flushSave} />
          </div>

          <div className="panel">
            <h3>Live-Signale</h3>
            <FieldList fields={groupedFields.live} drafts={drafts} onChange={handleFieldChange} onBlur={flushSave} />
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
          <p className="pane-copy">Runtime, Run-Typen, Historie und Fehleranalyse in Anwendersprache.</p>

          <div className="panel panel-highlight">
            <div className="panel-head">
              <h3>Vorschau-Horizont</h3>
              <span className="chip chip-neutral">
                aktuell: {baseHorizonHours}h
              </span>
            </div>
            <div className="run-horizon-row">
              <select
                value={horizonHoursDraft}
                onChange={(event) => setHorizonHoursDraft(event.target.value)}
                disabled={horizonControlDisabled}
              >
                {horizonOptions.map((hours) => (
                  <option key={hours} value={String(hours)}>
                    {hours}h
                  </option>
                ))}
              </select>
              <button
                type="button"
                onClick={() => void applyPredictionHorizon()}
                disabled={horizonControlDisabled || !horizonDirty}
              >
                {isSavingHorizon ? "speichere..." : "Horizont übernehmen"}
              </button>
            </div>
            <p className="meta-text">
              Setzt `prediction.hours` und {predictionHistoricHoursField ? "`prediction.historic_hours`" : "optional die Historie"}
              {hasOptimizationHorizonControl ? ", plus Optimierungs-Horizont" : ""}
              {" "}fur nachfolgende Runs. Historic wird adaptiv mit Mindestziel `840h` gesetzt.
            </p>
            {currentPredictionHistoricHours !== null ? (
              <p className="meta-text">Aktuelle Prediction-Historie: {Math.max(1, Math.round(currentPredictionHistoricHours))}h</p>
            ) : null}
            {currentOptimizationHours !== null ? (
              <p className="meta-text">Aktueller Optimierungs-Horizont: {Math.max(1, Math.round(currentOptimizationHours))}h</p>
            ) : null}
          </div>

          <div className="panel">
            <h3>Run-Typen</h3>
            <ul className="plain-list">
              <li><strong>Auto</strong>: EOS-intern ausgelöster Lauf (erkannt über neues `last_run_datetime`).</li>
              <li><strong>Prediction</strong>: aktualisiert nur Vorhersagen (PV/Preis/Load), ohne Plan/Solution.</li>
              <li><strong>Force</strong>: zentraler Optimierungslauf über `pulse_then_legacy`.</li>
              <li><strong>Status</strong>: `success` (vollständig), `partial` (Teilresultat), `failed` (abgebrochen).</li>
            </ul>
            <div className="actions-row wrap">
              <button
                type="button"
                className="secondary"
                onClick={() => void triggerPredictionRefresh("pv")}
                disabled={isRefreshingPrediction !== null || isForcingRun}
              >
                {isRefreshingPrediction === "pv" ? "PV refresh..." : "PV Forecast Refresh"}
              </button>
              <button
                type="button"
                className="secondary"
                onClick={() => void triggerPredictionRefresh("prices")}
                disabled={isRefreshingPrediction !== null || isForcingRun}
              >
                {isRefreshingPrediction === "prices" ? "Preis refresh..." : "Preis Refresh"}
              </button>
              <button
                type="button"
                className="secondary"
                onClick={() => void triggerPredictionRefresh("all")}
                disabled={isRefreshingPrediction !== null || isForcingRun}
              >
                {isRefreshingPrediction === "all" ? "Prediction refresh..." : "Prediction All Refresh"}
              </button>
            </div>
          </div>

          <div className="panel">
            <div className="panel-head">
              <h3>EOS Runtime</h3>
              <button
                type="button"
                onClick={triggerForceRun}
                disabled={isForcingRun || isRefreshingPrediction !== null}
              >
                {isForcingRun ? "läuft..." : "Force Run"}
              </button>
            </div>
            {runtimeMessage ? <p className="meta-text">{runtimeMessage}</p> : null}
            <ul className="plain-list">
              <li>EOS Base URL: <code>{runtime?.eos_base_url ?? "-"}</code></li>
              <li>Health: <strong>{runtime?.health_ok ? "ok" : "offline"}</strong></li>
              <li>Collector: <strong>{runtime?.collector.running ? "running" : "stopped"}</strong></li>
              <li>Letzter EOS-Run: <strong>{formatTimestamp(runtime?.collector.last_observed_eos_run_datetime ?? null)}</strong></li>
              <li>Last Poll: <strong>{formatTimestamp(runtime?.collector.last_poll_ts ?? null)}</strong></li>
              <li>Last Sync: <strong>{formatTimestamp(runtime?.collector.last_successful_sync_ts ?? null)}</strong></li>
              <li>Aligned Scheduler: <strong>{runtime?.collector.aligned_scheduler_enabled ? "aktiv" : "aus"}</strong></li>
              <li>Aligned Slots: <strong>{runtime?.collector.aligned_scheduler_minutes || "-"}</strong> (+{runtime?.collector.aligned_scheduler_delay_seconds ?? 0}s)</li>
              <li>Nächster geplanter Slot: <strong>{formatTimestamp(runtime?.collector.aligned_scheduler_next_due_ts ?? null)}</strong></li>
              <li>Letzter Scheduler-Trigger: <strong>{formatTimestamp(runtime?.collector.aligned_scheduler_last_trigger_ts ?? null)}</strong></li>
              <li>Letzter Scheduler-Skip: <strong>{runtime?.collector.aligned_scheduler_last_skip_reason ?? "-"}</strong></li>
            </ul>
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
            <div className="actions-row">
              <select
                value={runSourceFilter}
                onChange={(event) =>
                  setRunSourceFilter(
                    event.target.value as "all" | "automatic" | "force_run" | "prediction_refresh",
                  )
                }
              >
                <option value="all">Quelle: alle</option>
                <option value="automatic">Quelle: auto</option>
                <option value="force_run">Quelle: force</option>
                <option value="prediction_refresh">Quelle: prediction</option>
              </select>
              <select value={runStatusFilter} onChange={(event) => setRunStatusFilter(event.target.value as "all" | "running" | "success" | "partial" | "failed")}>
                <option value="all">Status: alle</option>
                <option value="running">running</option>
                <option value="success">success</option>
                <option value="partial">partial</option>
                <option value="failed">failed</option>
              </select>
            </div>
            <div className="run-list">
              {filteredRuns.length === 0 ? <p>Keine Runs für den aktiven Filter.</p> : null}
              {filteredRuns.map((run) => {
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
        </section>

        <section className="pane">
          <h2>Outputs</h2>
          <p className="pane-copy">
            Konkrete Ausführung aus EOS-Plan: aktive Entscheidungen, Zustandswechsel, HTTP-Dispatch und Plausibilitätschecks.
          </p>

          <OutputChartsPanel
            runId={selectedRunId}
            timeline={visibleOutputTimeline}
            current={visibleOutputCurrent}
            solutionPayload={solution?.payload_json ?? null}
          />

          <div className="panel">
            <div className="panel-head">
              <h3>Aktive Entscheidungen jetzt</h3>
              <button type="button" onClick={triggerForceDispatch} disabled={isForcingDispatch}>
                {isForcingDispatch ? "sende..." : "Force Dispatch"}
              </button>
            </div>
            {dispatchMessage ? <p className="meta-text">{dispatchMessage}</p> : null}
            {visibleOutputCurrent.length === 0 ? (
              <p>Keine aktive Entscheidung verfügbar.</p>
            ) : (
              <div className="data-table">
                <div className="table-head">
                  <span>Resource</span>
                  <span>Mode</span>
                  <span>Faktor</span>
                  <span>Effective</span>
                  <span>Safety</span>
                </div>
                {visibleOutputCurrent.map((item) => (
                  <div key={`${item.resource_id}-${item.effective_at ?? "na"}`} className="table-row">
                    <span>{item.resource_id}</span>
                    <span>{item.operation_mode_id ?? "-"}</span>
                    <span>{item.operation_mode_factor ?? "-"}</span>
                    <span>{formatTimestamp(item.effective_at)}</span>
                    <span className={`chip ${item.safety_status === "ok" ? "chip-ok" : "chip-warning"}`}>
                      {item.safety_status}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="panel">
            <h3>Nächste Zustandswechsel</h3>
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
          </div>

          <div className="panel">
            <h3>Dispatch-Log</h3>
            {visibleOutputEvents.length === 0 ? (
              <p>Noch keine Dispatch-Events.</p>
            ) : (
              <div className="data-table">
                <div className="table-head">
                  <span>Zeit</span>
                  <span>Resource</span>
                  <span>Kind</span>
                  <span>Status</span>
                  <span>HTTP</span>
                </div>
                {visibleOutputEvents.slice(0, 25).map((event) => (
                  <div key={event.id} className="table-row">
                    <span>{formatTimestamp(event.created_at)}</span>
                    <span>{event.resource_id ?? "-"}</span>
                    <span>{event.dispatch_kind}</span>
                    <span className={`chip ${dispatchStatusChipClass(event.status)}`}>{event.status}</span>
                    <span>{event.http_status ?? "-"}</span>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="panel">
            <h3>Output Targets</h3>
            <div className="data-table compact">
              <div className="table-head">
                <span>Resource</span>
                <span>Webhook</span>
                <span>Method</span>
                <span>Status</span>
                <span>Aktionen</span>
              </div>
              {outputTargets.map((target) => (
                <div key={target.id} className="table-row">
                  <span>{target.resource_id}</span>
                  <span className="truncate">{target.webhook_url}</span>
                  <span>{target.method}</span>
                  <span className={`chip ${target.enabled ? "chip-ok" : "chip-warning"}`}>
                    {target.enabled ? "enabled" : "disabled"}
                  </span>
                  <span className="actions-inline">
                    <button type="button" className="secondary" onClick={() => editOutputTarget(target)}>
                      Edit
                    </button>
                    <button type="button" className="secondary" onClick={() => void toggleOutputTargetEnabled(target)}>
                      {target.enabled ? "Disable" : "Enable"}
                    </button>
                  </span>
                </div>
              ))}
            </div>

            <div className="target-form-grid">
              <input
                value={targetForm.resource_id}
                onChange={(event) => setTargetForm((current) => ({ ...current, resource_id: event.target.value }))}
                placeholder="resource_id (z. B. lfp, shaby)"
              />
              <input
                value={targetForm.webhook_url}
                onChange={(event) => setTargetForm((current) => ({ ...current, webhook_url: event.target.value }))}
                placeholder="http://target.local/webhook"
              />
              <select
                value={targetForm.method}
                onChange={(event) => setTargetForm((current) => ({ ...current, method: event.target.value }))}
              >
                <option value="POST">POST</option>
                <option value="PUT">PUT</option>
                <option value="PATCH">PATCH</option>
              </select>
              <label className="checkbox-line">
                <input
                  type="checkbox"
                  checked={targetForm.enabled}
                  onChange={(event) => setTargetForm((current) => ({ ...current, enabled: event.target.checked }))}
                />
                enabled
              </label>
              <input
                value={targetForm.timeout_seconds}
                onChange={(event) => setTargetForm((current) => ({ ...current, timeout_seconds: event.target.value }))}
                placeholder="timeout_seconds"
              />
              <input
                value={targetForm.retry_max}
                onChange={(event) => setTargetForm((current) => ({ ...current, retry_max: event.target.value }))}
                placeholder="retry_max"
              />
              <textarea
                value={targetForm.headers_json}
                onChange={(event) => setTargetForm((current) => ({ ...current, headers_json: event.target.value }))}
                rows={3}
                placeholder='headers_json, z. B. {"Authorization":"Bearer ..."}'
              />
              <textarea
                value={targetForm.payload_template_json}
                onChange={(event) =>
                  setTargetForm((current) => ({ ...current, payload_template_json: event.target.value }))
                }
                rows={3}
                placeholder='payload_template_json (optional)'
              />
            </div>
            <div className="actions-row">
              <button type="button" onClick={submitOutputTarget}>
                {targetEditId === null ? "Target anlegen" : "Target aktualisieren"}
              </button>
              {targetEditId !== null ? (
                <button
                  type="button"
                  className="secondary"
                  onClick={() => {
                    setTargetEditId(null);
                    setTargetForm({
                      resource_id: "",
                      webhook_url: "",
                      method: "POST",
                      enabled: true,
                      timeout_seconds: "10",
                      retry_max: "2",
                      headers_json: "{}",
                      payload_template_json: "",
                    });
                  }}
                >
                  Edit abbrechen
                </button>
              ) : null}
            </div>
          </div>

          <div className="panel">
            <h3>Plausibilität</h3>
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
          </div>

          <div className="panel">
            <details>
              <summary><strong>Plan (JSON)</strong></summary>
              <pre>{prettyJson(plan?.payload_json ?? null)}</pre>
            </details>
            <details>
              <summary><strong>Solution (JSON)</strong></summary>
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
};

function FieldList({ fields, drafts, onChange, onBlur }: FieldListProps) {
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
        step="any"
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
