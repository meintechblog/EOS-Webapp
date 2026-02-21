import { useMemo } from "react";

import type { EosOutputCurrentItem, EosOutputTimelineItem, EosRunPredictionSeries } from "./types";

type OutputChartsPanelProps = {
  runId: number | null;
  timeline: EosOutputTimelineItem[];
  current: EosOutputCurrentItem[];
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

type FactorPoint = {
  tsMs: number;
  value: number;
};

type ChartModel = {
  windowStartMs: number;
  windowEndMs: number;
  modeSegments: ModeSegment[];
  modeLegend: string[];
  resourceOrder: string[];
  factorSeries: Record<string, FactorPoint[]>;
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
const PV_AC_COLOR = "#7DA6FF";
const PV_DC_COLOR = "#C6B6FF";
const LOAD_FORECAST_COLOR = "#F9E58B";

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

function createTimeTicks(startMs: number, endMs: number, targetTicks: number): number[] {
  if (endMs <= startMs) {
    return [startMs];
  }
  const count = Math.max(2, targetTicks);
  const step = (endMs - startMs) / (count - 1);
  const ticks: number[] = [];
  for (let index = 0; index < count; index += 1) {
    ticks.push(Math.round(startMs + step * index));
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

function normalizeMode(mode: string | null): string {
  const normalized = (mode ?? "").trim();
  return normalized === "" ? "UNKNOWN" : normalized;
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

function buildFactorSeries(points: TimelinePoint[], current: CurrentPoint[]): Record<string, FactorPoint[]> {
  const series = new Map<string, FactorPoint[]>();

  for (const point of points) {
    if (point.factor === null || !Number.isFinite(point.factor)) {
      continue;
    }
    const rows = series.get(point.resourceId);
    const item = { tsMs: point.tsMs, value: point.factor };
    if (rows) {
      rows.push(item);
    } else {
      series.set(point.resourceId, [item]);
    }
  }

  for (const point of current) {
    if (point.factor === null || !Number.isFinite(point.factor)) {
      continue;
    }
    const rows = series.get(point.resourceId);
    const item = { tsMs: point.tsMs, value: point.factor };
    if (rows) {
      rows.push(item);
    } else {
      series.set(point.resourceId, [item]);
    }
  }

  const normalized: Record<string, FactorPoint[]> = {};
  for (const [resourceId, rows] of series.entries()) {
    rows.sort((left, right) => left.tsMs - right.tsMs);
    const deduped: FactorPoint[] = [];
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

function buildChartModel(timeline: EosOutputTimelineItem[], current: EosOutputCurrentItem[]): ChartModel {
  const timelinePoints = parseTimelinePoints(timeline);
  const currentPoints = parseCurrentPoints(current);
  const { startMs, endMs } = deriveWindow(timelinePoints, currentPoints);
  const modeSegments = buildModeSegments(timelinePoints, endMs);
  const factorSeries = buildFactorSeries(timelinePoints, currentPoints);

  const modeLegend = Array.from(new Set(modeSegments.map((segment) => segment.mode))).sort((left, right) =>
    left.localeCompare(right),
  );

  const resourceFromSegments = modeSegments.map((segment) => segment.resourceId);
  const resourceFromFactors = Object.keys(factorSeries);
  const resourceFromCurrent = currentPoints.map((point) => point.resourceId);
  const resourceOrder = Array.from(new Set([...resourceFromSegments, ...resourceFromFactors, ...resourceFromCurrent]));

  return {
    windowStartMs: startMs,
    windowEndMs: endMs,
    modeSegments,
    modeLegend,
    resourceOrder,
    factorSeries,
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
    const nowMs = Date.now();
    return {
      points: [],
      windowStartMs: nowMs - 1000 * 60 * 60,
      windowEndMs: nowMs + 1000 * 60 * 60 * 8,
      priceSplitIndex: 0,
      knownPriceHours: 24,
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
  const intervalMinutes = Math.max(1, medianStepMs / (1000 * 60));
  const horizonHours = Math.max(0, (points.length * medianStepMs) / (1000 * 60 * 60));
  const knownPriceHours = Math.max(24, Math.min(48, Math.round(horizonHours * 0.5)));
  const pointsPerKnownPrice = Math.max(
    1,
    Math.round((knownPriceHours * 60 * 60 * 1000) / medianStepMs),
  );
  const priceSplitIndex = Math.min(points.length, pointsPerKnownPrice);

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

function ModeTimelineChart({ model }: { model: ChartModel }) {
  const width = 960;
  const rowHeight = 28;
  const top = 24;
  const left = 118;
  const right = width - 12;
  const bottom = 34;
  const rows = Math.max(1, model.resourceOrder.length);
  const height = top + rows * rowHeight + bottom;
  const ticks = createTimeTicks(model.windowStartMs, model.windowEndMs, 6);
  const spanMs = model.windowEndMs - model.windowStartMs;

  return (
    <section className="output-chart-card">
      <h4>Mode-Fahrplan je Resource</h4>
      <p className="meta-text">Horizont aus Run-Timeline und aktuellen Entscheidungen (x-Achse: lokale Zeit).</p>
      {model.modeSegments.length === 0 ? (
        <p className="meta-text">Keine verwertbaren Timeline-Segmente vorhanden.</p>
      ) : null}
      <svg viewBox={`0 0 ${width} ${height}`} className="output-chart-svg" role="img" aria-label="Mode timeline chart">
        <rect x={left} y={top - 6} width={right - left} height={rows * rowHeight + 6} fill="rgba(9,23,49,0.52)" />
        {ticks.map((tick) => {
          const x = mapX(tick, model.windowStartMs, model.windowEndMs, left, right);
          return (
            <g key={`mode-tick-${tick}`}>
              <line x1={x} y1={top - 6} x2={x} y2={top + rows * rowHeight} stroke="rgba(86,121,188,0.35)" strokeWidth="1" />
              <text x={x} y={height - 10} textAnchor="middle" fill="#9EB0D2" fontSize="11">
                {formatTimeTick(tick, spanMs)}
              </text>
            </g>
          );
        })}
        {model.resourceOrder.map((resourceId, index) => {
          const y = top + index * rowHeight + rowHeight / 2;
          return (
            <g key={`mode-row-${resourceId}`}>
              <text x={left - 8} y={y + 4} textAnchor="end" fill="#D8E6FF" fontSize="11">
                {resourceId}
              </text>
              <line x1={left} y1={y + rowHeight / 2 - 1} x2={right} y2={y + rowHeight / 2 - 1} stroke="rgba(64,91,145,0.26)" strokeWidth="1" />
            </g>
          );
        })}
        {model.modeSegments.map((segment, index) => {
          const row = model.resourceOrder.indexOf(segment.resourceId);
          if (row < 0) {
            return null;
          }
          const y = top + row * rowHeight + 5;
          const xStart = mapX(segment.startMs, model.windowStartMs, model.windowEndMs, left, right);
          const xEnd = mapX(segment.endMs, model.windowStartMs, model.windowEndMs, left, right);
          const segmentWidth = Math.max(2, xEnd - xStart);
          return (
            <rect
              key={`mode-segment-${index}`}
              x={xStart}
              y={y}
              width={segmentWidth}
              height={rowHeight - 10}
              rx={5}
              fill={colorForMode(segment.mode, model.modeLegend)}
              opacity="0.76"
            />
          );
        })}
      </svg>
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

function FactorTimelineChart({ model }: { model: ChartModel }) {
  const width = 960;
  const height = 320;
  const top = 18;
  const left = 56;
  const right = width - 12;
  const bottom = height - 28;
  const ticks = createTimeTicks(model.windowStartMs, model.windowEndMs, 6);
  const spanMs = model.windowEndMs - model.windowStartMs;

  const allFactors = Object.values(model.factorSeries).flatMap((rows) => rows.map((row) => row.value));
  const maxFactor = allFactors.reduce((result, value) => Math.max(result, value), 1);
  const yMax = Math.max(1, Math.ceil(maxFactor * 10) / 10);
  const yTicks = [0, yMax * 0.25, yMax * 0.5, yMax * 0.75, yMax];

  return (
    <section className="output-chart-card">
      <h4>Leistungsfaktor-Verlauf je Resource</h4>
      <p className="meta-text">Zeitlicher Verlauf aus den Plan-Instruktionen (`operation_mode_factor`).</p>
      {Object.keys(model.factorSeries).length === 0 ? (
        <p className="meta-text">Keine numerischen Faktorwerte verfugbar.</p>
      ) : null}
      <svg viewBox={`0 0 ${width} ${height}`} className="output-chart-svg" role="img" aria-label="Factor timeline chart">
        {yTicks.map((tick) => {
          const y = mapY(tick, 0, yMax, top, bottom);
          return (
            <g key={`factor-y-${tick}`}>
              <line x1={left} y1={y} x2={right} y2={y} stroke="rgba(86,121,188,0.28)" strokeWidth="1" />
              <text x={left - 8} y={y + 4} textAnchor="end" fill="#9EB0D2" fontSize="11">
                {tick.toFixed(2)}
              </text>
            </g>
          );
        })}
        {ticks.map((tick) => {
          const x = mapX(tick, model.windowStartMs, model.windowEndMs, left, right);
          return (
            <g key={`factor-x-${tick}`}>
              <line x1={x} y1={top} x2={x} y2={bottom} stroke="rgba(86,121,188,0.2)" strokeWidth="1" />
              <text x={x} y={height - 10} textAnchor="middle" fill="#9EB0D2" fontSize="11">
                {formatTimeTick(tick, spanMs)}
              </text>
            </g>
          );
        })}
        {Object.entries(model.factorSeries).map(([resourceId, rows]) => {
          if (rows.length === 0) {
            return null;
          }
          const points = rows
            .map((row) => {
              const x = mapX(row.tsMs, model.windowStartMs, model.windowEndMs, left, right);
              const y = mapY(row.value, 0, yMax, top, bottom);
              return `${x},${y}`;
            })
            .join(" ");
          const color = colorForResource(resourceId, model.resourceOrder);
          return (
            <g key={`factor-series-${resourceId}`}>
              <polyline points={points} fill="none" stroke={color} strokeWidth="2.2" />
              {rows.map((row) => {
                const x = mapX(row.tsMs, model.windowStartMs, model.windowEndMs, left, right);
                const y = mapY(row.value, 0, yMax, top, bottom);
                return <circle key={`factor-dot-${resourceId}-${row.tsMs}`} cx={x} cy={y} r="2.8" fill={color} />;
              })}
            </g>
          );
        })}
        {model.currentPoints.map((point) => {
          if (point.factor === null || !Number.isFinite(point.factor)) {
            return null;
          }
          const x = mapX(point.tsMs, model.windowStartMs, model.windowEndMs, left, right);
          const y = mapY(point.factor, 0, yMax, top, bottom);
          const color = colorForResource(point.resourceId, model.resourceOrder);
          return (
            <circle
              key={`factor-current-${point.resourceId}-${point.tsMs}`}
              cx={x}
              cy={y}
              r="5.2"
              fill="none"
              stroke={color}
              strokeWidth="2.2"
            />
          );
        })}
      </svg>
      <div className="chart-legend">
        {model.resourceOrder.map((resourceId) => (
          <span key={`factor-legend-${resourceId}`} className="legend-item">
            <i style={{ backgroundColor: colorForResource(resourceId, model.resourceOrder) }} />
            <span>{resourceId}</span>
          </span>
        ))}
      </div>
    </section>
  );
}

function PriceTimelineChart({ model }: { model: PredictionChartModel }) {
  const width = 960;
  const height = 320;
  const top = 18;
  const left = 56;
  const right = width - 12;
  const bottom = height - 28;
  const ticks = createTimeTicks(model.windowStartMs, model.windowEndMs, 6);
  const spanMs = model.windowEndMs - model.windowStartMs;

  const priceRows = model.points
    .filter((point): point is PredictionPoint & { priceCtPerKwh: number } => point.priceCtPerKwh !== null)
    .map((point) => ({ tsMs: point.tsMs, value: point.priceCtPerKwh }));

  if (priceRows.length === 0) {
    return (
      <section className="output-chart-card">
        <h4>Strompreis-Verlauf</h4>
        <p className="meta-text">Keine Preis-Prediction im Solution-Payload gefunden.</p>
      </section>
    );
  }

  const values = priceRows.map((row) => row.value);
  const rawMin = Math.min(...values);
  const rawMax = Math.max(...values);
  const yMin = rawMax > rawMin ? Math.floor(rawMin * 10) / 10 : rawMin - 1;
  const yMax = rawMax > rawMin ? Math.ceil(rawMax * 10) / 10 : rawMax + 1;
  const yTicks = createValueTicks(yMin, yMax, 5);

  const realRows = model.points
    .slice(0, model.priceSplitIndex)
    .filter((point): point is PredictionPoint & { priceCtPerKwh: number } => point.priceCtPerKwh !== null)
    .map((point) => ({ tsMs: point.tsMs, value: point.priceCtPerKwh }));
  const forecastRows = model.points
    .slice(Math.max(0, model.priceSplitIndex - 1))
    .filter((point): point is PredictionPoint & { priceCtPerKwh: number } => point.priceCtPerKwh !== null)
    .map((point) => ({ tsMs: point.tsMs, value: point.priceCtPerKwh }));

  const realPolyline = realRows
    .map((row) => {
      const x = mapX(row.tsMs, model.windowStartMs, model.windowEndMs, left, right);
      const y = mapY(row.value, yMin, yMax, top, bottom);
      return `${x},${y}`;
    })
    .join(" ");
  const forecastPolyline = forecastRows
    .map((row) => {
      const x = mapX(row.tsMs, model.windowStartMs, model.windowEndMs, left, right);
      const y = mapY(row.value, yMin, yMax, top, bottom);
      return `${x},${y}`;
    })
    .join(" ");

  return (
    <section className="output-chart-card">
      <h4>Strompreis-Verlauf (ct/kWh)</h4>
      <p className="meta-text">
        Farbtrennung: naher eingelesener Zukunftshorizont ({model.knownPriceHours}h) vs. weiterfuhrende Prognose.
        Gesamtfenster: {model.horizonHours.toFixed(1)}h bei {model.intervalMinutes.toFixed(0)}-min Auflosung.
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
          const x = mapX(tick, model.windowStartMs, model.windowEndMs, left, right);
          return (
            <g key={`price-x-${tick}`}>
              <line x1={x} y1={top} x2={x} y2={bottom} stroke="rgba(86,121,188,0.2)" strokeWidth="1" />
              <text x={x} y={height - 10} textAnchor="middle" fill="#9EB0D2" fontSize="11">
                {formatTimeTick(tick, spanMs)}
              </text>
            </g>
          );
        })}
        {realPolyline !== "" ? <polyline points={realPolyline} fill="none" stroke={PRICE_REAL_COLOR} strokeWidth="2.6" /> : null}
        {forecastPolyline !== "" ? (
          <polyline
            points={forecastPolyline}
            fill="none"
            stroke={PRICE_FORECAST_COLOR}
            strokeWidth="2.6"
            strokeDasharray="6 4"
          />
        ) : null}
      </svg>
      <div className="chart-legend">
        <span className="legend-item">
          <i style={{ backgroundColor: PRICE_REAL_COLOR }} />
          <span>Eingelesen (naher Horizont)</span>
        </span>
        <span className="legend-item">
          <i style={{ backgroundColor: PRICE_FORECAST_COLOR }} />
          <span>Forecast (weiterer Horizont)</span>
        </span>
      </div>
    </section>
  );
}

function PvForecastChart({ model }: { model: PredictionChartModel }) {
  const width = 960;
  const height = 320;
  const top = 18;
  const left = 56;
  const right = width - 12;
  const bottom = height - 28;
  const ticks = createTimeTicks(model.windowStartMs, model.windowEndMs, 6);
  const spanMs = model.windowEndMs - model.windowStartMs;

  const acRows = model.points
    .filter((point): point is PredictionPoint & { pvAcKw: number } => point.pvAcKw !== null)
    .map((point) => ({ tsMs: point.tsMs, value: point.pvAcKw }));
  const dcRows = model.points
    .filter((point): point is PredictionPoint & { pvDcKw: number } => point.pvDcKw !== null)
    .map((point) => ({ tsMs: point.tsMs, value: point.pvDcKw }));

  const allValues = [...acRows.map((row) => row.value), ...dcRows.map((row) => row.value)];
  if (allValues.length === 0) {
    return (
      <section className="output-chart-card">
        <h4>PV-Produktionsprognose (kW)</h4>
        <p className="meta-text">Keine PV-Prediction im Solution-Payload gefunden.</p>
      </section>
    );
  }

  const yMax = Math.max(0.1, Math.ceil(Math.max(...allValues) * 10) / 10);
  const yTicks = createValueTicks(0, yMax, 5);

  const acPolyline = acRows
    .map((row) => {
      const x = mapX(row.tsMs, model.windowStartMs, model.windowEndMs, left, right);
      const y = mapY(row.value, 0, yMax, top, bottom);
      return `${x},${y}`;
    })
    .join(" ");
  const dcPolyline = dcRows
    .map((row) => {
      const x = mapX(row.tsMs, model.windowStartMs, model.windowEndMs, left, right);
      const y = mapY(row.value, 0, yMax, top, bottom);
      return `${x},${y}`;
    })
    .join(" ");

  return (
    <section className="output-chart-card">
      <h4>PV-Produktionsprognose (kW)</h4>
      <p className="meta-text">Aus `solution.prediction.data` (AC als Hauptlinie, DC optional als Kontext).</p>
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
          const x = mapX(tick, model.windowStartMs, model.windowEndMs, left, right);
          return (
            <g key={`pv-x-${tick}`}>
              <line x1={x} y1={top} x2={x} y2={bottom} stroke="rgba(86,121,188,0.2)" strokeWidth="1" />
              <text x={x} y={height - 10} textAnchor="middle" fill="#9EB0D2" fontSize="11">
                {formatTimeTick(tick, spanMs)}
              </text>
            </g>
          );
        })}
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
        <span className="legend-item">
          <i style={{ backgroundColor: PV_AC_COLOR }} />
          <span>PV AC (kW)</span>
        </span>
        <span className="legend-item">
          <i style={{ backgroundColor: PV_DC_COLOR }} />
          <span>PV DC (kW)</span>
        </span>
      </div>
    </section>
  );
}

function LoadForecastChart({ model }: { model: PredictionChartModel }) {
  const width = 960;
  const height = 320;
  const top = 18;
  const left = 56;
  const right = width - 12;
  const bottom = height - 28;
  const ticks = createTimeTicks(model.windowStartMs, model.windowEndMs, 6);
  const spanMs = model.windowEndMs - model.windowStartMs;

  const loadRows = model.points
    .filter((point): point is PredictionPoint & { loadKw: number } => point.loadKw !== null)
    .map((point) => ({ tsMs: point.tsMs, value: point.loadKw }));

  if (loadRows.length === 0) {
    return (
      <section className="output-chart-card">
        <h4>Load-Prognose (kW)</h4>
        <p className="meta-text">Keine Last-Prediction im Solution-Payload gefunden.</p>
      </section>
    );
  }

  const yMax = Math.max(0.1, Math.ceil(Math.max(...loadRows.map((row) => row.value)) * 10) / 10);
  const yTicks = createValueTicks(0, yMax, 5);
  const loadPolyline = loadRows
    .map((row) => {
      const x = mapX(row.tsMs, model.windowStartMs, model.windowEndMs, left, right);
      const y = mapY(row.value, 0, yMax, top, bottom);
      return `${x},${y}`;
    })
    .join(" ");

  return (
    <section className="output-chart-card">
      <h4>Load-Prognose (kW)</h4>
      <p className="meta-text">
        Erwartete, nicht-optimierbare Haushaltslast aus `solution.prediction.data` uber den aktiven Vorhersagehorizont.
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
          const x = mapX(tick, model.windowStartMs, model.windowEndMs, left, right);
          return (
            <g key={`load-x-${tick}`}>
              <line x1={x} y1={top} x2={x} y2={bottom} stroke="rgba(86,121,188,0.2)" strokeWidth="1" />
              <text x={x} y={height - 10} textAnchor="middle" fill="#9EB0D2" fontSize="11">
                {formatTimeTick(tick, spanMs)}
              </text>
            </g>
          );
        })}
        {loadPolyline !== "" ? (
          <polyline points={loadPolyline} fill="none" stroke={LOAD_FORECAST_COLOR} strokeWidth="2.6" />
        ) : null}
      </svg>
      <div className="chart-legend">
        <span className="legend-item">
          <i style={{ backgroundColor: LOAD_FORECAST_COLOR }} />
          <span>Haushaltslast-Prognose (kW)</span>
        </span>
      </div>
    </section>
  );
}

export function OutputChartsPanel({ runId, timeline, current, solutionPayload, predictionSeries }: OutputChartsPanelProps) {
  const model = useMemo(() => buildChartModel(timeline, current), [timeline, current]);
  const predictionModel = useMemo(
    () => buildPredictionModel(solutionPayload, predictionSeries),
    [solutionPayload, predictionSeries],
  );
  const hasAnyData = model.hasAnyData || predictionModel.hasPrice || predictionModel.hasPv || predictionModel.hasLoad;

  return (
    <div className="panel">
      <details className="output-charts-panel" open>
        <summary>
          <strong>Charts: Entscheidungen mit Zeitbezug</strong>
          {runId !== null ? ` | Run #${runId}` : ""}
          {predictionModel.points.length > 0 ? ` | Prediction-Horizont ${predictionModel.horizonHours.toFixed(1)}h` : ""}
        </summary>
        <p className="meta-text">
          Visualisierung orientiert sich am EOSdash-Prinzip aus dem Prediction-Tab: Zeitachse + getrennte Fachcharts statt einer einzigen uberladenen Grafik.
        </p>
        {!hasAnyData ? <p>Keine Chart-Daten fur den ausgewahlten Run verfugbar.</p> : null}
        {hasAnyData ? (
          <div className="output-chart-grid">
            <PriceTimelineChart model={predictionModel} />
            <PvForecastChart model={predictionModel} />
            <LoadForecastChart model={predictionModel} />
            <ModeTimelineChart model={model} />
            <FactorTimelineChart model={model} />
          </div>
        ) : null}
      </details>
    </div>
  );
}
