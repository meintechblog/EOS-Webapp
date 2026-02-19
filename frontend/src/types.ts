export type Mapping = {
  id: number;
  eos_field: string;
  mqtt_topic: string;
  payload_path: string | null;
  unit: string | null;
  enabled: boolean;
  created_at: string;
  updated_at: string;
};

export type MappingCreatePayload = {
  eos_field: string;
  mqtt_topic: string;
  payload_path?: string | null;
  unit?: string | null;
  enabled?: boolean;
};

export type MappingUpdatePayload = Partial<{
  eos_field: string;
  mqtt_topic: string;
  payload_path: string | null;
  unit: string | null;
  enabled: boolean;
}>;

export type LiveValue = {
  mapping_id: number;
  eos_field: string;
  mqtt_topic: string;
  unit: string | null;
  parsed_value: string | null;
  ts: string | null;
  last_seen_seconds: number | null;
  status: "healthy" | "stale" | "never";
};

