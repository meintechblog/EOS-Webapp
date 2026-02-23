import { useEffect, useMemo, useState } from "react";

import { getDataSignalSeries } from "./api";
import type { DataSignalSeries, EosOutputCurrentItem, EosOutputTimelineItem, EosRunPredictionSeries } from "./types";

type OutputChartsPanelProps = {
  runId: number | null;
  timeline: EosOutputTimelineItem[];
  current: EosOutputCurrentItem[];
  configPayload: Record<string, unknown> | null;
  solutionPayload: unknown | null;
  predictionSeries: EosRunPredictionSeries | null;
};

type TimelinePoint = {
  resourceId: string;
  tsMs: number;
  mode: string;
  factor: number | null;
  endsMs: number | null;
};

type CurrentPoint = {
  resourceId: string;
  tsMs: number;
  mode: string;
  factor: number | null;
};

type ModeSegment = {
  resourceId: string;
  startMs: number;
  endMs: number;
  mode: string;
};

type PowerPoint = {
  tsMs: number;
  value: number;
};

type ChartModel = {
  windowStartMs: number;
  windowEndMs: number;
  modeSegments: ModeSegment[];
  modeLegend: string[];
  resourceOrder: string[];
  powerSeries: Record<string, PowerPoint[]>;
  currentPoints: CurrentPoint[];
  hasAnyData: boolean;
};

type PredictionPoint = {
  tsMs: number;
  priceCtPerKwh: number | null;
  pvAcKw: number | null;
  pvDcKw: number | null;
  loadKw: number | null;
};

type PredictionChartModel = {
  points: PredictionPoint[];
  windowStartMs: number;
  windowEndMs: number;
  priceSplitIndex: number;
  knownPriceHours: number;
  intervalMinutes: number;
  horizonHours: number;
  hasPrice: boolean;
  hasPv: boolean;
  hasLoad: boolean;
};

type NumericSeriesPoint = {
  tsMs: number;
  value: number;
};

type PredictionHistoryModel = {
  priceRows: NumericSeriesPoint[];
  pvRows: NumericSeriesPoint[];
  pvForecastRows: NumericSeriesPoint[];
  loadRows: NumericSeriesPoint[];
  loadForecastRows: NumericSeriesPoint[];
  windowStartMs: number | null;
  windowEndMs: number | null;
  hasPrice: boolean;
  hasPv: boolean;
  hasLoad: boolean;
};

type ResourceActualSignalPlan = {
  preferredDirectSignalKeys: string[];
  phaseSignalKeys: string[];
  fallbackDirectSignalKeys: string[];
};

type ActualPowerHistoryModel = {
  byResource: Record<string, NumericSeriesPoint[]>;
  windowStartMs: number | null;
  windowEndMs: number | null;
  hasAny: boolean;
};

type ModeHelpEntry = {
  meaning: string;
  practicalEffect: string;
};

const RESOURCE_COLORS = [
  "#2DD4A6",
  "#7DA6FF",
  "#FFBF75",
  "#F59CC5",
  "#8EE2FF",
  "#C6B6FF",
  "#F9E58B",
  "#7DF0C0",
];

const OUTPUT_CHARTS_OPEN_STORAGE_KEY = "eos-webapp.details.outputs_charts_open";
const MODE_TIMELINE_HELP_OPEN_STORAGE_KEY = "eos-webapp.details.mode_timeline_help_open";

const MODE_COLORS = [
  "#2E8BFF",
  "#28C89B",
  "#FF8F6A",
  "#B884FF",
  "#FFCC66",
  "#7FD1FF",
  "#F27FB5",
  "#A1E06E",
];

const PRICE_REAL_COLOR = "#2DD4A6";
const PRICE_FORECAST_COLOR = "#FFBF75";
const PRICE_NOW_COLOR = "#FF6B6B";
const PV_AC_COLOR = "#7DA6FF";
const PV_DC_COLOR = "#C6B6FF";
const PV_ACTUAL_COLOR = "#2DD4A6";
const LOAD_FORECAST_COLOR = "#F9E58B";
const LOAD_ACTUAL_COLOR = "#8EE2FF";
const FACTOR_ACTUAL_COLOR = "#8EE2FF";
const HISTORY_BUCKET_MS = 1000 * 60 * 5;
const PRICE_CHART_STEP_MS = 1000 * 60 * 15;
const DAY_AHEAD_PUBLICATION_HOUR_LOCAL = 12;
const HISTORY_START_LABEL = "gestern 00:00";
const EMPTY_PREDICTION_HISTORY: PredictionHistoryModel = {
  priceRows: [],
  pvRows: [],
  pvForecastRows: [],
  loadRows: [],
  loadForecastRows: [],
  windowStartMs: null,
  windowEndMs: null,
  hasPrice: false,
  hasPv: false,
  hasLoad: false,
};
const EMPTY_ACTUAL_POWER_HISTORY: ActualPowerHistoryModel = {
  byResource: {},
  windowStartMs: null,
  windowEndMs: null,
  hasAny: false,
};

const MODE_HELP_BY_ID: Record<string, ModeHelpEntry> = {
  IDLE: {
    meaning: "Kein Laden und kein Entladen.",
    practicalEffect: "Das Geraet bleibt im Ruhezustand ohne aktiven Energiefluss.",
  },
  SELF_CONSUMPTION: {
    meaning: "Laedt aus lokalem Ueberschuss und entlaedt zur Deckung lokaler Last (Eigenverbrauch).",
    practicalEffect: "PV-Ueberschuesse koennen gespeichert und spaeter fuer den eigenen Verbrauch genutzt werden.",
  },
  NON_EXPORT: {
    meaning: "Laedt so, dass Einspeisung ins Netz minimiert/verhindert wird; Entladen ins Netz ist nicht erlaubt.",
    practicalEffect: "Ueberschuss wird bevorzugt lokal aufgenommen, statt ins Netz abgegeben zu werden.",
  },
  GRID_SUPPORT_IMPORT: {
    meaning: "Laedt aus dem Netz, wenn der Modus zur Netzaufnahme aktiviert wird.",
    practicalEffect: "Netzbezug zum Laden ist explizit erlaubt und wird per Faktor gesteuert.",
  },
  FORCED_CHARGE: {
    meaning: "Erzwingt Laden unabhaengig von normalen Strategien oder Randbedingungen.",
    practicalEffect: "Ladevorgang hat Prioritaet und uebersteuert die regulaere Betriebslogik.",
  },
};

function toTimestampMs(value: string | null | undefined): number | null {
  if (!value) {
    return null;
  }
  const ts = new Date(value).getTime();
  return Number.isFinite(ts) ? ts : null;
}

function toFiniteNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string") {
    const parsed = Number(value.trim());
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}

