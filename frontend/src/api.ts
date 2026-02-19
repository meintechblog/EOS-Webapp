import type {
  LiveValue,
  Mapping,
  MappingCreatePayload,
  MappingUpdatePayload,
} from "./types";

async function apiRequest<T>(
  url: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(url, {
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    ...init,
  });

  if (!response.ok) {
    const payload = await response.text();
    throw new Error(payload || `API request failed with status ${response.status}`);
  }

  return (await response.json()) as T;
}

export function getMappings(): Promise<Mapping[]> {
  return apiRequest<Mapping[]>("/api/mappings");
}

export function createMapping(payload: MappingCreatePayload): Promise<Mapping> {
  return apiRequest<Mapping>("/api/mappings", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateMapping(
  mappingId: number,
  payload: MappingUpdatePayload,
): Promise<Mapping> {
  return apiRequest<Mapping>(`/api/mappings/${mappingId}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function getLiveValues(): Promise<LiveValue[]> {
  return apiRequest<LiveValue[]>("/api/live-values");
}

