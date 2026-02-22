import type {
  DataSignalSeries,
  DataSignalSeriesResolution,
  EosAutoRunPreset,
  EosAutoRunUpdateResponse,
  EosForceRunResponse,
  EosPredictionRefreshResponse,
  EosPredictionRefreshScope,
  EosOutputCurrentItem,
  EosOutputSignalsBundle,
  EosOutputTimelineItem,
  EosRunPlausibility,
  EosRunDetail,
  EosRunPlan,
  EosRunContext,
  EosRunPredictionSeries,
  EosRunSolution,
  EosRunSummary,
  EosRuntime,
  SetupExportPackageV2,
  SetupField,
  SetupFieldPatchResponse,
  SetupFieldSource,
  SetupImportResponse,
  SetupLayout,
  SetupEntityMutatePayload,
  SetupEntityMutateResponse,
  SetupReadiness,
  StatusResponse,
} from "./types";

async function apiRequest<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    ...init,
  });
  const responseText = response.status === 204 ? "" : await response.text();
  const contentType = response.headers.get("content-type") ?? "";

  const parseJson = (): unknown | null => {
    if (responseText.trim() === "") {
      return null;
    }
    try {
      return JSON.parse(responseText) as unknown;
    } catch {
      return null;
    }
  };

  if (!response.ok) {
    let errorText = `API request failed with status ${response.status}`;
    const payload = parseJson();
    if (payload !== null) {
      const payloadRecord =
        typeof payload === "object" && payload !== null
          ? (payload as Record<string, unknown>)
          : null;
      if (typeof payloadRecord?.detail === "string") {
        errorText = payloadRecord.detail;
      } else {
        errorText = JSON.stringify(payload);
      }
    } else if (responseText.trim() !== "") {
      errorText = responseText;
    }
    throw new Error(errorText);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  if (!contentType.includes("application/json")) {
    return undefined as T;
  }
  const payload = parseJson();
  if (payload === null) {
    throw new Error("API returned invalid JSON");
  }
  return payload as T;
}

export function getSetupFields(): Promise<SetupField[]> {
  return apiRequest<SetupField[]>("/api/setup/fields");
}

export function patchSetupFields(
  updates: Array<{ field_id: string; value: unknown; source: SetupFieldSource }>,
): Promise<SetupFieldPatchResponse> {
  return apiRequest<SetupFieldPatchResponse>("/api/setup/fields", {
    method: "PATCH",
    body: JSON.stringify({ updates }),
  });
}

export function getSetupLayout(): Promise<SetupLayout> {
  return apiRequest<SetupLayout>("/api/setup/layout");
}

