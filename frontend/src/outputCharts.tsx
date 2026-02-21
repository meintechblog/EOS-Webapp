import { useMemo } from "react";

import type { EosOutputCurrentItem, EosOutputTimelineItem } from "./types";

type OutputChartsPanelProps = {
  runId: number | null;
  timeline: EosOutputTimelineItem[];
  current: EosOutputCurrentItem[];
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

function toTimestampMs(value: string | null | undefined): number | null {
  if (!value) {
    return null;
  }
  const ts = new Date(value).getTime();
  return Number.isFinite(ts) ? ts : null;
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
export function OutputChartsPanel({ runId, timeline, current }: OutputChartsPanelProps) {
  const model = useMemo(() => buildChartModel(timeline, current), [timeline, current]);

  return (
    <div className="panel">
      <details className="output-charts-panel" open>
        <summary>
          <strong>Charts: Entscheidungen mit Zeitbezug</strong>
          {runId !== null ? ` | Run #${runId}` : ""}
        </summary>
        <p className="meta-text">
          Visualisierung orientiert sich am EOSdash-Prinzip aus dem Prediction-Tab: Zeitachse + getrennte Fachcharts statt einer einzigen uberladenen Grafik.
        </p>
        {!model.hasAnyData ? <p>Keine Chart-Daten fur den ausgewahlten Run verfugbar.</p> : null}
        {model.hasAnyData ? (
          <div className="output-chart-grid">
            <ModeTimelineChart model={model} />
            <FactorTimelineChart model={model} />
          </div>
        ) : null}
      </details>
    </div>
  );
}
