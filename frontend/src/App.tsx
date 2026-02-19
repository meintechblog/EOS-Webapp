import { type FormEvent, useEffect, useMemo, useState } from "react";

import {
  createMapping,
  getEosFields,
  getLiveValues,
  getMappings,
  updateMapping,
} from "./api";
import type { EosFieldOption, LiveValue, Mapping } from "./types";

type MappingFormState = {
  eosField: string;
  mqttTopic: string;
  payloadPath: string;
  unit: string;
  enabled: boolean;
};

const POLL_INTERVAL_MS = 5000;
const CUSTOM_FIELD_VALUE = "__custom_field__";
const CUSTOM_UNIT_VALUE = "__custom_unit__";
const DEFAULT_UNIT_OPTIONS = ["W", "kW", "Wh", "kWh", "%", "C", "EUR/Wh", "ct/kWh"];

const INITIAL_FORM: MappingFormState = {
  eosField: "",
  mqttTopic: "",
  payloadPath: "",
  unit: "",
  enabled: true,
};

function toErrorMessage(error: unknown): string {
  if (error instanceof Error && error.message.length > 0) {
    return error.message;
  }
  return "Unknown error";
}

function statusLabel(liveValue?: LiveValue): string {
  if (!liveValue) {
    return "never";
  }
  return liveValue.status;
}

function lastSeenLabel(liveValue?: LiveValue): string {
  if (!liveValue || liveValue.last_seen_seconds === null) {
    return "never seen";
  }
  if (liveValue.last_seen_seconds < 1) {
    return "just now";
  }
  return `${liveValue.last_seen_seconds}s ago`;
}

function valueLabel(liveValue?: LiveValue): string {
  if (!liveValue || liveValue.parsed_value === null) {
    return "n/a";
  }
  return liveValue.parsed_value;
}

function statusClassName(liveValue?: LiveValue): string {
  const status = statusLabel(liveValue);
  if (status === "healthy") {
    return "badge badge-healthy";
  }
  if (status === "stale") {
    return "badge badge-stale";
  }
  return "badge badge-never";
}