export function mutateSetupEntity(payload: SetupEntityMutatePayload): Promise<SetupEntityMutateResponse> {
  return apiRequest<SetupEntityMutateResponse>("/api/setup/entities/mutate", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getSetupReadiness(): Promise<SetupReadiness> {
  return apiRequest<SetupReadiness>("/api/setup/readiness");
}

export function getSetupExport(): Promise<SetupExportPackageV2> {
  return apiRequest<SetupExportPackageV2>("/api/setup/export");
}

export function postSetupImport(packageJson: Record<string, unknown>): Promise<SetupImportResponse> {
  return apiRequest<SetupImportResponse>("/api/setup/import", {
    method: "POST",
    body: JSON.stringify({ package_json: packageJson }),
  });
}

export function postSetupSet(path: string, value: unknown): Promise<unknown> {
  return apiRequest<unknown>("/api/setup/set", {
    method: "POST",
    body: JSON.stringify({ path, value, source: "http" }),
  });
}

export function getStatus(): Promise<StatusResponse> {
  return apiRequest<StatusResponse>("/status");
}

export function getDataSignalSeries(params: {
  signalKey: string;
  from: string;
  to: string;
  resolution?: DataSignalSeriesResolution;
}): Promise<DataSignalSeries> {
  const query = new URLSearchParams();
  query.set("signal_key", params.signalKey);
  query.set("from", params.from);
  query.set("to", params.to);
  query.set("resolution", params.resolution ?? "raw");
  return apiRequest<DataSignalSeries>(`/api/data/series?${query.toString()}`);
}

export function getEosRuntime(): Promise<EosRuntime> {
  return apiRequest<EosRuntime>("/api/eos/runtime");
}

export function putEosAutoRunPreset(preset: EosAutoRunPreset): Promise<EosAutoRunUpdateResponse> {
  return apiRequest<EosAutoRunUpdateResponse>("/api/eos/runtime/auto-run", {
    method: "PUT",
    body: JSON.stringify({ preset }),
  });
}

export function forceEosRun(): Promise<EosForceRunResponse> {
  return apiRequest<EosForceRunResponse>("/api/eos/runs/force", {
    method: "POST",
  });
}

export function refreshEosPredictions(scope: EosPredictionRefreshScope): Promise<EosPredictionRefreshResponse> {
  return apiRequest<EosPredictionRefreshResponse>("/api/eos/runs/predictions/refresh", {
    method: "POST",
    body: JSON.stringify({ scope }),
  });
}

export function getEosRuns(): Promise<EosRunSummary[]> {
  return apiRequest<EosRunSummary[]>("/api/eos/runs");
}

export function getEosRunDetail(runId: number): Promise<EosRunDetail> {
  return apiRequest<EosRunDetail>(`/api/eos/runs/${runId}`);
}

export function getEosRunPlan(runId: number): Promise<EosRunPlan> {
  return apiRequest<EosRunPlan>(`/api/eos/runs/${runId}/plan`);
}

export function getEosRunSolution(runId: number): Promise<EosRunSolution> {
  return apiRequest<EosRunSolution>(`/api/eos/runs/${runId}/solution`);
}

export function getEosRunContext(runId: number): Promise<EosRunContext> {
  return apiRequest<EosRunContext>(`/api/eos/runs/${runId}/context`);
}

export function getEosRunPredictionSeries(runId: number): Promise<EosRunPredictionSeries> {
  return apiRequest<EosRunPredictionSeries>(`/api/eos/runs/${runId}/prediction-series`);
}

export function getEosOutputsCurrent(runId?: number): Promise<EosOutputCurrentItem[]> {
  const query = runId !== undefined ? `?run_id=${encodeURIComponent(String(runId))}` : "";
  return apiRequest<EosOutputCurrentItem[]>(`/api/eos/outputs/current${query}`);
}

export function getEosOutputsTimeline(params?: {
  runId?: number;
  from?: string;
  to?: string;
  resourceId?: string;
}): Promise<EosOutputTimelineItem[]> {
  const query = new URLSearchParams();
  if (params?.runId !== undefined) {
    query.set("run_id", String(params.runId));
  }
  if (params?.from) {
    query.set("from_ts", params.from);
  }
  if (params?.to) {
    query.set("to_ts", params.to);
  }
  if (params?.resourceId) {
    query.set("resource_id", params.resourceId);
  }
  const suffix = query.toString();
  return apiRequest<EosOutputTimelineItem[]>(`/api/eos/outputs/timeline${suffix ? `?${suffix}` : ""}`);
}

export function getEosOutputSignals(params?: {
  runId?: number;
}): Promise<EosOutputSignalsBundle> {
  const query = new URLSearchParams();
  if (params?.runId !== undefined) {
    query.set("run_id", String(params.runId));
  }
  const suffix = query.toString();
  return apiRequest<EosOutputSignalsBundle>(`/api/eos/output-signals${suffix ? `?${suffix}` : ""}`);
}

export function getEosRunPlausibility(runId: number): Promise<EosRunPlausibility> {
  return apiRequest<EosRunPlausibility>(`/api/eos/runs/${runId}/plausibility`);
}
