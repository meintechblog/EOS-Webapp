import { type FormEvent, useEffect, useMemo, useState } from "react";

import { createMapping, getLiveValues, getMappings, updateMapping } from "./api";
import type { LiveValue, Mapping } from "./types";

type MappingFormState = {
  eosField: string;
  mqttTopic: string;
  payloadPath: string;
  unit: string;
  enabled: boolean;
};

const POLL_INTERVAL_MS = 5000;

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
  const [form, setForm] = useState<MappingFormState>(INITIAL_FORM);
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
    const [mappingData, liveData] = await Promise.all([getMappings(), getLiveValues()]);
    setMappings(mappingData);
    setLiveValues(liveData);
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
    try {
      await createMapping({
        eos_field: form.eosField.trim(),
        mqtt_topic: form.mqttTopic.trim(),
        payload_path: form.payloadPath.trim() || null,
        unit: form.unit.trim() || null,
        enabled: form.enabled,
      });
      setForm(INITIAL_FORM);
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
              EOS field
              <input
                required
                value={form.eosField}
                onChange={(event) => setForm((current) => ({ ...current, eosField: event.target.value }))}
                placeholder="pv_power_w"
              />
            </label>
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
            </label>
            <label>
              Unit (optional)
              <input
                value={form.unit}
                onChange={(event) => setForm((current) => ({ ...current, unit: event.target.value }))}
                placeholder="W"
              />
            </label>
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
