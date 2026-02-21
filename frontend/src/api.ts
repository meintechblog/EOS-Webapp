import type {
  EosForceRunResponse,
  EosPredictionRefreshResponse,
  EosPredictionRefreshScope,
  EosOutputCurrentItem,
  EosOutputTimelineItem,
  EosRunPlausibility,
  EosRunDetail,
  EosRunPlan,
  EosRunContext,
  EosRunSolution,
  EosRunSummary,
  EosRuntime,
  OutputDispatchEvent,
  OutputDispatchForceResponse,
  OutputTarget,
  OutputTargetCreatePayload,
  OutputTargetUpdatePayload,
  SetupExportPackageV2,
  SetupField,
  SetupFieldPatchResponse,
  SetupFieldSource,
  SetupImportResponse,
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

  if (!response.ok) {
    let errorText = `API request failed with status ${response.status}`;
    try {
      const payload = await response.json();
      if (typeof payload?.detail === "string") {
        errorText = payload.detail;
      } else {
        errorText = JSON.stringify(payload);
      }
    } catch {
      const payload = await response.text();
      if (payload.trim() !== "") {
        errorText = payload;
      }
    }
    throw new Error(errorText);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("application/json")) {
    return undefined as T;
  }

  return (await response.json()) as T;
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

export function getEosRuntime(): Promise<EosRuntime> {
  return apiRequest<EosRuntime>("/api/eos/runtime");
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

export function getEosOutputEvents(params?: {
  runId?: number;
  resourceId?: string;
  limit?: number;
}): Promise<OutputDispatchEvent[]> {
  const query = new URLSearchParams();
  if (params?.runId !== undefined) {
    query.set("run_id", String(params.runId));
  }
  if (params?.resourceId) {
    query.set("resource_id", params.resourceId);
  }
  if (params?.limit !== undefined) {
    query.set("limit", String(params.limit));
  }
  const suffix = query.toString();
  return apiRequest<OutputDispatchEvent[]>(`/api/eos/outputs/events${suffix ? `?${suffix}` : ""}`);
}

export function forceEosOutputDispatch(resourceIds?: string[]): Promise<OutputDispatchForceResponse> {
  return apiRequest<OutputDispatchForceResponse>("/api/eos/outputs/dispatch/force", {
    method: "POST",
    body: JSON.stringify({ resource_ids: resourceIds && resourceIds.length > 0 ? resourceIds : null }),
  });
}

export function getOutputTargets(): Promise<OutputTarget[]> {
  return apiRequest<OutputTarget[]>("/api/eos/output-targets");
}

export function createOutputTarget(payload: OutputTargetCreatePayload): Promise<OutputTarget> {
  return apiRequest<OutputTarget>("/api/eos/output-targets", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateOutputTarget(targetId: number, payload: OutputTargetUpdatePayload): Promise<OutputTarget> {
  return apiRequest<OutputTarget>(`/api/eos/output-targets/${targetId}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function getEosRunPlausibility(runId: number): Promise<EosRunPlausibility> {
  return apiRequest<EosRunPlausibility>(`/api/eos/runs/${runId}/plausibility`);
}