function formatTimeTick(ms: number, spanMs: number): string {
  const date = new Date(ms);
  if (spanMs > 1000 * 60 * 60 * 36) {
    return date.toLocaleString([], {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  }
  return date.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}

const HOUR_MS = 1000 * 60 * 60;
const HOUR_STEP_CANDIDATES = [1, 2, 3, 4, 6, 8, 12, 24, 48];

function historyWindowStartMs(referenceMs: number): number {
  const date = new Date(referenceMs);
  date.setHours(0, 0, 0, 0);
  date.setDate(date.getDate() - 1);
  return date.getTime();
}

function computeKnownPriceCutoffMs(referenceTsMs: number): number {
  const reference = new Date(referenceTsMs);
  const cutoff = new Date(reference);
  cutoff.setHours(0, 0, 0, 0);
  const daysAhead = reference.getHours() >= DAY_AHEAD_PUBLICATION_HOUR_LOCAL ? 2 : 1;
  cutoff.setDate(cutoff.getDate() + daysAhead);
  return cutoff.getTime();
}

function floorToHour(ms: number): number {
  return Math.floor(ms / HOUR_MS) * HOUR_MS;
}

function ceilToHour(ms: number): number {
  return Math.ceil(ms / HOUR_MS) * HOUR_MS;
}

function pickHourStep(rangeMs: number, targetTicks: number): number {
  const desiredStepHours = Math.max(1, rangeMs / HOUR_MS / Math.max(1, targetTicks - 1));
  for (const candidate of HOUR_STEP_CANDIDATES) {
    if (candidate >= desiredStepHours) {
      return candidate;
    }
  }
  return HOUR_STEP_CANDIDATES[HOUR_STEP_CANDIDATES.length - 1];
}

function createTimeTicks(startMs: number, endMs: number, targetTicks: number): number[] {
  if (endMs <= startMs) {
    return [floorToHour(startMs)];
  }
  const count = Math.max(2, targetTicks);
  const firstHourTick = ceilToHour(startMs);
  const lastHourTick = floorToHour(endMs);
  const stepHours = pickHourStep(endMs - startMs, count);
  const stepMs = stepHours * HOUR_MS;

  const ticks: number[] = [];
  for (let tick = firstHourTick; tick <= lastHourTick; tick += stepMs) {
    ticks.push(tick);
  }

  if (ticks.length === 0) {
    const startHour = floorToHour(startMs);
    const endHour = ceilToHour(endMs);
    if (startHour === endHour) {
      return [startHour];
    }
    return [startHour, endHour];
  }

  return ticks;
}

function createValueTicks(minValue: number, maxValue: number, targetTicks: number): number[] {
  if (!Number.isFinite(minValue) || !Number.isFinite(maxValue)) {
    return [0, 1];
  }
  if (maxValue <= minValue) {
    return [minValue, maxValue + 1];
  }
  const count = Math.max(2, targetTicks);
  const step = (maxValue - minValue) / (count - 1);
  const ticks: number[] = [];
  for (let index = 0; index < count; index += 1) {
    ticks.push(minValue + step * index);
  }
  return ticks;
}

function mapX(ms: number, startMs: number, endMs: number, left: number, right: number): number {
  if (endMs <= startMs) {
    return left;
  }
  const ratio = (ms - startMs) / (endMs - startMs);
  return left + Math.max(0, Math.min(1, ratio)) * (right - left);
}

function mapY(value: number, minValue: number, maxValue: number, top: number, bottom: number): number {
  if (maxValue <= minValue) {
    return bottom;
  }
  const ratio = (value - minValue) / (maxValue - minValue);
  return bottom - Math.max(0, Math.min(1, ratio)) * (bottom - top);
}

function toIsoUtc(ms: number): string {
  return new Date(ms).toISOString();
}

function toNumericFromDataPoint(point: Record<string, unknown>): number | null {
  const direct = toFiniteNumber(point.value_num);
  if (direct !== null) {
    return direct;
  }
  const last = toFiniteNumber(point.last_num);
  if (last !== null) {
    return last;
  }
  const avg = toFiniteNumber(point.avg_num);
  if (avg !== null) {
    return avg;
  }
  return null;
}

function dedupeSeriesRows(rows: NumericSeriesPoint[]): NumericSeriesPoint[] {
  if (rows.length <= 1) {
    return rows;
  }
  rows.sort((left, right) => left.tsMs - right.tsMs);
  const deduped: NumericSeriesPoint[] = [];
  for (const row of rows) {
    const last = deduped[deduped.length - 1];
    if (last && last.tsMs === row.tsMs) {
      deduped[deduped.length - 1] = row;
    } else {
      deduped.push(row);
    }
  }
  return deduped;
}

function mergeSeriesPreferSecond(
  primaryRows: NumericSeriesPoint[],
  secondaryRows: NumericSeriesPoint[],
): NumericSeriesPoint[] {
  const byTs = new Map<number, number>();
  for (const row of primaryRows) {
    byTs.set(row.tsMs, row.value);
  }
  for (const row of secondaryRows) {
    byTs.set(row.tsMs, row.value);
  }
  const merged: NumericSeriesPoint[] = [];
  for (const [tsMs, value] of byTs.entries()) {
    merged.push({ tsMs, value });
  }
  merged.sort((left, right) => left.tsMs - right.tsMs);
  return merged;
}

function bucketAverageSeries(rows: NumericSeriesPoint[], bucketMs: number): NumericSeriesPoint[] {
  if (rows.length === 0) {
    return [];
  }
  const buckets = new Map<number, { sum: number; count: number }>();
  for (const row of rows) {
    const bucketTs = Math.floor(row.tsMs / bucketMs) * bucketMs;
    const current = buckets.get(bucketTs);
    if (current) {
      current.sum += row.value;
      current.count += 1;
    } else {
      buckets.set(bucketTs, { sum: row.value, count: 1 });
    }
  }

  const aggregated: NumericSeriesPoint[] = [];
  for (const [tsMs, bucket] of buckets.entries()) {
    if (bucket.count <= 0) {
      continue;
    }
    aggregated.push({
      tsMs,
      value: bucket.sum / bucket.count,
    });
  }
  aggregated.sort((left, right) => left.tsMs - right.tsMs);
  return aggregated;
}

function resampleSeriesStep(rows: NumericSeriesPoint[], stepMs: number): NumericSeriesPoint[] {
  if (rows.length <= 1 || stepMs <= 0) {
    return rows;
  }
  const deduped = dedupeSeriesRows([...rows]);
  const startMs = deduped[0].tsMs;
  const endMs = deduped[deduped.length - 1].tsMs;
  if (endMs <= startMs) {
    return deduped;
  }

  const resampled: NumericSeriesPoint[] = [];
  let sourceIndex = 0;
  for (let tsMs = startMs; tsMs <= endMs; tsMs += stepMs) {
    while (
      sourceIndex + 1 < deduped.length &&
      deduped[sourceIndex + 1].tsMs <= tsMs
    ) {
      sourceIndex += 1;
    }
    resampled.push({ tsMs, value: deduped[sourceIndex].value });
  }

  const last = deduped[deduped.length - 1];
  if (resampled.length === 0 || resampled[resampled.length - 1].tsMs !== last.tsMs) {
    resampled.push(last);
  }
  return resampled;
}

function normalizePriceSeriesRows(rows: NumericSeriesPoint[]): NumericSeriesPoint[] {
  if (rows.length === 0) {
    return [];
  }
  return resampleSeriesStep(rows, PRICE_CHART_STEP_MS);
}

function buildStepPolyline(
  rows: NumericSeriesPoint[],
  {
    windowStartMs,
    windowEndMs,
    left,
    right,
    top,
    bottom,
    yMin,
    yMax,
  }: {
    windowStartMs: number;
    windowEndMs: number;
    left: number;
    right: number;
    top: number;
    bottom: number;
    yMin: number;
    yMax: number;
  },
): string {
  if (rows.length === 0) {
    return "";
  }

  const deduped = dedupeSeriesRows([...rows]);
  const points: string[] = [];

  const first = deduped[0];
  let previousY = mapY(first.value, yMin, yMax, top, bottom);
  points.push(
    `${mapX(first.tsMs, windowStartMs, windowEndMs, left, right)},${previousY}`,
  );

  for (let index = 1; index < deduped.length; index += 1) {
    const current = deduped[index];
    const currentX = mapX(current.tsMs, windowStartMs, windowEndMs, left, right);
    const currentY = mapY(current.value, yMin, yMax, top, bottom);
    points.push(`${currentX},${previousY}`);
    points.push(`${currentX},${currentY}`);
    previousY = currentY;
  }

  return points.join(" ");
}

function toNumericSeriesRows(series: DataSignalSeries | null, factor: number): NumericSeriesPoint[] {
  if (!series || !Array.isArray(series.points) || series.points.length === 0) {
    return [];
  }

  const rows: NumericSeriesPoint[] = [];
  for (const point of series.points) {
    const row = asRecord(point);
    if (!row) {
      continue;
    }
    const tsMs = toTimestampMs(typeof row.ts === "string" ? row.ts : null);
    if (tsMs === null) {
      continue;
    }
    const rawValue = toNumericFromDataPoint(row);
    if (rawValue === null) {
      continue;
    }
    rows.push({
      tsMs,
      value: rawValue * factor,
    });
  }

  const deduped = dedupeSeriesRows(rows);
  if (series.resolution === "raw" && deduped.length > 500) {
    return bucketAverageSeries(deduped, HISTORY_BUCKET_MS);
  }
  return deduped;
}

async function fetchSeriesWithFallback(
  signalKey: string,
  fromIso: string,
  toIso: string,
): Promise<DataSignalSeries | null> {
  try {
    const rollupSeries = await getDataSignalSeries({
      signalKey,
      from: fromIso,
      to: toIso,
      resolution: "5m",
    });
    if (isSeriesCoverageSufficient(rollupSeries, fromIso, toIso)) {
      return rollupSeries;
    }
    const rawSeries = await getDataSignalSeries({
      signalKey,
      from: fromIso,
      to: toIso,
      resolution: "raw",
    });
    if (rawSeries.points.length > 0) {
      return rawSeries;
    }
    return rollupSeries.points.length > 0 ? rollupSeries : null;
  } catch {
    return null;
  }
}

function isSeriesCoverageSufficient(
  series: DataSignalSeries | null,
  fromIso: string,
  toIso: string,
): boolean {
  if (!series || !Array.isArray(series.points) || series.points.length === 0) {
    return false;
  }
  if (series.resolution === "raw") {
    return true;
  }

  const targetFromMs = Date.parse(fromIso);
  const targetToMs = Date.parse(toIso);
  if (!Number.isFinite(targetFromMs) || !Number.isFinite(targetToMs) || targetToMs <= targetFromMs) {
    return series.points.length >= 8;
  }

  const timestamps = series.points
    .map((point) => Date.parse(point.ts))
    .filter((value) => Number.isFinite(value))
    .sort((left, right) => left - right);
  if (timestamps.length < 2) {
    return false;
  }

  const coveredMs = timestamps[timestamps.length - 1] - timestamps[0];
  const requestedMs = targetToMs - targetFromMs;
  if (coveredMs <= 0 || requestedMs <= 0) {
    return false;
  }

  const coverageRatio = coveredMs / requestedMs;
  return series.points.length >= 8 && coverageRatio >= 0.6;
}

async function fetchFirstSeriesWithFallback(
  signalKeys: string[],
  fromIso: string,
  toIso: string,
): Promise<DataSignalSeries | null> {
  for (const signalKey of signalKeys) {
    const series = await fetchSeriesWithFallback(signalKey, fromIso, toIso);
    if (series && series.points.length > 0) {
      return series;
    }
  }
  return null;
}

async function fetchPriceHistoryRows(fromIso: string, toIso: string): Promise<NumericSeriesPoint[]> {
  const priceSeriesWh = await fetchSeriesWithFallback("prediction.elecprice_marketprice_wh", fromIso, toIso);
  const rowsWh = toNumericSeriesRows(priceSeriesWh, 100000);
  if (rowsWh.length > 0) {
    return rowsWh;
  }

  const priceSeriesKwh = await fetchSeriesWithFallback("prediction.elecprice_marketprice_kwh", fromIso, toIso);
  return toNumericSeriesRows(priceSeriesKwh, 100);
}

async function loadPredictionHistory(): Promise<PredictionHistoryModel> {
  const nowMs = Date.now();
  const fromMs = historyWindowStartMs(nowMs);
  const fromIso = toIsoUtc(fromMs);
  const toIso = toIsoUtc(nowMs);

  const [priceRows, pvSeries, loadSeries] = await Promise.all([
    fetchPriceHistoryRows(fromIso, toIso),
    fetchSeriesWithFallback("pv_power_w", fromIso, toIso),
    fetchSeriesWithFallback("house_load_w", fromIso, toIso),
  ]);
  const [pvForecastSeries, loadForecastSeries] = await Promise.all([
    fetchFirstSeriesWithFallback(
      ["prediction.pvforecast_ac_power", "prediction.pvforecastakkudoktor_ac_power_any"],
      fromIso,
      toIso,
    ),
    fetchFirstSeriesWithFallback(
      [
        "prediction.loadforecast_power_w",
        "prediction.load_mean_adjusted",
        "prediction.load_mean",
        "prediction.loadakkudoktor_mean_power_w",
      ],
      fromIso,
      toIso,
    ),
  ]);
  const pvRows = toNumericSeriesRows(pvSeries, 0.001);
  const pvForecastRows = toNumericSeriesRows(pvForecastSeries, 0.001);
  const loadRows = toNumericSeriesRows(loadSeries, 0.001);
  const loadForecastRows = toNumericSeriesRows(loadForecastSeries, 0.001);
  const allRows = [
    ...priceRows,
    ...pvRows,
    ...pvForecastRows,
    ...loadRows,
    ...loadForecastRows,
  ];

  return {
    priceRows,
    pvRows,
    pvForecastRows,
    loadRows,
    loadForecastRows,
    windowStartMs: allRows.length > 0 ? Math.min(...allRows.map((row) => row.tsMs)) : null,
    windowEndMs: allRows.length > 0 ? Math.max(...allRows.map((row) => row.tsMs)) : null,
    hasPrice: priceRows.length > 0,
    hasPv: pvRows.length > 0 || pvForecastRows.length > 0,
    hasLoad: loadRows.length > 0 || loadForecastRows.length > 0,
  };
}

function mergePredictionWindow(
  predictionModel: PredictionChartModel,
  historyModel: PredictionHistoryModel,
): { startMs: number; endMs: number } {
  const baseHistoryStartMs = historyWindowStartMs(Date.now());
  const startCandidates: number[] = [];
  const endCandidates: number[] = [];

  if (predictionModel.points.length > 0) {
    startCandidates.push(predictionModel.windowStartMs);
    endCandidates.push(predictionModel.windowEndMs);
  }
  if (historyModel.windowStartMs !== null) {
    startCandidates.push(historyModel.windowStartMs);
  }
  if (historyModel.windowEndMs !== null) {
    endCandidates.push(historyModel.windowEndMs);
  }

  if (startCandidates.length === 0 || endCandidates.length === 0) {
    const nowMs = Date.now();
    return {
      startMs: baseHistoryStartMs,
      endMs: Math.max(nowMs + HOUR_MS * 8, baseHistoryStartMs + HOUR_MS * 8),
    };
  }

  const startMs = Math.min(baseHistoryStartMs, ...startCandidates);
  const endMs = Math.max(...endCandidates);
  if (endMs <= startMs) {
    return {
      startMs,
      endMs: Math.max(startMs + HOUR_MS, Date.now()),
    };
  }
  return { startMs, endMs };
}

function mergeFactorWindow(
  instructionWindow: { startMs: number; endMs: number },
  actualHistory: ActualPowerHistoryModel,
): { startMs: number; endMs: number } {
  const startMs =
    actualHistory.windowStartMs !== null
      ? Math.min(instructionWindow.startMs, actualHistory.windowStartMs)
      : instructionWindow.startMs;
  const endMs =
    actualHistory.windowEndMs !== null
      ? Math.max(instructionWindow.endMs, actualHistory.windowEndMs)
      : instructionWindow.endMs;
  if (endMs <= startMs) {
    return {
      startMs,
      endMs: Math.max(startMs + HOUR_MS, Date.now()),
    };
  }
  return { startMs, endMs };
}

function normalizeMode(mode: string | null): string {
  const normalized = (mode ?? "").trim();
  return normalized === "" ? "UNKNOWN" : normalized;
}

function normalizeResourceKey(resourceId: string | null | undefined): string {
  return (resourceId ?? "").trim().toLowerCase();
}

function toNonEmptyString(value: unknown): string | null {
  if (typeof value !== "string") {
    return null;
  }
  const text = value.trim();
  return text === "" ? null : text;
}

function uniqueSignalKeys(values: Array<string | null | undefined>): string[] {
  const unique: string[] = [];
  const seen = new Set<string>();
  for (const value of values) {
    const text = typeof value === "string" ? value.trim() : "";
    if (text === "" || seen.has(text)) {
      continue;
    }
    seen.add(text);
    unique.push(text);
  }
  return unique;
}

function toPositiveKwFromW(rawValue: unknown): number | null {
  const watts = toFiniteNumber(rawValue);
  if (watts === null || watts <= 0) {
    return null;
  }
  return watts / 1000.0;
}

function extractResourceMaxPowerKw(configPayload: Record<string, unknown> | null): Map<string, number> {
  const map = new Map<string, number>();
  const config = asRecord(configPayload);
  const devices = asRecord(config?.devices);
  if (!devices) {
    return map;
  }

  const put = (key: string, kw: number | null) => {
    if (kw === null || !Number.isFinite(kw) || kw <= 0) {
      return;
    }
    map.set(normalizeResourceKey(key), kw);
  };

  const batteries = Array.isArray(devices.batteries) ? devices.batteries : [];
  batteries.forEach((rawItem, index) => {
    const item = asRecord(rawItem);
    if (!item) {
      return;
    }
    const maxKw = toPositiveKwFromW(item.max_charge_power_w);
    put(`battery${index + 1}`, maxKw);
    if (typeof item.device_id === "string" && item.device_id.trim() !== "") {
      put(item.device_id, maxKw);
    }
  });

  const electricVehicles = Array.isArray(devices.electric_vehicles) ? devices.electric_vehicles : [];
  electricVehicles.forEach((rawItem, index) => {
    const item = asRecord(rawItem);
    if (!item) {
      return;
    }
    const maxKw = toPositiveKwFromW(item.max_charge_power_w);
    put(`ev${index + 1}`, maxKw);
    put(`electric_vehicle${index + 1}`, maxKw);
    if (typeof item.device_id === "string" && item.device_id.trim() !== "") {
      put(item.device_id, maxKw);
    }
  });

  return map;
}

function isBatteryLikeResource(resourceId: string): boolean {
  const key = normalizeResourceKey(resourceId);
  return key.includes("battery") || key.includes("lfp");
}

function isEvLikeResource(resourceId: string): boolean {
  const key = normalizeResourceKey(resourceId);
  return key.startsWith("ev") || key.includes("electric_vehicle") || key.includes("vehicle");
}

function resolveResourceActualSignalPlans(
  configPayload: Record<string, unknown> | null,
  resourceOrder: string[],
): Record<string, ResourceActualSignalPlan> {
  const config = asRecord(configPayload);
  const devices = asRecord(config?.devices);
  if (!devices || resourceOrder.length === 0) {
    return {};
  }

  type DeviceSignalProfile = {
    aliases: Set<string>;
    preferredDirectSignalKeys: string[];
    phaseSignalKeys: string[];
  };

  const buildProfiles = (
    items: unknown[],
    aliasesForIndex: (index: number) => string[],
  ): DeviceSignalProfile[] => {
    const profiles: DeviceSignalProfile[] = [];
    items.forEach((rawItem, index) => {
      const item = asRecord(rawItem);
      if (!item) {
        return;
      }
      const aliases = new Set<string>();
      for (const alias of aliasesForIndex(index)) {
        const normalizedAlias = normalizeResourceKey(alias);
        if (normalizedAlias !== "") {
          aliases.add(normalizedAlias);
        }
      }
      const deviceId = toNonEmptyString(item.device_id);
      if (deviceId) {
        aliases.add(normalizeResourceKey(deviceId));
      }
      if (aliases.size === 0) {
        return;
      }
      const preferredDirectSignalKeys = uniqueSignalKeys([
        toNonEmptyString(item.measurement_key_power_3_phase_sym_w),
        toNonEmptyString(item.measurement_key_power_w),
      ]);
      const phaseSignalKeys = uniqueSignalKeys([
        toNonEmptyString(item.measurement_key_power_l1_w),
        toNonEmptyString(item.measurement_key_power_l2_w),
        toNonEmptyString(item.measurement_key_power_l3_w),
      ]);
      profiles.push({
        aliases,
        preferredDirectSignalKeys,
        phaseSignalKeys,
      });
    });
    return profiles;
  };

  const batteryProfiles = buildProfiles(
    Array.isArray(devices.batteries) ? devices.batteries : [],
    (index) => [`battery${index + 1}`],
  );
  const evProfiles = buildProfiles(
    Array.isArray(devices.electric_vehicles) ? devices.electric_vehicles : [],
    (index) => [`ev${index + 1}`, `electric_vehicle${index + 1}`],
  );

  const findProfileForResource = (
    profiles: DeviceSignalProfile[],
    resourceId: string,
  ): DeviceSignalProfile | null => {
    const normalized = normalizeResourceKey(resourceId);
    if (normalized === "") {
      return null;
    }
    for (const profile of profiles) {
      if (profile.aliases.has(normalized)) {
        return profile;
      }
    }
    return null;
  };

  const plans: Record<string, ResourceActualSignalPlan> = {};
  for (const resourceId of resourceOrder) {
    const batteryProfile =
      findProfileForResource(batteryProfiles, resourceId) ??
      (isBatteryLikeResource(resourceId) && batteryProfiles.length === 1 ? batteryProfiles[0] : null);
    const evProfile =
      findProfileForResource(evProfiles, resourceId) ??
      (isEvLikeResource(resourceId) && evProfiles.length === 1 ? evProfiles[0] : null);

    const preferredDirectSignalKeys: string[] = [];
    const phaseSignalKeys: string[] = [];
    const fallbackDirectSignalKeys: string[] = [];

    if (batteryProfile !== null) {
      preferredDirectSignalKeys.push(...batteryProfile.preferredDirectSignalKeys);
      phaseSignalKeys.push(...batteryProfile.phaseSignalKeys);
      if (batteryProfiles.length === 1) {
        fallbackDirectSignalKeys.push("battery_power_w");
      }
    }

    if (evProfile !== null) {
      preferredDirectSignalKeys.push(...evProfile.preferredDirectSignalKeys);
      phaseSignalKeys.push(...evProfile.phaseSignalKeys);
      if (evProfiles.length === 1) {
        fallbackDirectSignalKeys.push("ev_charging_power_w");
      }
    }

    const normalizedPlan: ResourceActualSignalPlan = {
      preferredDirectSignalKeys: uniqueSignalKeys(preferredDirectSignalKeys),
      phaseSignalKeys: uniqueSignalKeys(phaseSignalKeys),
      fallbackDirectSignalKeys: uniqueSignalKeys(fallbackDirectSignalKeys),
    };
    if (
      normalizedPlan.preferredDirectSignalKeys.length === 0 &&
      normalizedPlan.phaseSignalKeys.length === 0 &&
      normalizedPlan.fallbackDirectSignalKeys.length === 0
    ) {
      continue;
    }
    plans[resourceId] = normalizedPlan;
  }
  return plans;
}

function sumSeriesRows(seriesRows: NumericSeriesPoint[][]): NumericSeriesPoint[] {
  const summedByTs = new Map<number, number>();
  let hasAnyRow = false;
  for (const rows of seriesRows) {
    if (rows.length === 0) {
      continue;
    }
    hasAnyRow = true;
    for (const row of rows) {
      summedByTs.set(row.tsMs, (summedByTs.get(row.tsMs) ?? 0) + row.value);
    }
  }
  if (!hasAnyRow) {
    return [];
  }
  const summedRows: NumericSeriesPoint[] = [];
  for (const [tsMs, value] of summedByTs.entries()) {
    summedRows.push({ tsMs, value });
  }
  summedRows.sort((left, right) => left.tsMs - right.tsMs);
  return summedRows;
}

async function loadActualPowerRowsForResource(
  plan: ResourceActualSignalPlan,
  fromIso: string,
  toIso: string,
): Promise<NumericSeriesPoint[]> {
  for (const signalKey of plan.preferredDirectSignalKeys) {
    const series = await fetchSeriesWithFallback(signalKey, fromIso, toIso);
    const rows = toNumericSeriesRows(series, 0.001);
    if (rows.length > 0) {
      return rows;
    }
  }

  if (plan.phaseSignalKeys.length > 0) {
    const phaseRows = await Promise.all(
      plan.phaseSignalKeys.map(async (signalKey) => {
        const series = await fetchSeriesWithFallback(signalKey, fromIso, toIso);
        return toNumericSeriesRows(series, 0.001);
      }),
    );
    const summedRows = sumSeriesRows(phaseRows);
    if (summedRows.length > 0) {
      return summedRows;
    }
  }

  for (const signalKey of plan.fallbackDirectSignalKeys) {
    const series = await fetchSeriesWithFallback(signalKey, fromIso, toIso);
    const rows = toNumericSeriesRows(series, 0.001);
    if (rows.length > 0) {
      return rows;
    }
  }

  return [];
}

async function loadActualPowerHistory(
  plansByResource: Record<string, ResourceActualSignalPlan>,
): Promise<ActualPowerHistoryModel> {
  const entries = Object.entries(plansByResource);
  if (entries.length === 0) {
    return EMPTY_ACTUAL_POWER_HISTORY;
  }
  const nowMs = Date.now();
  const fromIso = toIsoUtc(historyWindowStartMs(nowMs));
  const toIso = toIsoUtc(nowMs);

  const loadedEntries = await Promise.all(
    entries.map(async ([resourceId, plan]) => {
      const rows = await loadActualPowerRowsForResource(plan, fromIso, toIso);
      return [resourceId, rows] as const;
    }),
  );

  const byResource: Record<string, NumericSeriesPoint[]> = {};
  const allRows: NumericSeriesPoint[] = [];
  for (const [resourceId, rows] of loadedEntries) {
    if (rows.length === 0) {
      continue;
    }
    byResource[resourceId] = rows;
    allRows.push(...rows);
  }

  return {
    byResource,
    windowStartMs: allRows.length > 0 ? Math.min(...allRows.map((row) => row.tsMs)) : null,
    windowEndMs: allRows.length > 0 ? Math.max(...allRows.map((row) => row.tsMs)) : null,
    hasAny: allRows.length > 0,
  };
}

function inferModeDirection(mode: string): number {
  const upperMode = mode.trim().toUpperCase();
  if (
    upperMode === "" ||
    upperMode === "UNKNOWN" ||
    upperMode === "IDLE" ||
    upperMode === "NONE"
  ) {
    return 0;
  }
  if (upperMode === "NON_EXPORT" || upperMode === "SELF_CONSUMPTION") {
    return 0;
  }
  if (
    upperMode.includes("FORCED_CHARGE") ||
    upperMode.includes("GRID_SUPPORT_IMPORT") ||
    upperMode.includes("CHARGE") ||
    upperMode.includes("IMPORT")
  ) {
    return 1;
  }
  if (
    upperMode.includes("FORCED_DISCHARGE") ||
    upperMode.includes("GRID_SUPPORT_EXPORT") ||
    upperMode.includes("DISCHARGE") ||
    upperMode.includes("EXPORT")
  ) {
    return -1;
  }
  return 0;
}

function toRequestedPowerKw({
  resourceId,
  mode,
  factor,
  maxPowerKwByResource,
}: {
  resourceId: string;
  mode: string;
  factor: number | null;
  maxPowerKwByResource: Map<string, number>;
}): number | null {
  const maxPowerKw = maxPowerKwByResource.get(normalizeResourceKey(resourceId));
  if (maxPowerKw === undefined) {
    return null;
  }
  const direction = inferModeDirection(mode);
  if (isBatteryLikeResource(resourceId) && direction > 0) {
    // Requested: battery chart should only show explicit discharge/export phases.
    return 0;
  }
  if (direction === 0) {
    return 0;
  }
  const normalizedFactor =
    factor !== null && Number.isFinite(factor) ? Math.max(0, Math.abs(factor)) : 1.0;
  return direction * normalizedFactor * maxPowerKw;
}

function parseTimelinePoints(timeline: EosOutputTimelineItem[]): TimelinePoint[] {
  const parsed: TimelinePoint[] = [];
  for (const item of timeline) {
    const tsMs = toTimestampMs(item.execution_time ?? item.starts_at);
    if (tsMs === null) {
      continue;
    }
    parsed.push({
      resourceId: item.resource_id,
      tsMs,
      mode: normalizeMode(item.operation_mode_id),
      factor: item.operation_mode_factor,
      endsMs: toTimestampMs(item.ends_at),
    });
  }
  parsed.sort((left, right) => left.tsMs - right.tsMs);
  return parsed;
}

function parseCurrentPoints(current: EosOutputCurrentItem[]): CurrentPoint[] {
  const nowMs = Date.now();
  const parsed: CurrentPoint[] = [];
  for (const item of current) {
    const tsMs = toTimestampMs(item.effective_at) ?? nowMs;
    parsed.push({
      resourceId: item.resource_id,
      tsMs,
      mode: normalizeMode(item.operation_mode_id),
      factor: item.operation_mode_factor,
    });
  }
  parsed.sort((left, right) => left.tsMs - right.tsMs);
  return parsed;
}

function deriveWindow(points: TimelinePoint[], current: CurrentPoint[]): { startMs: number; endMs: number } {
  const allTs: number[] = [];
  for (const point of points) {
    allTs.push(point.tsMs);
    if (point.endsMs !== null) {
      allTs.push(point.endsMs);
    }
  }
  for (const point of current) {
    allTs.push(point.tsMs);
  }

  if (allTs.length === 0) {
    const nowMs = Date.now();
    return {
      startMs: nowMs - 1000 * 60 * 60,
      endMs: nowMs + 1000 * 60 * 60 * 8,
    };
  }

  const minTs = Math.min(...allTs);
  const maxTs = Math.max(...allTs);
  const span = Math.max(1000 * 60 * 60, maxTs - minTs);
  const pad = Math.round(span * 0.08);
  return {
    startMs: minTs - pad,
    endMs: maxTs + pad,
  };
}

function buildModeSegments(points: TimelinePoint[], windowEndMs: number): ModeSegment[] {
  const byResource = new Map<string, TimelinePoint[]>();
  for (const point of points) {
    const existing = byResource.get(point.resourceId);
    if (existing) {
      existing.push(point);
    } else {
      byResource.set(point.resourceId, [point]);
    }
  }

  const segments: ModeSegment[] = [];
  for (const [resourceId, rows] of byResource.entries()) {
    rows.sort((left, right) => left.tsMs - right.tsMs);
    for (let index = 0; index < rows.length; index += 1) {
      const current = rows[index];
      const next = rows[index + 1];
      const nextStart = next ? next.tsMs : null;
      let endMs = current.endsMs ?? nextStart ?? windowEndMs;
      if (endMs <= current.tsMs) {
        endMs = current.tsMs + 1000 * 60 * 5;
      }
      segments.push({
        resourceId,
        startMs: current.tsMs,
        endMs,
        mode: current.mode,
      });
    }
  }
  segments.sort((left, right) => left.startMs - right.startMs);
  return segments;
}

function buildPowerSeries(
  points: TimelinePoint[],
  current: CurrentPoint[],
  maxPowerKwByResource: Map<string, number>,
): Record<string, PowerPoint[]> {
  const series = new Map<string, PowerPoint[]>();

  for (const point of points) {
    const powerKw = toRequestedPowerKw({
      resourceId: point.resourceId,
      mode: point.mode,
      factor: point.factor,
      maxPowerKwByResource,
    });
    if (powerKw === null) {
      continue;
    }
    const rows = series.get(point.resourceId);
    const item = { tsMs: point.tsMs, value: powerKw };
    if (rows) {
      rows.push(item);
    } else {
      series.set(point.resourceId, [item]);
    }
  }

  for (const point of current) {
    const powerKw = toRequestedPowerKw({
      resourceId: point.resourceId,
      mode: point.mode,
      factor: point.factor,
      maxPowerKwByResource,
    });
    if (powerKw === null) {
      continue;
    }
    const rows = series.get(point.resourceId);
    const item = { tsMs: point.tsMs, value: powerKw };
    if (rows) {
      rows.push(item);
    } else {
      series.set(point.resourceId, [item]);
    }
  }

  const normalized: Record<string, PowerPoint[]> = {};
  for (const [resourceId, rows] of series.entries()) {
    rows.sort((left, right) => left.tsMs - right.tsMs);
    const deduped: PowerPoint[] = [];
    for (const row of rows) {
      const last = deduped[deduped.length - 1];
      if (last && last.tsMs === row.tsMs) {
        deduped[deduped.length - 1] = row;
      } else {
        deduped.push(row);
      }
    }
    normalized[resourceId] = deduped;
  }
  return normalized;
}

function buildChartModel(
  timeline: EosOutputTimelineItem[],
  current: EosOutputCurrentItem[],
  maxPowerKwByResource: Map<string, number>,
): ChartModel {
  const timelinePoints = parseTimelinePoints(timeline);
  const currentPoints = parseCurrentPoints(current);
  const { startMs, endMs } = deriveWindow(timelinePoints, currentPoints);
  const modeSegments = buildModeSegments(timelinePoints, endMs);
  const powerSeries = buildPowerSeries(timelinePoints, currentPoints, maxPowerKwByResource);

  const modeLegend = Array.from(new Set(modeSegments.map((segment) => segment.mode))).sort((left, right) =>
    left.localeCompare(right),
  );

  const resourceFromSegments = modeSegments.map((segment) => segment.resourceId);
  const resourceFromPowers = Object.keys(powerSeries);
  const resourceFromCurrent = currentPoints.map((point) => point.resourceId);
  const resourceOrder = Array.from(new Set([...resourceFromSegments, ...resourceFromPowers, ...resourceFromCurrent]));

  return {
    windowStartMs: startMs,
    windowEndMs: endMs,
    modeSegments,
    modeLegend,
    resourceOrder,
    powerSeries,
    currentPoints,
    hasAnyData: timelinePoints.length > 0 || currentPoints.length > 0,
  };
}

function median(values: number[]): number | null {
  if (values.length === 0) {
    return null;
  }
  const sorted = [...values].sort((left, right) => left - right);
  const middle = Math.floor(sorted.length / 2);
  if (sorted.length % 2 === 0) {
    return (sorted[middle - 1] + sorted[middle]) / 2;
  }
  return sorted[middle];
}

function inferIntervalHours(points: Array<{ tsMs: number }>, index: number): number {
  const current = points[index];
  const next = points[index + 1];
  const prev = points[index - 1];

  const diffsMs: number[] = [];
  if (next && next.tsMs > current.tsMs) {
    diffsMs.push(next.tsMs - current.tsMs);
  }
  if (prev && current.tsMs > prev.tsMs) {
    diffsMs.push(current.tsMs - prev.tsMs);
  }

  if (diffsMs.length === 0) {
    return 1;
  }
  const rawHours = diffsMs[0] / (1000 * 60 * 60);
  if (!Number.isFinite(rawHours) || rawHours <= 0 || rawHours > 12) {
    return 1;
  }
  return rawHours;
}

function priceCtFromRow(row: Record<string, unknown>): number | null {
  const ctDirect = toFiniteNumber(row.elec_price_ct_per_kwh ?? row.electricity_price_ct_per_kwh);
  if (ctDirect !== null) {
    return ctDirect;
  }

  const eurPerKwh = toFiniteNumber(
    row.elec_price_amt_kwh ??
      row.elecprice_marketprice_kwh ??
      row.electricity_price_eur_per_kwh ??
      row.strompreis_euro_pro_kwh,
  );
  if (eurPerKwh !== null) {
    return eurPerKwh * 100;
  }

  const eurPerWh = toFiniteNumber(row.elecprice_marketprice_wh ?? row.strompreis_euro_pro_wh);
  if (eurPerWh !== null) {
    return eurPerWh * 100000;
  }

  return null;
}

function powerWToKw(powerW: number): number {
  return powerW / 1000;
}

function energyWhToKw(energyWh: number, intervalHours: number): number {
  return energyWh / (1000 * Math.max(0.05, intervalHours));
}

function pvKwFromRow(row: Record<string, unknown>, intervalHours: number, kind: "ac" | "dc"): number | null {
  const powerKey = kind === "ac" ? "pvforecast_ac_power" : "pvforecast_dc_power";
  const powerW = toFiniteNumber(row[powerKey]);
  if (powerW !== null) {
    return powerWToKw(powerW);
  }

  const energyKey = kind === "ac" ? "pvforecast_ac_energy_wh" : "pvforecast_dc_energy_wh";
  const energyWh = toFiniteNumber(row[energyKey]);
  if (energyWh !== null) {
    return energyWhToKw(energyWh, intervalHours);
  }

  if (kind === "ac") {
    const legacyWh = toFiniteNumber(row.pv_prognose_wh);
    if (legacyWh !== null) {
      return energyWhToKw(legacyWh, intervalHours);
    }
  }

  return null;
}

function loadKwFromRow(row: Record<string, unknown>, intervalHours: number): number | null {
  const powerKeys = [
    "loadforecast_power_w",
    "load_mean_adjusted",
    "load_mean",
    "loadakkudoktor_mean_power_w",
  ] as const;
  for (const key of powerKeys) {
    const powerW = toFiniteNumber(row[key]);
    if (powerW !== null) {
      return powerWToKw(powerW);
    }
  }

  const energyKeys = [
    "loadforecast_energy_wh",
    "loadakkudoktor_mean_energy_wh",
    "load_mean_energy_wh",
  ] as const;
  for (const key of energyKeys) {
    const energyWh = toFiniteNumber(row[key]);
    if (energyWh !== null) {
      return energyWhToKw(energyWh, intervalHours);
    }
  }

  return null;
}

function parsePredictionPoints(solutionPayload: unknown): PredictionPoint[] {
  const solution = asRecord(solutionPayload);
  const prediction = asRecord(solution?.prediction);
  const dataRaw = prediction?.data;
  const rows: Array<{ tsMs: number; row: Record<string, unknown> }> = [];

  if (Array.isArray(dataRaw)) {
    for (const item of dataRaw) {
      const row = asRecord(item);
      if (!row) {
        continue;
      }
      const tsMs = toTimestampMs(String(row.date_time ?? ""));
      if (tsMs === null) {
        continue;
      }
      rows.push({ tsMs, row });
    }
  } else {
    const dataObject = asRecord(dataRaw);
    if (dataObject) {
      for (const [rawTs, value] of Object.entries(dataObject)) {
        const row = asRecord(value);
        if (!row) {
          continue;
        }
        const tsMs = toTimestampMs(String(row.date_time ?? rawTs));
        if (tsMs === null) {
          continue;
        }
        rows.push({ tsMs, row });
      }
    }
  }

  rows.sort((left, right) => left.tsMs - right.tsMs);

  const points: PredictionPoint[] = [];
  for (let index = 0; index < rows.length; index += 1) {
    const current = rows[index];
    const intervalHours = inferIntervalHours(rows, index);
    points.push({
      tsMs: current.tsMs,
      priceCtPerKwh: priceCtFromRow(current.row),
      pvAcKw: pvKwFromRow(current.row, intervalHours, "ac"),
      pvDcKw: pvKwFromRow(current.row, intervalHours, "dc"),
      loadKw: loadKwFromRow(current.row, intervalHours),
    });
  }

  return points;
}

function parseArtifactPredictionPoints(predictionSeries: EosRunPredictionSeries | null): PredictionPoint[] {
  if (!predictionSeries || !Array.isArray(predictionSeries.points)) {
    return [];
  }

  const points: PredictionPoint[] = [];
  for (const item of predictionSeries.points) {
    const tsMs = toTimestampMs(item.date_time);
    if (tsMs === null) {
      continue;
    }
    points.push({
      tsMs,
      priceCtPerKwh: toFiniteNumber(item.elec_price_ct_per_kwh),
      pvAcKw: toFiniteNumber(item.pv_ac_kw),
      pvDcKw: toFiniteNumber(item.pv_dc_kw),
      loadKw: toFiniteNumber(item.load_kw),
    });
  }
  points.sort((left, right) => left.tsMs - right.tsMs);
  return points;
}

function buildPredictionModel(solutionPayload: unknown, predictionSeries: EosRunPredictionSeries | null): PredictionChartModel {
  const artifactPoints = parseArtifactPredictionPoints(predictionSeries);
  const points = artifactPoints.length > 0 ? artifactPoints : parsePredictionPoints(solutionPayload);
  if (points.length === 0) {
    const historyStartMs = historyWindowStartMs(Date.now());
    return {
      points: [],
      windowStartMs: historyStartMs,
      windowEndMs: Math.max(Date.now() + 1000 * 60 * 60 * 8, historyStartMs + 1000 * 60 * 60 * 8),
      priceSplitIndex: 0,
      knownPriceHours: 0,
      intervalMinutes: 60,
      horizonHours: 0,
      hasPrice: false,
      hasPv: false,
      hasLoad: false,
    };
  }

  const firstTs = points[0].tsMs;
  const lastTs = points[points.length - 1].tsMs;
  const diffs = points
    .slice(1)
    .map((point, index) => point.tsMs - points[index].tsMs)
    .filter((diff) => diff > 0 && diff <= 1000 * 60 * 60 * 12);
  const medianStepMs = median(diffs) ?? 1000 * 60 * 60;
  const baseStepMs = diffs.length > 0 ? Math.min(...diffs) : 1000 * 60 * 60;
  const spanMs = Math.max(0, lastTs - firstTs);
  const intervalMinutes = Math.max(1, medianStepMs / (1000 * 60));
  const horizonHours = Math.max(0, (spanMs + baseStepMs) / (1000 * 60 * 60));
  const knownPriceCutoffMs = computeKnownPriceCutoffMs(firstTs);
  const splitIndex = points.findIndex((point) => point.tsMs > knownPriceCutoffMs);
  const priceSplitIndex = splitIndex < 0 ? points.length : splitIndex;
  const knownPriceHours = Math.max(0, (Math.min(knownPriceCutoffMs, lastTs) - firstTs) / HOUR_MS);

  return {
    points,
    windowStartMs: firstTs,
    windowEndMs: Math.max(firstTs + 1000 * 60 * 30, lastTs),
    priceSplitIndex,
    knownPriceHours,
    intervalMinutes,
    horizonHours,
    hasPrice: points.some((point) => point.priceCtPerKwh !== null),
    hasPv: points.some((point) => point.pvAcKw !== null || point.pvDcKw !== null),
    hasLoad: points.some((point) => point.loadKw !== null),
  };
}

function colorForResource(resourceId: string, orderedResources: string[]): string {
  const index = Math.max(0, orderedResources.indexOf(resourceId));
  return RESOURCE_COLORS[index % RESOURCE_COLORS.length];
}

function colorForMode(mode: string, modeLegend: string[]): string {
  const index = Math.max(0, modeLegend.indexOf(mode));
  return MODE_COLORS[index % MODE_COLORS.length];
}

function buildResourceDisplayNameMap(configPayload: Record<string, unknown> | null): Map<string, string> {
  const map = new Map<string, string>();
  const config = asRecord(configPayload);
  const devices = asRecord(config?.devices);
  if (!devices) {
    return map;
  }

  const setAlias = (alias: string, label: string) => {
    const normalizedAlias = normalizeResourceKey(alias);
    if (normalizedAlias === "") {
      return;
    }
    map.set(normalizedAlias, label);
  };

  const addAliasesForCollection = (
    items: unknown[],
    aliasFactory: (index: number) => string[],
  ) => {
    items.forEach((rawItem, index) => {
      const item = asRecord(rawItem);
      if (!item) {
        return;
      }
      const deviceId = typeof item.device_id === "string" ? item.device_id.trim() : "";
      if (deviceId === "") {
        return;
      }
      for (const alias of aliasFactory(index)) {
        setAlias(alias, deviceId);
      }
      setAlias(deviceId, deviceId);
    });
  };

  addAliasesForCollection(Array.isArray(devices.batteries) ? devices.batteries : [], (index) => [
    `battery${index + 1}`,
  ]);
  addAliasesForCollection(Array.isArray(devices.inverters) ? devices.inverters : [], (index) => [
    `inverter${index + 1}`,
  ]);
  addAliasesForCollection(Array.isArray(devices.electric_vehicles) ? devices.electric_vehicles : [], (index) => [
    `ev${index + 1}`,
    `electric_vehicle${index + 1}`,
  ]);
  addAliasesForCollection(Array.isArray(devices.home_appliances) ? devices.home_appliances : [], (index) => [
    `homeappliance${index + 1}`,
    `home_appliance${index + 1}`,
  ]);

  return map;
}

function displayResourceName(resourceId: string, displayNameByResource: Map<string, string>): string {
  return displayNameByResource.get(normalizeResourceKey(resourceId)) ?? resourceId;
}

function ModeTimelineResourceChart({
  resourceId,
  segments,
  modeLegend,
  windowStartMs,
  windowEndMs,
  displayNameByResource,
}: {
  resourceId: string;
  segments: ModeSegment[];
  modeLegend: string[];
  windowStartMs: number;
  windowEndMs: number;
  displayNameByResource: Map<string, string>;
}) {
  const width = 960;
  const height = 136;
  const top = 24;
  const left = 84;
  const right = width - 12;
  const bottom = height - 30;
  const trackHeight = 30;
  const trackY = top + 12;
  const ticks = createTimeTicks(windowStartMs, windowEndMs, 8);
  const spanMs = windowEndMs - windowStartMs;
  const lastSegment = segments.length > 0 ? segments[segments.length - 1] : null;

  return (
    <div className="output-chart-subcard">
      <h5>{displayResourceName(resourceId, displayNameByResource)}</h5>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="output-chart-svg"
        role="img"
        aria-label={`Mode timeline ${resourceId}`}
      >
        <rect x={left} y={trackY} width={right - left} height={trackHeight} fill="rgba(9,23,49,0.52)" rx="5" />
        {ticks.map((tick) => {
          const x = mapX(tick, windowStartMs, windowEndMs, left, right);
          return (
            <g key={`mode-resource-tick-${resourceId}-${tick}`}>
              <line x1={x} y1={trackY} x2={x} y2={trackY + trackHeight} stroke="rgba(86,121,188,0.35)" strokeWidth="1" />
              <text x={x} y={bottom + 16} textAnchor="middle" fill="#9EB0D2" fontSize="11">
                {formatTimeTick(tick, spanMs)}
              </text>
            </g>
          );
        })}
        {segments.map((segment, index) => {
          const clippedStart = Math.max(segment.startMs, windowStartMs);
          const clippedEnd = Math.min(segment.endMs, windowEndMs);
          if (clippedEnd <= clippedStart) {
            return null;
          }
          const xStart = mapX(clippedStart, windowStartMs, windowEndMs, left, right);
          const xEnd = mapX(clippedEnd, windowStartMs, windowEndMs, left, right);
          const segmentWidth = Math.max(2, xEnd - xStart);
          return (
            <rect
              key={`mode-resource-segment-${resourceId}-${index}`}
              x={xStart}
              y={trackY + 3}
              width={segmentWidth}
              height={trackHeight - 6}
              rx={5}
              fill={colorForMode(segment.mode, modeLegend)}
              opacity="0.78"
            />
          );
        })}
        {lastSegment !== null && lastSegment.endMs < windowEndMs ? (
          (() => {
            const inferredStart = Math.max(lastSegment.endMs, windowStartMs);
            if (inferredStart >= windowEndMs) {
              return null;
            }
            const inferredXStart = mapX(inferredStart, windowStartMs, windowEndMs, left, right);
            const inferredXEnd = mapX(windowEndMs, windowStartMs, windowEndMs, left, right);
            const inferredWidth = Math.max(0, inferredXEnd - inferredXStart);
            if (inferredWidth <= 0) {
              return null;
            }
            return (
              <rect
                x={inferredXStart}
                y={trackY + 4}
                width={inferredWidth}
                height={trackHeight - 8}
                fill={colorForMode(lastSegment.mode, modeLegend)}
                opacity="0.32"
                stroke="rgba(230,239,255,0.65)"
                strokeWidth="1"
                strokeDasharray="4 3"
              />
            );
          })()
        ) : null}
      </svg>
    </div>
  );
}

function ModeTimelineChart({
  model,
  windowStartMs,
  windowEndMs,
  displayNameByResource,
}: {
  model: ChartModel;
  windowStartMs: number;
  windowEndMs: number;
  displayNameByResource: Map<string, string>;
}) {
  const [isHelpOpen, setIsHelpOpen] = useState<boolean>(() => {
    if (typeof window === "undefined") {
      return false;
    }
    try {
      return window.localStorage.getItem(MODE_TIMELINE_HELP_OPEN_STORAGE_KEY) === "1";
    } catch {
      return false;
    }
  });
  const visibleModes = model.modeLegend;
  const modeEntriesByResource = useMemo(() => {
    return model.resourceOrder
      .map((resourceId) => {
        const resourceSegments = model.modeSegments
          .filter((segment) => segment.resourceId === resourceId)
          .sort((left, right) => left.startMs - right.startMs);
        return [resourceId, resourceSegments] as const;
      })
      .filter((entry) => entry[1].length > 0);
  }, [model.modeSegments, model.resourceOrder]);

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    try {
      window.localStorage.setItem(MODE_TIMELINE_HELP_OPEN_STORAGE_KEY, isHelpOpen ? "1" : "0");
    } catch {
      // ignore localStorage write errors
    }
  }, [isHelpOpen]);

  return (
    <section className="output-chart-card">
      <h4>Mode-Fahrplan je Resource</h4>
      <p className="meta-text">
        Horizont aus aktuellem Prediction-/Planungsfenster (x-Achse: lokale Zeit). Wenn danach keine neue
        EOS-Instruktion folgt, wird der letzte Status gestrichelt bis zum Horizont fortgeschrieben.
      </p>
      <details
        className="field-help"
        open={isHelpOpen}
        onToggle={(event) => setIsHelpOpen((event.currentTarget as HTMLDetailsElement).open)}
      >
        <summary>Was bedeuten die aktuell sichtbaren Mode-Status?</summary>
        <div className="field-help-content">
          <p>Angezeigt werden nur Mode-IDs, die in diesem Chart aktuell sichtbar sind.</p>
          <ul className="plain-list">
            {visibleModes.length > 0 ? (
              visibleModes.map((mode) => {
                const help = MODE_HELP_BY_ID[mode];
                if (!help) {
                  return (
                    <li key={`mode-help-${mode}`}>
                      <strong>{mode}:</strong> Mode unbekannt im aktuellen Hilfekatalog; technische Details im Plan/Runtime
                      JSON pruefen.
                    </li>
                  );
                }
                return (
                  <li key={`mode-help-${mode}`}>
                    <strong>{mode}:</strong> {help.meaning} Wirkung: {help.practicalEffect}
                  </li>
                );
              })
            ) : (
              <li>Aktuell sind keine Mode-IDs sichtbar.</li>
            )}
          </ul>
          <p>
            <strong>Hinweis:</strong> <code>operation_mode_factor</code> ist ein Skalierungsfaktor von <code>0..1</code> fur den
            aktiven Modus.
          </p>
        </div>
      </details>
      {modeEntriesByResource.length === 0 ? (
        <p className="meta-text">Keine verwertbaren Timeline-Segmente vorhanden.</p>
      ) : null}
      {modeEntriesByResource.length > 0 ? (
        <div className="output-chart-stack">
          {modeEntriesByResource.map(([resourceId, resourceSegments]) => (
            <ModeTimelineResourceChart
              key={`mode-resource-${resourceId}`}
              resourceId={resourceId}
              segments={resourceSegments}
              modeLegend={model.modeLegend}
              windowStartMs={windowStartMs}
              windowEndMs={windowEndMs}
              displayNameByResource={displayNameByResource}
            />
          ))}
        </div>
      ) : null}
      <div className="chart-legend">
        {model.modeLegend.map((mode) => (
          <span key={mode} className="legend-item">
            <i style={{ backgroundColor: colorForMode(mode, model.modeLegend) }} />
            <span>{mode}</span>
          </span>
        ))}
      </div>
    </section>
  );
}