export default function App() {
  const [mappings, setMappings] = useState<Mapping[]>([]);
  const [liveValues, setLiveValues] = useState<LiveValue[]>([]);
  const [eosFields, setEosFields] = useState<EosFieldOption[]>([]);
  const [form, setForm] = useState<MappingFormState>(INITIAL_FORM);
  const [useCustomField, setUseCustomField] = useState(false);
  const [customField, setCustomField] = useState("");
  const [useCustomUnit, setUseCustomUnit] = useState(false);
  const [customUnit, setCustomUnit] = useState("");
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastRefresh, setLastRefresh] = useState<string | null>(null);

  const liveValuesByMappingId = useMemo(() => {
    const map = new Map<number, LiveValue>();
    for (const value of liveValues) {
      map.set(value.mapping_id, value);
    }
    return map;
  }, [liveValues]);

  const selectedFieldOption = useMemo(() => {
    if (useCustomField || form.eosField.trim() === "") {
      return undefined;
    }
    return eosFields.find((field) => field.eos_field === form.eosField);
  }, [eosFields, form.eosField, useCustomField]);

  const unitOptions = useMemo(() => {
    const options =
      selectedFieldOption && selectedFieldOption.suggested_units.length > 0
        ? selectedFieldOption.suggested_units
        : DEFAULT_UNIT_OPTIONS;

    const uniqueOptions = new Set<string>(options);
    if (!useCustomUnit && form.unit.trim() !== "") {
      uniqueOptions.add(form.unit.trim());
    }
    return Array.from(uniqueOptions);
  }, [selectedFieldOption, useCustomUnit, form.unit]);

  async function refreshMappings(): Promise<void> {
    const data = await getMappings();
    setMappings(data);
  }

  async function refreshLiveValues(): Promise<void> {
    const data = await getLiveValues();
    setLiveValues(data);
    setLastRefresh(new Date().toISOString());
  }

  async function refreshAll(): Promise<void> {
    const [mappingData, liveData, eosFieldData] = await Promise.all([
      getMappings(),
      getLiveValues(),
      getEosFields(),
    ]);
    setMappings(mappingData);
    setLiveValues(liveData);
    setEosFields(eosFieldData);
    setLastRefresh(new Date().toISOString());
  }

  useEffect(() => {
    let active = true;

    async function bootstrap() {
      try {
        await refreshAll();
        if (!active) {
          return;
        }
        setError(null);
      } catch (bootstrapError) {
        if (!active) {
          return;
        }
        setError(toErrorMessage(bootstrapError));
      } finally {
        if (active) {
          setLoading(false);
        }
      }
    }

    bootstrap().catch(() => {
      setLoading(false);
    });

    const intervalId = setInterval(() => {
      refreshLiveValues().catch((pollError) => {
        setError(toErrorMessage(pollError));
      });
    }, POLL_INTERVAL_MS);

    return () => {
      active = false;
      clearInterval(intervalId);
    };
  }, []);

  async function handleCreateMapping(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);

    const eosField = useCustomField ? customField.trim() : form.eosField.trim();
    const unit = useCustomUnit ? customUnit.trim() : form.unit.trim();

    if (eosField === "") {
      setError("EOS field is required.");
      setSubmitting(false);
      return;
    }

    try {
      await createMapping({
        eos_field: eosField,
        mqtt_topic: form.mqttTopic.trim(),
        payload_path: form.payloadPath.trim() || null,
        unit: unit || null,
        enabled: form.enabled,
      });
      setForm(INITIAL_FORM);
      setUseCustomField(false);
      setCustomField("");
      setUseCustomUnit(false);
      setCustomUnit("");
      await refreshAll();
    } catch (submitError) {
      setError(toErrorMessage(submitError));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleToggleEnabled(mapping: Mapping) {
    setError(null);
    try {
      await updateMapping(mapping.id, { enabled: !mapping.enabled });
      await refreshMappings();
      await refreshLiveValues();
    } catch (toggleError) {
      setError(toErrorMessage(toggleError));
    }
  }

  function handleSelectEosField(value: string) {
    if (value === CUSTOM_FIELD_VALUE) {
      setUseCustomField(true);
      setForm((current) => ({ ...current, eosField: "" }));
      return;
    }

    setUseCustomField(false);
    const option = eosFields.find((field) => field.eos_field === value);
    setForm((current) => {
      const nextUnit =
        !useCustomUnit && current.unit.trim() === "" && option && option.suggested_units.length > 0
          ? option.suggested_units[0]
          : current.unit;
      return { ...current, eosField: value, unit: nextUnit };
    });
  }

  function handleSelectUnit(value: string) {
    if (value === CUSTOM_UNIT_VALUE) {
      setUseCustomUnit(true);
      setCustomUnit(form.unit);
      return;
    }

    setUseCustomUnit(false);
    setCustomUnit("");
    setForm((current) => ({ ...current, unit: value }));
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">EOS Webapp</p>
          <h1>Local Control Console</h1>
        </div>
        <div className="refresh-info">
          <span className="meta-label">Live refresh:</span>
          <strong>{POLL_INTERVAL_MS / 1000}s</strong>
          <span className="meta-label">Last pull:</span>
          <strong>{lastRefresh ? new Date(lastRefresh).toLocaleTimeString() : "n/a"}</strong>
        </div>
      </header>

      {error ? (
        <div className="error-banner">
          <span>API error:</span>
          <strong>{error}</strong>
        </div>
      ) : null}

      <main className="app-grid">
        <section className="pane pane-inputs">
          <h2>Inputs</h2>
          <p className="pane-copy">
            Configure EOS input mappings and watch live MQTT values per field.
          </p>

          <form className="panel form-panel" onSubmit={handleCreateMapping}>
            <h3>New Mapping</h3>

            <label>
              EOS field (from EOS catalog)
              <select
                required={!useCustomField}
                value={useCustomField ? CUSTOM_FIELD_VALUE : form.eosField}
                onChange={(event) => handleSelectEosField(event.target.value)}
              >
                <option value="">Select EOS field...</option>
                {eosFields.map((field) => (
                  <option key={field.eos_field} value={field.eos_field}>
                    {field.label} ({field.eos_field})
                  </option>
                ))}
                <option value={CUSTOM_FIELD_VALUE}>Custom field...</option>
              </select>
            </label>

            {useCustomField ? (
              <label>
                Custom EOS field
                <input
                  required
                  value={customField}
                  onChange={(event) => setCustomField(event.target.value)}
                  placeholder="pv_power_w"
                />
              </label>
            ) : null}

            {!useCustomField && selectedFieldOption ? (
              <p className="field-hint">
                {selectedFieldOption.description || "No description from EOS available."}
                <span className="hint-meta">sources: {selectedFieldOption.sources.join(", ")}</span>
              </p>
            ) : null}

            {eosFields.length === 0 ? (
              <p className="field-hint">EOS catalog is empty. You can still use custom fields.</p>
            ) : null}

            <label>
              MQTT topic
              <input
                required
                value={form.mqttTopic}
                onChange={(event) => setForm((current) => ({ ...current, mqttTopic: event.target.value }))}
                placeholder="eos/input/pv_power_w"
              />
            </label>

            <label>
              Payload path (optional)
              <input
                value={form.payloadPath}
                onChange={(event) => setForm((current) => ({ ...current, payloadPath: event.target.value }))}
                placeholder="sensor.power"
              />
              <span className="hint-inline">
                For JSON payloads only. Example: for <code>{'{"sensor":{"power":987}}'}</code> use
                <code>sensor.power</code>. Leave empty for plain values like <code>1234</code>.
              </span>
            </label>

            <label>
              Unit (optional, field-aware)
              <select
                value={useCustomUnit ? CUSTOM_UNIT_VALUE : form.unit}
                onChange={(event) => handleSelectUnit(event.target.value)}
              >
                <option value="">No unit</option>
                {unitOptions.map((unitOption) => (
                  <option key={unitOption} value={unitOption}>
                    {unitOption}
                  </option>
                ))}
                <option value={CUSTOM_UNIT_VALUE}>Custom unit...</option>
              </select>
            </label>

            {useCustomUnit ? (
              <label>
                Custom unit
                <input
                  value={customUnit}
                  onChange={(event) => setCustomUnit(event.target.value)}
                  placeholder="W"
                />
              </label>
            ) : null}

            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={form.enabled}
                onChange={(event) => setForm((current) => ({ ...current, enabled: event.target.checked }))}
              />
              Enabled
            </label>
            <button type="submit" disabled={submitting}>
              {submitting ? "Saving..." : "Create mapping"}
            </button>
          </form>

          <div className="panel list-panel">
            <h3>Configured Mappings</h3>
            {loading ? <p>Loading mappings...</p> : null}
            {!loading && mappings.length === 0 ? <p>No mappings configured yet.</p> : null}
            <ul className="mapping-list">
              {mappings.map((mapping) => {
                const live = liveValuesByMappingId.get(mapping.id);
                return (
                  <li key={mapping.id} className="mapping-item">
                    <div className="mapping-head">
                      <strong>{mapping.eos_field}</strong>
                      <span className={statusClassName(live)}>{statusLabel(live)}</span>
                    </div>
                    <code>{mapping.mqtt_topic}</code>
                    <div className="mapping-meta">
                      <span>value: {valueLabel(live)}</span>
                      <span>last seen: {lastSeenLabel(live)}</span>
                      <span>
                        enabled: <strong>{mapping.enabled ? "yes" : "no"}</strong>
                      </span>
                    </div>
                    <div className="mapping-actions">
                      <button
                        type="button"
                        className="secondary"
                        onClick={() => {
                          void handleToggleEnabled(mapping);
                        }}
                      >
                        {mapping.enabled ? "Disable" : "Enable"}
                      </button>
                    </div>
                  </li>
                );
              })}
            </ul>
          </div>
        </section>

        <section className="pane pane-params">
          <h2>Parameters + Run</h2>
          <p className="pane-copy">
            Slice 2 keeps this area ready for the next step: optimization parameter editor and run trigger.
          </p>
          <div className="panel placeholder-card">
            <h3>Next Implementation</h3>
            <p>Connect EOS optimization settings to backend orchestration endpoints.</p>
            <ul>
              <li>Load preset parameter set from DB</li>
              <li>Validate user edits before run</li>
              <li>Trigger /optimize through backend only</li>
            </ul>
          </div>
        </section>

        <section className="pane pane-outputs">
          <h2>Outputs</h2>
          <p className="pane-copy">
            Output widgets will consume optimization results and export targets in the next slice.
          </p>
          <div className="panel placeholder-card">
            <h3>Planned Output Flow</h3>
            <p>EOS result persistence, result timeline, and forwarding integration points.</p>
            <ul>
              <li>Latest run summary</li>
              <li>Result detail table</li>
              <li>Export to MQTT and external systems</li>
            </ul>
          </div>
        </section>
      </main>
    </div>
  );
}