function FactorTimelineResourceChart({
  resourceId,
  rows,
  actualRows,
  windowStartMs,
  windowEndMs,
  nowMs,
  color,
  displayNameByResource,
}: {
  resourceId: string;
  rows: PowerPoint[];
  actualRows: NumericSeriesPoint[];
  windowStartMs: number;
  windowEndMs: number;
  nowMs: number;
  color: string;
  displayNameByResource: Map<string, string>;
}) {
  const width = 960;
  const height = 260;
  const top = 18;
  const left = 56;
  const right = width - 12;
  const bottom = height - 28;
  const ticks = createTimeTicks(windowStartMs, windowEndMs, 8);
  const spanMs = windowEndMs - windowStartMs;
  const actualHistoryRows = dedupeSeriesRows(
    actualRows.filter((row) => row.tsMs >= windowStartMs && row.tsMs <= Math.min(windowEndMs, nowMs)),
  );
  const allValues = [0, ...rows.map((row) => row.value), ...actualHistoryRows.map((row) => row.value)];
  const rawMin = Math.min(...allValues);
  const rawMax = Math.max(...allValues);
  let yMin = rawMin;
  let yMax = rawMax;
  if (yMax <= yMin) {
    yMin -= 1;
    yMax += 1;
  }
  const range = yMax - yMin;
  const padding = range * 0.08;
  yMin -= padding;
  yMax += padding;
  const yTicks = createValueTicks(yMin, yMax, 6);
  const zeroY = mapY(0, yMin, yMax, top, bottom);
  const points = buildStepPolyline(rows, {
    windowStartMs,
    windowEndMs,
    left,
    right,
    top,
    bottom,
    yMin,
    yMax,
  });
  const actualPoints = buildStepPolyline(actualHistoryRows, {
    windowStartMs,
    windowEndMs,
    left,
    right,
    top,
    bottom,
    yMin,
    yMax,
  });
  const sortedRows = dedupeSeriesRows([...rows]).sort((leftRow, rightRow) => leftRow.tsMs - rightRow.tsMs);
  const lastRow = sortedRows.length > 0 ? sortedRows[sortedRows.length - 1] : null;
  const hasInferredExtension = lastRow !== null && lastRow.tsMs < windowEndMs;
  const inferredXStart =
    hasInferredExtension && lastRow !== null
      ? mapX(lastRow.tsMs, windowStartMs, windowEndMs, left, right)
      : null;
  const inferredXEnd = hasInferredExtension ? mapX(windowEndMs, windowStartMs, windowEndMs, left, right) : null;
  const inferredY =
    hasInferredExtension && lastRow !== null ? mapY(lastRow.value, yMin, yMax, top, bottom) : null;

  return (
    <div className="output-chart-subcard">
      <h5>{displayResourceName(resourceId, displayNameByResource)}</h5>
      <svg viewBox={`0 0 ${width} ${height}`} className="output-chart-svg" role="img" aria-label="Factor timeline chart">
        {yTicks.map((tick) => {
          const y = mapY(tick, yMin, yMax, top, bottom);
          return (
            <g key={`factor-y-${tick}`}>
              <line x1={left} y1={y} x2={right} y2={y} stroke="rgba(86,121,188,0.28)" strokeWidth="1" />
              <text x={left - 8} y={y + 4} textAnchor="end" fill="#9EB0D2" fontSize="11">
                {tick.toFixed(2)}
              </text>
            </g>
          );
        })}
        <line x1={left} y1={zeroY} x2={right} y2={zeroY} stroke="rgba(255,191,117,0.8)" strokeWidth="1.6" />
        {ticks.map((tick) => {
          const x = mapX(tick, windowStartMs, windowEndMs, left, right);
          return (
            <g key={`factor-x-${tick}`}>
              <line x1={x} y1={top} x2={x} y2={bottom} stroke="rgba(86,121,188,0.2)" strokeWidth="1" />
              <text x={x} y={height - 10} textAnchor="middle" fill="#9EB0D2" fontSize="11">
                {formatTimeTick(tick, spanMs)}
              </text>
            </g>
          );
        })}
        {points !== "" ? <polyline points={points} fill="none" stroke={color} strokeWidth="2.4" /> : null}
        {actualPoints !== "" ? (
          <polyline
            points={actualPoints}
            fill="none"
            stroke={FACTOR_ACTUAL_COLOR}
            strokeWidth="2.2"
            strokeDasharray="3 2"
          />
        ) : null}
        {hasInferredExtension && inferredXStart !== null && inferredXEnd !== null && inferredY !== null ? (
          <line
            x1={inferredXStart}
            y1={inferredY}
            x2={inferredXEnd}
            y2={inferredY}
            stroke={color}
            strokeWidth="2"
            strokeDasharray="5 4"
            opacity="0.85"
          />
        ) : null}
      </svg>
    </div>
  );
}

function FactorTimelineChart({
  model,
  actualHistoryByResource,
  windowStartMs,
  windowEndMs,
  nowMs,
  displayNameByResource,
}: {
  model: ChartModel;
  actualHistoryByResource: Record<string, NumericSeriesPoint[]>;
  windowStartMs: number;
  windowEndMs: number;
  nowMs: number;
  displayNameByResource: Map<string, string>;
}) {
  const powerEntries = model.resourceOrder
    .map(
      (resourceId) =>
        [resourceId, model.powerSeries[resourceId] ?? [], actualHistoryByResource[resourceId] ?? []] as const,
    )
    .filter((entry) => entry[1].length > 0 || entry[2].length > 0);

  if (powerEntries.length === 0) {
    return (
      <section className="output-chart-card">
        <h4>Soll-Leistung je Resource (kW, Treppenstufen)</h4>
        <p className="meta-text">Keine ableitbaren Soll-/Ist-Leistungswerte fuer die aktuellen Ressourcen verfuegbar.</p>
      </section>
    );
  }

  return (
    <section className="output-chart-card">
      <h4>Soll-Leistung je Resource (kW, Treppenstufen)</h4>
      <p className="meta-text">
        Mode-basiert aus `operation_mode_factor` und konfigurierter Max-Leistung: `+` Laden/Import, `-` Entladen/Export,
        `0` kein aktiver Leistungsauftrag. Fur Batterien werden nur explizite Entlade-/Export-Phasen als Soll gezeigt.
        Tuerkis gestrichelt zeigt den gemessenen Istwert in der Vergangenheit; gestrichelte Soll-Abschnitte markieren
        die Fortfuhrung des letzten Sollwerts ohne neue EOS-Instruktion.
      </p>
      <div className="chart-legend">
        <span className="legend-item">
          <i style={{ backgroundColor: "#7DA6FF" }} />
          <span>Soll (resource-farbig)</span>
        </span>
        <span className="legend-item">
          <i style={{ backgroundColor: FACTOR_ACTUAL_COLOR }} />
          <span>Ist (gemessen, Vergangenheit)</span>
        </span>
      </div>
      <div className="output-chart-stack">
        {powerEntries.map(([resourceId, rows, actualRows]) => (
          <FactorTimelineResourceChart
            key={`factor-series-${resourceId}`}
            resourceId={resourceId}
            rows={rows}
            actualRows={actualRows}
            windowStartMs={windowStartMs}
            windowEndMs={windowEndMs}
            nowMs={nowMs}
            color={colorForResource(resourceId, model.resourceOrder)}
            displayNameByResource={displayNameByResource}
          />
        ))}
      </div>
    </section>
  );
}

function PriceTimelineChart({
  model,
  history,
  windowStartMs,
  windowEndMs,
  nowMs,
}: {
  model: PredictionChartModel;
  history: PredictionHistoryModel;
  windowStartMs: number;
  windowEndMs: number;
  nowMs: number;
}) {
  const width = 960;
  const height = 320;
  const top = 18;
  const left = 56;
  const right = width - 12;
  const bottom = height - 28;
  const ticks = createTimeTicks(windowStartMs, windowEndMs, 8);
  const spanMs = windowEndMs - windowStartMs;

  const predictionPriceRows = model.points
    .filter((point): point is PredictionPoint & { priceCtPerKwh: number } => point.priceCtPerKwh !== null)
    .map((point) => ({ tsMs: point.tsMs, value: point.priceCtPerKwh }));
  const historyPriceRows = history.priceRows;

  if (predictionPriceRows.length === 0 && historyPriceRows.length === 0) {
    return (
      <section className="output-chart-card">
        <h4>Strompreis-Verlauf</h4>
        <p className="meta-text">Keine Preisdaten fuer Vergangenheit/Prognose verfuegbar.</p>
      </section>
    );
  }

  const values = [...predictionPriceRows, ...historyPriceRows].map((row) => row.value);
  const rawMin = Math.min(...values);
  const rawMax = Math.max(...values);
  const yMin = rawMax > rawMin ? Math.floor(rawMin * 10) / 10 : rawMin - 1;
  const yMax = rawMax > rawMin ? Math.ceil(rawMax * 10) / 10 : rawMax + 1;
  const yTicks = createValueTicks(yMin, yMax, 5);

  const realRows = model.points
    .slice(0, model.priceSplitIndex)
    .filter((point): point is PredictionPoint & { priceCtPerKwh: number } => point.priceCtPerKwh !== null)
    .map((point) => ({ tsMs: point.tsMs, value: point.priceCtPerKwh }));
  const forecastRowsRaw = model.points
    .slice(Math.max(0, model.priceSplitIndex - 1))
    .filter((point): point is PredictionPoint & { priceCtPerKwh: number } => point.priceCtPerKwh !== null)
    .map((point) => ({ tsMs: point.tsMs, value: point.priceCtPerKwh }));
  const ingestedRows = normalizePriceSeriesRows(mergeSeriesPreferSecond(historyPriceRows, realRows));
  const forecastRows = normalizePriceSeriesRows(forecastRowsRaw);

  const ingestedPolyline = buildStepPolyline(ingestedRows, {
    windowStartMs,
    windowEndMs,
    left,
    right,
    top,
    bottom,
    yMin,
    yMax,
  });
  const forecastPolyline = buildStepPolyline(forecastRows, {
    windowStartMs,
    windowEndMs,
    left,
    right,
    top,
    bottom,
    yMin,
    yMax,
  });
  const nowInWindow = windowEndMs > windowStartMs && nowMs >= windowStartMs && nowMs <= windowEndMs;
  const nowXRaw = nowInWindow ? mapX(nowMs, windowStartMs, windowEndMs, left, right) : null;
  const nowVisible = nowXRaw !== null && Number.isFinite(nowXRaw);
  const nowX = nowVisible ? nowXRaw : null;
  const nowLabel = nowVisible
    ? new Date(nowMs).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      })
    : null;
  const nowLabelX = nowX === null ? null : Math.max(left + 34, Math.min(right - 34, nowX));

  return (
    <section className="output-chart-card">
      <h4>Strompreis-Verlauf (ct/kWh)</h4>
      <p className="meta-text">
        Eingelesen/Day-Ahead: DB-Vergangenheit ab {HISTORY_START_LABEL} plus boersenbekannter Horizont
        (heute 24:00, nach Mittagsveroeffentlichung bis morgen 24:00). Darstellung als Treppenstufen in 15min-Basis.
        Vertikale Linie markiert die aktuelle Zeit.
      </p>
      <svg viewBox={`0 0 ${width} ${height}`} className="output-chart-svg" role="img" aria-label="Electricity price chart">
        {yTicks.map((tick) => {
          const y = mapY(tick, yMin, yMax, top, bottom);
          return (
            <g key={`price-y-${tick}`}>
              <line x1={left} y1={y} x2={right} y2={y} stroke="rgba(86,121,188,0.28)" strokeWidth="1" />
              <text x={left - 8} y={y + 4} textAnchor="end" fill="#9EB0D2" fontSize="11">
                {tick.toFixed(2)}
              </text>
            </g>
          );
        })}
        {ticks.map((tick) => {
          const x = mapX(tick, windowStartMs, windowEndMs, left, right);
          return (
            <g key={`price-x-${tick}`}>
              <line x1={x} y1={top} x2={x} y2={bottom} stroke="rgba(86,121,188,0.2)" strokeWidth="1" />
              <text x={x} y={height - 10} textAnchor="middle" fill="#9EB0D2" fontSize="11">
                {formatTimeTick(tick, spanMs)}
              </text>
            </g>
          );
        })}
        {ingestedPolyline !== "" ? (
          <polyline
            points={ingestedPolyline}
            fill="none"
            stroke={PRICE_REAL_COLOR}
            strokeWidth="2.6"
            strokeLinecap="butt"
            strokeLinejoin="miter"
            shapeRendering="geometricPrecision"
          />
        ) : null}
        {forecastPolyline !== "" ? (
          <polyline
            points={forecastPolyline}
            fill="none"
            stroke={PRICE_FORECAST_COLOR}
            strokeWidth="2.6"
            strokeDasharray="6 4"
            strokeLinecap="butt"
            strokeLinejoin="miter"
            shapeRendering="geometricPrecision"
          />
        ) : null}
        {nowX !== null ? (
          <g>
            <line
              x1={nowX}
              y1={top}
              x2={nowX}
              y2={bottom}
              stroke={PRICE_NOW_COLOR}
              strokeWidth="1.8"
              strokeDasharray="4 4"
            />
            {nowLabelX !== null && nowLabel !== null ? (
              <text x={nowLabelX} y={top + 12} textAnchor="middle" fill={PRICE_NOW_COLOR} fontSize="11" fontWeight="600">
                {`Jetzt ${nowLabel}`}
              </text>
            ) : null}
          </g>
        ) : null}
      </svg>
      <div className="chart-legend">
        <span className="legend-item">
          <i style={{ backgroundColor: PRICE_REAL_COLOR }} />
          <span>Eingelesen / Day-Ahead (Treppenstufen)</span>
        </span>
        <span className="legend-item">
          <i style={{ backgroundColor: PRICE_FORECAST_COLOR }} />
          <span>Forecast (weiterer Horizont, Treppenstufen)</span>
        </span>
        <span className="legend-item">
          <i style={{ backgroundColor: PRICE_NOW_COLOR }} />
          <span>Jetzt</span>
        </span>
      </div>
    </section>
  );
}

function PvForecastChart({
  model,
  history,
  windowStartMs,
  windowEndMs,
}: {
  model: PredictionChartModel;
  history: PredictionHistoryModel;
  windowStartMs: number;
  windowEndMs: number;
}) {
  const width = 960;
  const height = 320;
  const top = 18;
  const left = 56;
  const right = width - 12;
  const bottom = height - 28;
  const ticks = createTimeTicks(windowStartMs, windowEndMs, 8);
  const spanMs = windowEndMs - windowStartMs;

  const acRows = model.points
    .filter((point): point is PredictionPoint & { pvAcKw: number } => point.pvAcKw !== null)
    .map((point) => ({ tsMs: point.tsMs, value: point.pvAcKw }));
  const dcRows = model.points
    .filter((point): point is PredictionPoint & { pvDcKw: number } => point.pvDcKw !== null)
    .map((point) => ({ tsMs: point.tsMs, value: point.pvDcKw }));
  const actualRows = history.pvRows;
  const historyForecastRows = history.pvForecastRows;
  const mergedForecastRows = mergeSeriesPreferSecond(historyForecastRows, acRows);

  const allValues = [
    ...mergedForecastRows.map((row) => row.value),
    ...dcRows.map((row) => row.value),
    ...actualRows.map((row) => row.value),
  ];
  if (allValues.length === 0) {
    return (
      <section className="output-chart-card">
        <h4>PV-Produktionsprognose (kW)</h4>
        <p className="meta-text">Keine PV-Daten fuer Vergangenheit/Prognose verfuegbar.</p>
      </section>
    );
  }

  const yMax = Math.max(0.1, Math.ceil(Math.max(...allValues) * 10) / 10);
  const yTicks = createValueTicks(0, yMax, 5);

  const acPolyline = mergedForecastRows
    .map((row) => {
      const x = mapX(row.tsMs, windowStartMs, windowEndMs, left, right);
      const y = mapY(row.value, 0, yMax, top, bottom);
      return `${x},${y}`;
    })
    .join(" ");
  const dcPolyline = dcRows
    .map((row) => {
      const x = mapX(row.tsMs, windowStartMs, windowEndMs, left, right);
      const y = mapY(row.value, 0, yMax, top, bottom);
      return `${x},${y}`;
    })
    .join(" ");
  const actualPolyline = actualRows
    .map((row) => {
      const x = mapX(row.tsMs, windowStartMs, windowEndMs, left, right);
      const y = mapY(row.value, 0, yMax, top, bottom);
      return `${x},${y}`;
    })
    .join(" ");

  return (
    <section className="output-chart-card">
      <h4>PV-Produktionsprognose (kW)</h4>
      <p className="meta-text">
        Historische Istwerte (DB, ab {HISTORY_START_LABEL}) und Forecast (Vergangenheit aus DB + Zukunft aus
        gewaehltem Run).
      </p>
      <svg viewBox={`0 0 ${width} ${height}`} className="output-chart-svg" role="img" aria-label="PV forecast chart">
        {yTicks.map((tick) => {
          const y = mapY(tick, 0, yMax, top, bottom);
          return (
            <g key={`pv-y-${tick}`}>
              <line x1={left} y1={y} x2={right} y2={y} stroke="rgba(86,121,188,0.28)" strokeWidth="1" />
              <text x={left - 8} y={y + 4} textAnchor="end" fill="#9EB0D2" fontSize="11">
                {tick.toFixed(2)}
              </text>
            </g>
          );
        })}
        {ticks.map((tick) => {
          const x = mapX(tick, windowStartMs, windowEndMs, left, right);
          return (
            <g key={`pv-x-${tick}`}>
              <line x1={x} y1={top} x2={x} y2={bottom} stroke="rgba(86,121,188,0.2)" strokeWidth="1" />
              <text x={x} y={height - 10} textAnchor="middle" fill="#9EB0D2" fontSize="11">
                {formatTimeTick(tick, spanMs)}
              </text>
            </g>
          );
        })}
        {actualPolyline !== "" ? (
          <polyline points={actualPolyline} fill="none" stroke={PV_ACTUAL_COLOR} strokeWidth="2.2" />
        ) : null}
        {acPolyline !== "" ? <polyline points={acPolyline} fill="none" stroke={PV_AC_COLOR} strokeWidth="2.6" /> : null}
        {dcPolyline !== "" ? (
          <polyline
            points={dcPolyline}
            fill="none"
            stroke={PV_DC_COLOR}
            strokeWidth="2.2"
            strokeDasharray="5 4"
          />
        ) : null}
      </svg>
      <div className="chart-legend">
        {actualRows.length > 0 ? (
          <span className="legend-item">
            <i style={{ backgroundColor: PV_ACTUAL_COLOR }} />
            <span>PV Ist (kW)</span>
          </span>
        ) : null}
        <span className="legend-item">
          <i style={{ backgroundColor: PV_AC_COLOR }} />
          <span>PV Forecast AC (kW)</span>
        </span>
        <span className="legend-item">
          <i style={{ backgroundColor: PV_DC_COLOR }} />
          <span>PV DC (kW)</span>
        </span>
      </div>
    </section>
  );
}

function LoadForecastChart({
  model,
  history,
  windowStartMs,
  windowEndMs,
}: {
  model: PredictionChartModel;
  history: PredictionHistoryModel;
  windowStartMs: number;
  windowEndMs: number;
}) {
  const width = 960;
  const height = 320;
  const top = 18;
  const left = 56;
  const right = width - 12;
  const bottom = height - 28;
  const ticks = createTimeTicks(windowStartMs, windowEndMs, 8);
  const spanMs = windowEndMs - windowStartMs;

  const loadRows = model.points
    .filter((point): point is PredictionPoint & { loadKw: number } => point.loadKw !== null)
    .map((point) => ({ tsMs: point.tsMs, value: point.loadKw }));
  const actualRows = history.loadRows;
  const historyForecastRows = history.loadForecastRows;
  const mergedForecastRows = mergeSeriesPreferSecond(historyForecastRows, loadRows);

  if (mergedForecastRows.length === 0 && actualRows.length === 0) {
    return (
      <section className="output-chart-card">
        <h4>Haushaltslast: Istwerte vs. Prognose (kW)</h4>
        <p className="meta-text">Keine Lastdaten fuer Vergangenheit/Prognose verfuegbar.</p>
      </section>
    );
  }

  const allValues = [...mergedForecastRows.map((row) => row.value), ...actualRows.map((row) => row.value)];
  const yMax = Math.max(0.1, Math.ceil(Math.max(...allValues) * 10) / 10);
  const yTicks = createValueTicks(0, yMax, 5);
  const forecastPolyline = mergedForecastRows
    .map((row) => {
      const x = mapX(row.tsMs, windowStartMs, windowEndMs, left, right);
      const y = mapY(row.value, 0, yMax, top, bottom);
      return `${x},${y}`;
    })
    .join(" ");
  const actualPolyline = actualRows
    .map((row) => {
      const x = mapX(row.tsMs, windowStartMs, windowEndMs, left, right);
      const y = mapY(row.value, 0, yMax, top, bottom);
      return `${x},${y}`;
    })
    .join(" ");

  return (
    <section className="output-chart-card">
      <h4>Haushaltslast: Istwerte vs. Prognose (kW)</h4>
      <p className="meta-text">
        Vergleich von realer Haushaltslast (DB-Historie, ab {HISTORY_START_LABEL}) und Load-Forecast
        (Vergangenheit aus DB + Zukunft aus gewaehltem Run).
      </p>
      <svg viewBox={`0 0 ${width} ${height}`} className="output-chart-svg" role="img" aria-label="Load forecast chart">
        {yTicks.map((tick) => {
          const y = mapY(tick, 0, yMax, top, bottom);
          return (
            <g key={`load-y-${tick}`}>
              <line x1={left} y1={y} x2={right} y2={y} stroke="rgba(86,121,188,0.28)" strokeWidth="1" />
              <text x={left - 8} y={y + 4} textAnchor="end" fill="#9EB0D2" fontSize="11">
                {tick.toFixed(2)}
              </text>
            </g>
          );
        })}
        {ticks.map((tick) => {
          const x = mapX(tick, windowStartMs, windowEndMs, left, right);
          return (
            <g key={`load-x-${tick}`}>
              <line x1={x} y1={top} x2={x} y2={bottom} stroke="rgba(86,121,188,0.2)" strokeWidth="1" />
              <text x={x} y={height - 10} textAnchor="middle" fill="#9EB0D2" fontSize="11">
                {formatTimeTick(tick, spanMs)}
              </text>
            </g>
          );
        })}
        {actualPolyline !== "" ? (
          <polyline points={actualPolyline} fill="none" stroke={LOAD_ACTUAL_COLOR} strokeWidth="2.2" />
        ) : null}
        {forecastPolyline !== "" ? (
          <polyline points={forecastPolyline} fill="none" stroke={LOAD_FORECAST_COLOR} strokeWidth="2.6" />
        ) : null}
      </svg>
      <div className="chart-legend">
        {actualRows.length > 0 ? (
          <span className="legend-item">
            <i style={{ backgroundColor: LOAD_ACTUAL_COLOR }} />
            <span>Haushaltslast Ist (kW)</span>
          </span>
        ) : null}
        <span className="legend-item">
          <i style={{ backgroundColor: LOAD_FORECAST_COLOR }} />
          <span>Haushaltslast-Prognose (kW)</span>
        </span>
      </div>
    </section>
  );
}

export function OutputChartsPanel({
  runId,
  timeline,
  current,
  configPayload,
  solutionPayload,
  predictionSeries,
}: OutputChartsPanelProps) {
  const maxPowerKwByResource = useMemo(() => extractResourceMaxPowerKw(configPayload), [configPayload]);
  const displayNameByResource = useMemo(() => buildResourceDisplayNameMap(configPayload), [configPayload]);
  const model = useMemo(
    () => buildChartModel(timeline, current, maxPowerKwByResource),
    [timeline, current, maxPowerKwByResource],
  );
  const actualSignalPlansByResource = useMemo(
    () => resolveResourceActualSignalPlans(configPayload, model.resourceOrder),
    [configPayload, model.resourceOrder],
  );
  const predictionModel = useMemo(
    () => buildPredictionModel(solutionPayload, predictionSeries),
    [solutionPayload, predictionSeries],
  );
  const [historyModel, setHistoryModel] = useState<PredictionHistoryModel>(EMPTY_PREDICTION_HISTORY);
  const [actualPowerHistory, setActualPowerHistory] = useState<ActualPowerHistoryModel>(EMPTY_ACTUAL_POWER_HISTORY);
  const [nowMs, setNowMs] = useState<number>(() => Date.now());
  const predictionWindow = useMemo(
    () => mergePredictionWindow(predictionModel, historyModel),
    [historyModel, predictionModel],
  );
  const instructionWindow = useMemo(
    () =>
      predictionModel.points.length > 0
        ? { startMs: predictionModel.windowStartMs, endMs: predictionModel.windowEndMs }
        : { startMs: model.windowStartMs, endMs: model.windowEndMs },
    [
      predictionModel.points.length,
      predictionModel.windowStartMs,
      predictionModel.windowEndMs,
      model.windowStartMs,
      model.windowEndMs,
    ],
  );
  const factorWindow = useMemo(
    () => mergeFactorWindow(instructionWindow, actualPowerHistory),
    [instructionWindow, actualPowerHistory],
  );
  const hasAnyData =
    model.hasAnyData ||
    predictionModel.hasPrice ||
    predictionModel.hasPv ||
    predictionModel.hasLoad ||
    historyModel.hasPrice ||
    historyModel.hasPv ||
    historyModel.hasLoad ||
    actualPowerHistory.hasAny;
  const [isOpen, setIsOpen] = useState<boolean>(() => {
    if (typeof window === "undefined") {
      return true;
    }
    try {
      const raw = window.localStorage.getItem(OUTPUT_CHARTS_OPEN_STORAGE_KEY);
      if (raw === "0") {
        return false;
      }
      if (raw === "1") {
        return true;
      }
    } catch {
      // ignore localStorage read errors
    }
    return true;
  });

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    try {
      window.localStorage.setItem(OUTPUT_CHARTS_OPEN_STORAGE_KEY, isOpen ? "1" : "0");
    } catch {
      // ignore localStorage write errors
    }
  }, [isOpen]);

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    setNowMs(Date.now());
    const interval = window.setInterval(() => {
      setNowMs(Date.now());
    }, 30000);
    return () => {
      window.clearInterval(interval);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const loadHistory = async () => {
      try {
        const next = await loadPredictionHistory();
        if (!cancelled) {
          setHistoryModel(next);
        }
      } catch {
        if (!cancelled) {
          setHistoryModel(EMPTY_PREDICTION_HISTORY);
        }
      }
    };

    void loadHistory();
    const interval = window.setInterval(() => {
      void loadHistory();
    }, 60000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [runId]);

  useEffect(() => {
    let cancelled = false;
    const loadActualHistory = async () => {
      try {
        const next = await loadActualPowerHistory(actualSignalPlansByResource);
        if (!cancelled) {
          setActualPowerHistory(next);
        }
      } catch {
        if (!cancelled) {
          setActualPowerHistory(EMPTY_ACTUAL_POWER_HISTORY);
        }
      }
    };

    void loadActualHistory();
    const interval = window.setInterval(() => {
      void loadActualHistory();
    }, 60000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [runId, actualSignalPlansByResource]);

  return (
    <div className="panel">
      <details
        className="output-charts-panel"
        open={isOpen}
        onToggle={(event) => setIsOpen((event.currentTarget as HTMLDetailsElement).open)}
      >
        <summary>
          <strong>Charts: Entscheidungen mit Zeitbezug</strong>
          {runId !== null ? ` | Run #${runId}` : ""}
          {predictionModel.points.length > 0 ? ` | Prediction-Horizont ${predictionModel.horizonHours.toFixed(1)}h` : ""}
        </summary>
        <p className="meta-text">
          Visualisierung orientiert sich am EOSdash-Prinzip aus dem Prediction-Tab: getrennte Fachcharts mit gemeinsamer
          Zeitachse, inkl. DB-Historie ab {HISTORY_START_LABEL} (falls vorhanden).
        </p>
        {!hasAnyData ? <p>Keine Chart-Daten fur den ausgewahlten Run verfugbar.</p> : null}
        {hasAnyData ? (
          <div className="output-chart-grid">
            <PriceTimelineChart
              model={predictionModel}
              history={historyModel}
              windowStartMs={predictionWindow.startMs}
              windowEndMs={predictionWindow.endMs}
              nowMs={nowMs}
            />
            <PvForecastChart
              model={predictionModel}
              history={historyModel}
              windowStartMs={predictionWindow.startMs}
              windowEndMs={predictionWindow.endMs}
            />
            <LoadForecastChart
              model={predictionModel}
              history={historyModel}
              windowStartMs={predictionWindow.startMs}
              windowEndMs={predictionWindow.endMs}
            />
            <ModeTimelineChart
              model={model}
              windowStartMs={predictionWindow.startMs}
              windowEndMs={predictionWindow.endMs}
              displayNameByResource={displayNameByResource}
            />
            <FactorTimelineChart
              model={model}
              actualHistoryByResource={actualPowerHistory.byResource}
              windowStartMs={factorWindow.startMs}
              windowEndMs={factorWindow.endMs}
              nowMs={nowMs}
              displayNameByResource={displayNameByResource}
            />
          </div>
        ) : null}
      </details>
    </div>
  );
}
