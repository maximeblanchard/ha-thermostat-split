/**
 * HA Thermostat Split — Carte Lovelace tout-en-un (Régulation Inverter + Planning Hebdomadaire 7 jours)
 * Inspirée des meilleures pratiques de Netatmo, Panasonic Comfort Cloud, Scheduler Card et Mushroom.
 */

const DOMAIN = "ha_thermostat_split";
const DAY_LABELS_SHORT = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"];
const DAY_LABELS_FULL = [
  "Lundi",
  "Mardi",
  "Mercredi",
  "Jeudi",
  "Vendredi",
  "Samedi",
  "Dimanche",
];

const MODE_META = {
  off: { label: "Coupé", short: "OFF", color: "#64748b" },
  away: { label: "Absent", short: "Absent", color: "#3b82f6" },
  eco: { label: "Éco", short: "Éco", color: "#10b981" },
  comfort: { label: "Confort", short: "Confort", color: "#f59e0b" },
  boost: { label: "Boost", short: "Boost", color: "#ef4444" },
  temp: { label: "Libre", short: "Libre", color: "#f97316" },
};

function parseMin(hhmm) {
  if (!hhmm || typeof hhmm !== "string") return 0;
  const [h, m] = hhmm.split(":").map((x) => parseInt(x, 10) || 0);
  return Math.max(0, Math.min(1439, h * 60 + m));
}

function formatMin(total) {
  const c = Math.max(0, Math.min(1439, Math.round(total)));
  const h = String(Math.floor(c / 60)).padStart(2, "0");
  const m = String(c % 60).padStart(2, "0");
  return `${h}:${m}`;
}

function formatTemp(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return "—";
  return Number(val).toFixed(1).replace(".", ",");
}

function colorForSlot(slot, presets) {
  if (!slot || slot.mode === "off") return MODE_META.off.color;
  if (slot.mode !== "temp" && MODE_META[slot.mode]) {
    return MODE_META[slot.mode].color;
  }
  const t = Number(slot.mode === "temp" ? slot.temp : presets?.[slot.mode] ?? 20);
  if (t < 17.5) return "#3b82f6";
  if (t < 19.2) return "#10b981";
  if (t < 21.0) return "#f59e0b";
  return "#ef4444";
}

function slotSummary(slot, presets) {
  if (!slot || slot.mode === "off") return "OFF";
  if (slot.mode === "temp") return `${formatTemp(slot.temp)}°`;
  const pTemp = presets?.[slot.mode];
  const meta = MODE_META[slot.mode];
  return pTemp !== undefined ? `${meta ? meta.short : slot.mode} ${formatTemp(pTemp)}°` : slot.mode;
}

function normalizeSlotsJS(slots, minTemp = 15, maxTemp = 24) {
  if (!Array.isArray(slots) || slots.length === 0) {
    return [{ start: "00:00", mode: "eco", temp: null }];
  }
  const map = new Map();
  for (const s of slots) {
    const m = Math.round(parseMin(s.start) / 15) * 15;
    const clampedM = Math.max(0, Math.min(1425, m));
    const mode = MODE_META[s.mode] ? s.mode : "eco";
    let temp = null;
    if (mode === "temp") {
      const raw = Number(s.temp ?? 20);
      temp = Math.max(minTemp, Math.min(maxTemp, Math.round(raw * 2) / 2));
    }
    map.set(clampedM, { start: formatMin(clampedM), mode, temp });
  }
  const keys = Array.from(map.keys()).sort((a, b) => a - b);
  if (keys[0] !== 0) {
    const first = map.get(keys[0]);
    map.set(0, { start: "00:00", mode: first.mode, temp: first.temp });
    keys.unshift(0);
  }
  return keys.map((k) => map.get(k));
}

function buildTimeOptions(selectedHHMM, occupiedStarts = null) {
  const opts = [];
  for (let m = 15; m < 1440; m += 15) {
    const hhmm = formatMin(m);
    const isSelected = hhmm === selectedHHMM;
    const isOccupied = !isSelected && occupiedStarts && occupiedStarts.has(hhmm);
    opts.push(
      `<option value="${hhmm}" ${isSelected ? "selected" : ""} ${isOccupied ? "disabled" : ""}>${hhmm}</option>`
    );
  }
  return opts.join("");
}

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/* ==========================================================================
   ÉDITEUR VISUEL DE CONFIGURATION DE LA CARTE (Lovelace UI)
   ========================================================================== */
class HAThermostatSplitCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = { layout_mode: "full", ...config };
    if (this.shadowRoot?.activeElement?.id === "title-input") {
      return;
    }
    this._render();
  }

  set hass(hass) {
    const prevCount = this._entities ? this._entities.length : -1;
    this._hass = hass;
    const list = Object.keys(hass.states || {})
      .filter((eid) => eid.startsWith("climate."))
      .sort((a, b) => {
        const aOurs = hass.states[a]?.attributes?.planning_version !== undefined ? 0 : 1;
        const bOurs = hass.states[b]?.attributes?.planning_version !== undefined ? 0 : 1;
        return aOurs - bOurs || a.localeCompare(b);
      });
    this._entities = list;
    if (list.length !== prevCount) {
      this._render();
    }
  }

  _render() {
    if (!this._config) return;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    const entities = this._entities || [];
    const curEntity = this._config.entity || "";
    const curTitle = this._config.title || "";
    const layoutMode = this._config.layout_mode || "full";

    this.shadowRoot.innerHTML = `
      <style>
        .editor { display: flex; flex-direction: column; gap: 14px; padding: 8px 0; font-family: var(--paper-font-body1_-_font-family, sans-serif); }
        label { display: flex; flex-direction: column; gap: 6px; font-size: 13px; font-weight: 500; color: var(--primary-text-color); }
        select, input {
          padding: 9px 10px;
          border-radius: 8px;
          border: 1px solid var(--divider-color, #cbd5e1);
          background: var(--card-background-color, #fff);
          color: var(--primary-text-color, #111);
          font-size: 14px;
        }
      </style>
      <div class="editor">
        <label>
          Thermostat (HA Thermostat Split)
          <select id="entity-select">
            <option value="">— Sélectionner une entité climate —</option>
            ${entities
              .map((eid) => {
                const st = this._hass?.states?.[eid];
                const fname = st?.attributes?.friendly_name || eid;
                const tag = st?.attributes?.planning_version !== undefined ? " ★" : "";
                return `<option value="${escapeHtml(eid)}" ${eid === curEntity ? "selected" : ""}>${escapeHtml(fname)} (${escapeHtml(eid)})${tag}</option>`;
              })
              .join("")}
          </select>
        </label>
        <label>
          Titre personnalisé (optionnel)
          <input id="title-input" type="text" placeholder="Ex : Salon" value="${escapeHtml(curTitle)}" />
        </label>
        <label>
          Mode d'affichage
          <select id="layout-select">
            <option value="full" ${layoutMode === "full" ? "selected" : ""}>Complet (Thermostat + Planning 7 jours)</option>
            <option value="compact" ${layoutMode === "compact" ? "selected" : ""}>Compact (Planning repliable)</option>
            <option value="schedule_only" ${layoutMode === "schedule_only" ? "selected" : ""}>Planning hebdomadaire uniquement</option>
          </select>
        </label>
      </div>
    `;

    this.shadowRoot.getElementById("entity-select")?.addEventListener("change", (e) => {
      this._emit({ ...this._config, entity: e.target.value });
    });
    this.shadowRoot.getElementById("title-input")?.addEventListener("input", (e) => {
      this._emit({ ...this._config, title: e.target.value });
    });
    this.shadowRoot.getElementById("layout-select")?.addEventListener("change", (e) => {
      this._emit({ ...this._config, layout_mode: e.target.value });
    });
  }

  _emit(config) {
    this._config = config;
    this.dispatchEvent(
      new CustomEvent("config-changed", {
        detail: { config },
        bubbles: true,
        composed: true,
      })
    );
  }
}

/* ==========================================================================
   CARTE PRINCIPALE : THERMOSTAT INVERTER & PLANNING HEBDOMADAIRE
   ========================================================================== */
class HAThermostatSplitCard extends HTMLElement {
  static getConfigElement() {
    return document.createElement("ha-thermostat-split-card-editor");
  }

  static getStubConfig(hass, entities = [], entitiesFallback = []) {
    const states = hass?.states || {};
    const pool = [
      ...Object.keys(states),
      ...(Array.isArray(entities) ? entities : []),
      ...(Array.isArray(entitiesFallback) ? entitiesFallback : []),
    ];
    const match =
      pool.find(
        (eid) =>
          typeof eid === "string" &&
          eid.startsWith("climate.") &&
          states[eid]?.attributes?.planning_version !== undefined
      ) ||
      pool.find((eid) => typeof eid === "string" && eid.startsWith("climate.")) ||
      "";
    return {
      entity: match,
      layout_mode: "full",
    };
  }

  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = null;
    this._hass = null;
    this._lastEntitySig = "";
    this._lastLoadedVersion = -1;
    this._scheduleData = null;
    this._draftDays = null;
    this._draftPresets = null;
    this._dirty = false;
    this._saving = false;
    this._toast = "";
    this._selectedDay = null; // 0..6 quand l'éditeur d'un jour est ouvert
    this._showPresetTuning = false;
    this._showDiagnostics = false;
    this._scheduleExpanded = true;
    this._pendingTarget = null;
    this._tempDebounce = null;
    this._toastTimer = null;
    this._clockInterval = null;
    this._skeletonBuilt = false;
  }

  connectedCallback() {
    if (!this._clockInterval) {
      this._clockInterval = setInterval(() => {
        const marker = this.shadowRoot?.querySelector(".now-marker");
        if (marker) {
          const now = new Date();
          const nowPct = ((now.getHours() * 60 + now.getMinutes()) / 1440) * 100;
          marker.style.left = `${nowPct.toFixed(2)}%`;
        }
      }, 60000);
    }
  }

  disconnectedCallback() {
    if (this._clockInterval) {
      clearInterval(this._clockInterval);
      this._clockInterval = null;
    }
    if (this._tempDebounce) {
      clearTimeout(this._tempDebounce);
      this._tempDebounce = null;
    }
    if (this._toastTimer) {
      clearTimeout(this._toastTimer);
      this._toastTimer = null;
    }
  }

  setConfig(config) {
    if (!config) {
      throw new Error("Configuration invalide.");
    }
    const entityChanged = this._config?.entity !== config.entity;
    const prevLayout = this._config?.layout_mode;
    this._config = { layout_mode: "full", ...config };
    if (this._config.layout_mode === "compact" && (entityChanged || prevLayout !== "compact")) {
      this._scheduleExpanded = false;
    } else if (this._config.layout_mode !== "compact") {
      this._scheduleExpanded = true;
    }
    if (entityChanged) {
      this._scheduleData = null;
      this._draftDays = null;
      this._draftPresets = null;
      this._dirty = false;
      this._lastLoadedVersion = -1;
      this._lastEntitySig = "";
    }
    this._ensureSkeleton();
    if (this._hass) {
      this._loadSchedule(true);
      this._renderHeader();
      this._renderSchedule();
    }
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._config) return;
    this._ensureSkeleton();

    const st = hass.states?.[this._config.entity];
    if (!st) {
      if (this._lastEntitySig !== "__missing__") {
        this._lastEntitySig = "__missing__";
        this._renderHeader();
        this._renderSchedule();
      }
      return;
    }

    const a = st.attributes || {};
    const sig = JSON.stringify([
      st.state,
      a.current_temperature,
      a.temperature,
      a.hvac_action,
      a.preset_mode,
      a.consigne_envoyee_au_split,
      a.statut_inverter,
      a.planning_actif,
      a.planning_version,
      a.prochain_changement,
      a.derogation_planning,
      a.decision,
    ]);

    if (sig !== this._lastEntitySig) {
      this._lastEntitySig = sig;
      const remoteVer = Number(a.planning_version ?? 0);
      if (
        (!this._scheduleData || (remoteVer !== this._lastLoadedVersion && !this._dirty)) &&
        !this._loadingSchedule
      ) {
        this._loadSchedule(false);
      }
      this._renderHeader();
      if (!this._dirty) {
        this._renderSchedule();
      }
    }
  }

  getCardSize() {
    return this._config?.layout_mode === "compact" && !this._scheduleExpanded ? 3 : 7;
  }

  _ensureSkeleton() {
    if (this._skeletonBuilt) return;
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
        }
        ha-card {
          padding: 16px;
          border-radius: var(--ha-card-border-radius, 16px);
          background: var(--ha-card-background, var(--card-background-color, #fff));
          color: var(--primary-text-color, #1e293b);
          box-sizing: border-box;
          font-family: var(--paper-font-body1_-_font-family, system-ui, -apple-system, sans-serif);
        }
        .header-top {
          display: flex;
          justify-content: space-between;
          align-items: flex-start;
          gap: 12px;
        }
        .room-title {
          font-size: 17px;
          font-weight: 700;
          line-height: 1.25;
          margin: 0;
        }
        .status-line {
          display: flex;
          flex-wrap: wrap;
          align-items: center;
          gap: 6px;
          margin-top: 4px;
          font-size: 12px;
          color: var(--secondary-text-color, #64748b);
        }
        .badge {
          display: inline-flex;
          align-items: center;
          gap: 5px;
          padding: 2px 8px;
          border-radius: 999px;
          font-size: 11px;
          font-weight: 600;
        }
        .badge.heating { background: rgba(245, 158, 11, 0.16); color: #d97706; }
        .badge.idle { background: rgba(16, 185, 129, 0.14); color: #059669; }
        .badge.off { background: rgba(100, 116, 139, 0.15); color: var(--secondary-text-color, #64748b); }

        .temp-row {
          display: flex;
          justify-content: space-between;
          align-items: center;
          margin: 14px 0 12px;
          padding: 12px 14px;
          border-radius: 14px;
          background: rgba(127, 127, 127, 0.06);
        }
        .curr-temp-block {
          display: flex;
          flex-direction: column;
        }
        .curr-label {
          font-size: 11px;
          text-transform: uppercase;
          letter-spacing: 0.04em;
          color: var(--secondary-text-color, #64748b);
        }
        .curr-temp {
          font-size: 32px;
          font-weight: 700;
          line-height: 1.1;
          margin-top: 2px;
        }
        .curr-unit {
          font-size: 16px;
          font-weight: 500;
          opacity: 0.7;
        }
        .stepper {
          display: flex;
          align-items: center;
          gap: 10px;
        }
        .step-btn {
          width: 38px;
          height: 38px;
          border-radius: 50%;
          border: 1px solid var(--divider-color, rgba(127,127,127,0.25));
          background: var(--card-background-color, #fff);
          color: var(--primary-text-color);
          font-size: 20px;
          font-weight: 600;
          cursor: pointer;
          display: flex;
          align-items: center;
          justify-content: center;
          transition: transform 0.1s ease, background 0.15s ease;
          user-select: none;
        }
        .step-btn:active { transform: scale(0.93); }
        .target-box {
          min-width: 66px;
          text-align: center;
        }
        .target-label {
          font-size: 10px;
          text-transform: uppercase;
          color: var(--secondary-text-color, #64748b);
        }
        .target-val {
          font-size: 21px;
          font-weight: 700;
        }

        .modes-bar {
          display: grid;
          grid-template-columns: repeat(6, minmax(0, 1fr));
          gap: 6px;
          margin-bottom: 10px;
        }
        @media (max-width: 480px) {
          .modes-bar {
            grid-template-columns: repeat(3, minmax(0, 1fr));
          }
        }
        .mode-chip {
          border: 1px solid var(--divider-color, rgba(127,127,127,0.2));
          background: transparent;
          color: var(--primary-text-color);
          border-radius: 10px;
          padding: 7px 4px;
          font-size: 12px;
          font-weight: 600;
          cursor: pointer;
          text-align: center;
          transition: all 0.15s ease;
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
        }
        .mode-chip.active {
          background: var(--primary-color, #0ea5e9);
          border-color: var(--primary-color, #0ea5e9);
          color: #fff;
        }
        .mode-chip.active-off {
          background: #475569;
          border-color: #475569;
          color: #fff;
        }

        .override-banner {
          display: flex;
          justify-content: space-between;
          align-items: center;
          gap: 8px;
          padding: 8px 12px;
          margin-bottom: 12px;
          border-radius: 10px;
          background: rgba(245, 158, 11, 0.14);
          border: 1px solid rgba(245, 158, 11, 0.35);
          font-size: 12px;
        }
        .override-btn {
          border: none;
          border-radius: 8px;
          padding: 5px 10px;
          font-size: 12px;
          font-weight: 600;
          cursor: pointer;
          background: #f59e0b;
          color: #fff;
          white-space: nowrap;
        }

        .diag-toggle-bar {
          display: flex;
          justify-content: center;
          margin: 6px 0 10px;
        }
        .diag-toggle-btn {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          border: 1px solid var(--divider-color, rgba(127,127,127,0.2));
          background: rgba(127, 127, 127, 0.05);
          color: var(--secondary-text-color, #64748b);
          border-radius: 20px;
          padding: 4px 12px;
          font-size: 11px;
          font-weight: 600;
          cursor: pointer;
          transition: all 0.2s ease;
        }
        .diag-toggle-btn:hover {
          background: rgba(127, 127, 127, 0.12);
          color: var(--primary-text-color);
        }
        .diag-arrow {
          font-size: 9px;
          opacity: 0.7;
        }
        .diag-panel {
          background: var(--secondary-background-color, rgba(127,127,127,0.06));
          border: 1px solid var(--divider-color, rgba(127,127,127,0.18));
          border-radius: 12px;
          padding: 10px 12px;
          margin-bottom: 12px;
          display: flex;
          flex-direction: column;
          gap: 8px;
        }
        .diag-grid {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
          gap: 8px;
        }
        .diag-card {
          background: var(--card-background-color, rgba(255,255,255,0.03));
          border: 1px solid var(--divider-color, rgba(127,127,127,0.14));
          border-radius: 8px;
          padding: 8px 10px;
          display: flex;
          flex-direction: column;
          gap: 2px;
        }
        .diag-card-title {
          font-size: 11px;
          font-weight: 600;
          color: var(--secondary-text-color, #94a3b8);
        }
        .diag-card-val {
          font-size: 15px;
          font-weight: 700;
          color: var(--primary-text-color);
        }
        .diag-card-sub {
          font-size: 10px;
          color: var(--secondary-text-color, #64748b);
        }
        .diag-outdoor-box {
          border-top: 1px dashed var(--divider-color, rgba(127,127,127,0.2));
          padding-top: 8px;
          margin-top: 2px;
          display: flex;
          flex-direction: column;
          gap: 4px;
        }
        .diag-outdoor-header {
          display: flex;
          justify-content: space-between;
          align-items: center;
          font-size: 12px;
        }
        .diag-outdoor-badge {
          background: rgba(16, 185, 129, 0.15);
          color: #10b981;
          font-size: 10px;
          font-weight: 700;
          padding: 2px 7px;
          border-radius: 6px;
        }
        .diag-outdoor-details {
          font-size: 11px;
          color: var(--secondary-text-color, #94a3b8);
          display: flex;
          flex-wrap: wrap;
          gap: 4px 8px;
        }
        .diag-outdoor-hint {
          font-size: 11px;
          color: var(--secondary-text-color, #94a3b8);
          font-style: italic;
          padding: 4px 0;
        }

        .sched-divider {
          border-top: 1px solid var(--divider-color, rgba(127,127,127,0.16));
          margin: 10px 0 12px;
        }
        .sched-header {
          display: flex;
          justify-content: space-between;
          align-items: center;
          gap: 8px;
          margin-bottom: 8px;
        }
        .sched-title-wrap {
          display: flex;
          align-items: center;
          gap: 8px;
          cursor: pointer;
          user-select: none;
        }
        .sched-title {
          font-size: 14px;
          font-weight: 700;
        }
        .sched-actions-top {
          display: flex;
          align-items: center;
          gap: 6px;
        }
        .pill-btn {
          border: 1px solid var(--divider-color, rgba(127,127,127,0.25));
          background: transparent;
          color: var(--primary-text-color);
          border-radius: 8px;
          padding: 4px 9px;
          font-size: 11px;
          font-weight: 600;
          cursor: pointer;
        }
        .pill-btn.on {
          background: rgba(16, 185, 129, 0.16);
          border-color: rgba(16, 185, 129, 0.45);
          color: #059669;
        }

        .hours-scale {
          display: grid;
          grid-template-columns: 38px 1fr;
          align-items: center;
          margin-bottom: 4px;
          font-size: 10px;
          color: var(--secondary-text-color, #64748b);
        }
        .hours-ticks {
          display: flex;
          justify-content: space-between;
          padding: 0 2px;
        }
        .week-rows {
          display: flex;
          flex-direction: column;
          gap: 5px;
        }
        .day-row {
          display: grid;
          grid-template-columns: 38px 1fr;
          align-items: center;
          gap: 6px;
          cursor: pointer;
          padding: 2px 0;
          border-radius: 8px;
        }
        .day-label {
          font-size: 12px;
          font-weight: 600;
          color: var(--secondary-text-color, #64748b);
        }
        .day-row.today .day-label {
          color: var(--primary-color, #0ea5e9);
          font-weight: 800;
        }
        .day-row.selected .day-label {
          color: var(--primary-text-color);
          text-decoration: underline;
        }
        .day-bar {
          position: relative;
          height: 22px;
          border-radius: 7px;
          overflow: hidden;
          display: flex;
          background: rgba(127, 127, 127, 0.12);
          box-shadow: inset 0 0 0 1px rgba(0,0,0,0.06);
        }
        .day-row.selected .day-bar {
          outline: 2px solid var(--primary-color, #0ea5e9);
          outline-offset: 1px;
        }
        .seg {
          height: 100%;
          display: flex;
          align-items: center;
          justify-content: center;
          font-size: 10px;
          font-weight: 700;
          color: #fff;
          overflow: hidden;
          white-space: nowrap;
          border-right: 1px solid rgba(255,255,255,0.22);
          box-sizing: border-box;
        }
        .seg:last-child { border-right: none; }
        .now-marker {
          position: absolute;
          top: -1px;
          bottom: -1px;
          width: 2px;
          background: #fff;
          box-shadow: 0 0 0 1px #0f172a;
          pointer-events: none;
          z-index: 2;
        }

        .legend {
          display: flex;
          flex-wrap: wrap;
          gap: 10px;
          margin-top: 8px;
          font-size: 11px;
          color: var(--secondary-text-color, #64748b);
        }
        .legend-item {
          display: inline-flex;
          align-items: center;
          gap: 4px;
        }
        .dot {
          width: 9px;
          height: 9px;
          border-radius: 50%;
          display: inline-block;
        }

        .day-editor {
          margin-top: 12px;
          padding: 12px;
          border-radius: 12px;
          background: rgba(127, 127, 127, 0.06);
          border: 1px solid var(--divider-color, rgba(127,127,127,0.18));
        }
        .editor-top {
          display: flex;
          justify-content: space-between;
          align-items: center;
          margin-bottom: 10px;
        }
        .editor-title {
          font-size: 13px;
          font-weight: 700;
        }
        .slot-list {
          display: flex;
          flex-direction: column;
          gap: 8px;
        }
        .slot-item {
          display: flex;
          flex-wrap: wrap;
          align-items: center;
          gap: 6px;
          padding: 7px 8px;
          border-radius: 9px;
          background: var(--card-background-color, #fff);
          border: 1px solid var(--divider-color, rgba(127,127,127,0.16));
        }
        .slot-time {
          font-size: 12px;
          font-weight: 600;
          min-width: 112px;
          display: flex;
          align-items: center;
          gap: 4px;
        }
        .slot-select, .time-select {
          padding: 5px 7px;
          border-radius: 7px;
          border: 1px solid var(--divider-color, rgba(127,127,127,0.28));
          background: var(--card-background-color, #fff);
          color: var(--primary-text-color);
          font-size: 12px;
          font-weight: 600;
        }
        .slot-select { flex: 1; min-width: 120px; }
        .mini-temp {
          display: inline-flex;
          align-items: center;
          gap: 4px;
          font-size: 12px;
          font-weight: 700;
        }
        .mini-btn {
          width: 24px;
          height: 24px;
          border-radius: 6px;
          border: 1px solid var(--divider-color, rgba(127,127,127,0.28));
          background: transparent;
          color: var(--primary-text-color);
          font-weight: 700;
          cursor: pointer;
        }
        .del-btn {
          border: none;
          background: rgba(239, 68, 68, 0.12);
          color: #ef4444;
          border-radius: 6px;
          width: 26px;
          height: 26px;
          font-weight: 700;
          cursor: pointer;
          margin-left: auto;
        }
        .copy-bar {
          display: flex;
          flex-wrap: wrap;
          align-items: center;
          gap: 6px;
          margin-top: 10px;
          padding-top: 10px;
          border-top: 1px dashed var(--divider-color, rgba(127,127,127,0.2));
          font-size: 11px;
          color: var(--secondary-text-color);
        }
        .preset-drawer {
          margin-top: 10px;
          padding: 10px 12px;
          border-radius: 12px;
          background: rgba(127, 127, 127, 0.06);
          display: grid;
          grid-template-columns: repeat(2, minmax(0, 1fr));
          gap: 8px;
        }
        @media (max-width: 420px) {
          .preset-drawer { grid-template-columns: 1fr; }
        }
        .preset-row {
          display: flex;
          justify-content: space-between;
          align-items: center;
          padding: 5px 8px;
          border-radius: 8px;
          background: var(--card-background-color, #fff);
          font-size: 12px;
          font-weight: 600;
        }
        .save-bar {
          display: flex;
          justify-content: space-between;
          align-items: center;
          gap: 8px;
          margin-top: 12px;
          padding: 9px 12px;
          border-radius: 10px;
          background: rgba(14, 165, 233, 0.12);
          border: 1px solid rgba(14, 165, 233, 0.35);
          font-size: 12px;
          font-weight: 600;
        }
        .save-btn {
          border: none;
          border-radius: 8px;
          padding: 6px 12px;
          background: var(--primary-color, #0ea5e9);
          color: #fff;
          font-size: 12px;
          font-weight: 700;
          cursor: pointer;
        }
        .toast {
          margin-top: 8px;
          font-size: 12px;
          font-weight: 600;
          color: #059669;
          text-align: right;
        }
      </style>
      <ha-card>
        <div id="header-root"></div>
        <div id="schedule-root"></div>
      </ha-card>
    `;
    this._skeletonBuilt = true;
  }

  async _loadSchedule(force) {
    if (!this._hass || !this._config?.entity) return;
    if (this._loadingSchedule && !force) return;
    this._loadingSchedule = true;
    try {
      const res = await this._hass.callWS({
        type: `${DOMAIN}/get_schedule`,
        entity_id: this._config.entity,
      });
      this._scheduleData = res;
      this._lastLoadedVersion = Number(res.version ?? 0);
      if (!this._dirty || force) {
        this._draftDays = JSON.parse(JSON.stringify(res.days || {}));
        this._draftPresets = JSON.parse(JSON.stringify(res.presets || {}));
        this._dirty = false;
      }
      this._renderHeader();
      this._renderSchedule();
    } catch (err) {
      // L'entité peut être en cours de démarrage au boot de HA
      console.warn("HA Thermostat Split : chargement du planning en attente", err);
    } finally {
      this._loadingSchedule = false;
    }
  }

  /* ------------------------------------------------------------------------
     RENDU EN-TÊTE TEMPS RÉEL (indépendant de l'éditeur de planning)
     ------------------------------------------------------------------------ */
  _renderHeader() {
    const root = this.shadowRoot?.getElementById("header-root");
    if (!root) return;
    if (this._config?.layout_mode === "schedule_only") {
      root.innerHTML = "";
      return;
    }

    if (!this._config?.entity) {
      root.innerHTML = `<div class="room-title">Sélectionnez votre thermostat dans l'éditeur</div>`;
      return;
    }
    const st = this._hass?.states?.[this._config.entity];
    if (!st) {
      root.innerHTML = `<div class="room-title">Entité introuvable : ${escapeHtml(this._config.entity)}</div>`;
      return;
    }

    const a = st.attributes || {};
    const title = this._config.title || a.friendly_name || this._config.entity;
    const isOff = st.state === "off";
    const action = a.hvac_action || (isOff ? "off" : "idle");
    const preset = a.preset_mode || "none";
    const currTemp = a.current_temperature;
    const shownTarget =
      this._pendingTarget !== null ? this._pendingTarget : a.temperature;
    const splitSent = a.consigne_envoyee_au_split;
    const overrideActive = Boolean(a.derogation_planning);
    const nextChange = a.prochain_changement;

    let badgeClass = "off";
    let badgeText = "Coupé";
    if (!isOff && action === "heating") {
      badgeClass = "heating";
      badgeText = splitSent ? `Chauffe • Split ${formatTemp(splitSent)} °C` : "Chauffe en cours";
    } else if (!isOff) {
      badgeClass = "idle";
      badgeText = "Maintien / Veille";
    }

    let subInfo = "";
    if (a.planning_actif && nextChange) {
      subInfo = `Prochain créneau à ${escapeHtml(nextChange)}`;
    } else if (a.decision) {
      subInfo = escapeHtml(a.decision);
    }

    root.innerHTML = `
      <div class="header-top">
        <div>
          <h3 class="room-title">${escapeHtml(title)}</h3>
          <div class="status-line">
            <span class="badge ${badgeClass}">${escapeHtml(badgeText)}</span>
            ${subInfo ? `<span>${subInfo}</span>` : ""}
          </div>
        </div>
      </div>

      <div class="temp-row">
        <div class="curr-temp-block">
          <span class="curr-label">Pièce</span>
          <span class="curr-temp">${formatTemp(currTemp)}<span class="curr-unit"> °C</span></span>
        </div>
        <div class="stepper">
          <button class="step-btn" id="btn-minus" title="Baisser de 0,5 °C">−</button>
          <div class="target-box">
            <div class="target-label">${isOff ? "État" : "Consigne"}</div>
            <div class="target-val">${isOff ? "OFF" : `${formatTemp(shownTarget)}°`}</div>
          </div>
          <button class="step-btn" id="btn-plus" title="Monter de 0,5 °C">+</button>
        </div>
      </div>

      <div class="modes-bar">
        <button class="mode-chip ${!isOff && preset === "schedule" ? "active" : ""}" data-preset="schedule">📅 Planning</button>
        <button class="mode-chip ${!isOff && preset === "comfort" ? "active" : ""}" data-preset="comfort">☀️ Confort</button>
        <button class="mode-chip ${!isOff && preset === "eco" ? "active" : ""}" data-preset="eco">🌙 Éco</button>
        <button class="mode-chip ${!isOff && preset === "boost" ? "active" : ""}" data-preset="boost">🔥 Boost</button>
        <button class="mode-chip ${!isOff && preset === "away" ? "active" : ""}" data-preset="away">🧳 Absent</button>
        <button class="mode-chip ${isOff ? "active-off" : ""}" data-hvac="off">⏻ Arrêt</button>
      </div>

      ${
        overrideActive
          ? `
        <div class="override-banner">
          <span>⏱️ Dérogation manuelle active${nextChange ? ` jusqu'à <strong>${escapeHtml(nextChange)}</strong>` : ""}</span>
          <button class="override-btn" id="btn-resume-sched">Reprendre</button>
        </div>
      `
          : ""
      }

      <div class="diag-toggle-bar">
        <button class="diag-toggle-btn" id="btn-toggle-diag">
          <span>📊 Diagnostic & Efficacité</span>
          <span class="diag-arrow">${this._showDiagnostics ? "▲" : "▼"}</span>
        </button>
      </div>

      ${
        this._showDiagnostics
          ? `
        <div class="diag-panel">
          <div class="diag-grid">
            <div class="diag-card">
              <div class="diag-card-title">⏱️ Inertie apprise</div>
              <div class="diag-card-val">${a.inertie_anticipee_min ?? 15} min</div>
              <div class="diag-card-sub">Anticipation anti-yoyo</div>
            </div>
            <div class="diag-card">
              <div class="diag-card-title">🌡️ Biais sonde split</div>
              <div class="diag-card-val">+${formatTemp(a.biais_appris ?? 1.5)} °C</div>
              <div class="diag-card-sub">Stratification plafond</div>
            </div>
            <div class="diag-card">
              <div class="diag-card-title">📈 Cycles analysés</div>
              <div class="diag-card-val">${a.cycles_appris ?? 0}</div>
              <div class="diag-card-sub">Précision : ${a.dernier_overshoot_deg !== null && a.dernier_overshoot_deg !== undefined ? `${a.dernier_overshoot_deg > 0 ? "+" : ""}${formatTemp(a.dernier_overshoot_deg)} °C` : "—"}</div>
            </div>
            <div class="diag-card">
              <div class="diag-card-title">💨 Vitesse chauffe</div>
              <div class="diag-card-val">${a.vitesse_chauffe_deg_h !== undefined ? `${a.vitesse_chauffe_deg_h > 0 ? "+" : ""}${formatTemp(a.vitesse_chauffe_deg_h)} °C/h` : "0,0 °C/h"}</div>
              <div class="diag-card-sub">${escapeHtml(a.statut_inverter || "Inverter actif")}</div>
            </div>
          </div>

          ${
            a.temperature_exterieure !== undefined && a.temperature_exterieure !== null
              ? `
            <div class="diag-outdoor-box">
              <div class="diag-outdoor-header">
                <span class="diag-outdoor-temp">❄️ Extérieur : <strong>${formatTemp(a.temperature_exterieure)} °C</strong></span>
                <span class="diag-outdoor-badge">${escapeHtml(a.efficacite_thermique_label || "Calcul inertie...")}</span>
              </div>
              <div class="diag-outdoor-details">
                ${a.constante_temps_tau_h ? `<span>Constante d'inertie : <strong>tau = ${formatTemp(a.constante_temps_tau_h)}h</strong></span>` : `<span>Constante d'inertie : <em>en cours d'apprentissage</em></span>`}
                ${a.deperdition_deg_h_pour_10k ? `<span>• Déperdition : <strong>${formatTemp(a.deperdition_deg_h_pour_10k)} °C/h</strong> (pour 10° d'écart)</span>` : ""}
                ${a.puissance_maintien_estimee_w ? `<span>• Puissance maintien : <strong>~${a.puissance_maintien_estimee_w} W</strong></span>` : ""}
              </div>
            </div>
          `
              : `
            <div class="diag-outdoor-hint">
              💡 <em>Astuce : configurez votre capteur extérieur dans l'intégration pour calculer l'inertie de la maison (tau) et l'anticipation météo.</em>
            </div>
          `
          }
        </div>
      `
          : ""
      }
    `;

    root.querySelector("#btn-minus")?.addEventListener("click", () => this._stepTarget(-0.5));
    root.querySelector("#btn-plus")?.addEventListener("click", () => this._stepTarget(+0.5));
    root.querySelectorAll("[data-preset]").forEach((btn) => {
      btn.addEventListener("click", () => this._selectPreset(btn.dataset.preset));
    });
    root.querySelector("[data-hvac='off']")?.addEventListener("click", () => this._turnOff());
    root.querySelector("#btn-resume-sched")?.addEventListener("click", () => this._clearOverride());
    root.querySelector("#btn-toggle-diag")?.addEventListener("click", () => {
      this._showDiagnostics = !this._showDiagnostics;
      this._renderHeader();
    });
  }

  _stepTarget(delta) {
    const st = this._hass?.states?.[this._config.entity];
    if (!st) return;
    const minT = Number(st.attributes?.min_temp ?? 15);
    const maxT = Number(st.attributes?.max_temp ?? 24);
    const base =
      this._pendingTarget !== null
        ? this._pendingTarget
        : Number(st.attributes?.temperature ?? 20);
    const next = Math.max(minT, Math.min(maxT, Math.round((base + delta) * 2) / 2));
    this._pendingTarget = next;
    this._renderHeader();

    if (this._tempDebounce) clearTimeout(this._tempDebounce);
    this._tempDebounce = setTimeout(async () => {
      const val = this._pendingTarget;
      this._pendingTarget = null;
      if (val === null || !this._hass) return;
      if (st.state === "off") {
        await this._hass.callService("climate", "set_hvac_mode", {
          entity_id: this._config.entity,
          hvac_mode: "heat",
        });
      }
      await this._hass.callService("climate", "set_temperature", {
        entity_id: this._config.entity,
        temperature: val,
      });
    }, 420);
  }

  async _selectPreset(preset) {
    if (!this._hass) return;
    await this._hass.callService("climate", "set_preset_mode", {
      entity_id: this._config.entity,
      preset_mode: preset,
    });
  }

  async _turnOff() {
    if (!this._hass) return;
    await this._hass.callService("climate", "set_hvac_mode", {
      entity_id: this._config.entity,
      hvac_mode: "off",
    });
  }

  async _clearOverride() {
    if (!this._hass) return;
    await this._hass.callWS({
      type: `${DOMAIN}/clear_override`,
      entity_id: this._config.entity,
    });
    await this._loadSchedule(true);
  }

  /* ------------------------------------------------------------------------
     RENDU PLANNING HEBDOMADAIRE 7 JOURS & ÉDITEUR TACTILE
     ------------------------------------------------------------------------ */
  _renderSchedule() {
    const root = this.shadowRoot?.getElementById("schedule-root");
    if (!root) return;
    const st = this._hass?.states?.[this._config?.entity];
    if (!st) {
      root.innerHTML = "";
      return;
    }

    if (!this._draftDays || !this._draftPresets) {
      root.innerHTML = `<div class="sched-divider"></div><div class="status-line">Chargement du planning hebdomadaire…</div>`;
      return;
    }

    const isCompact = this._config.layout_mode === "compact";
    const showDivider = this._config.layout_mode !== "schedule_only";
    const schedEnabled = Boolean(this._scheduleData?.enabled);

    // Jour courant (0 = Lundi .. 6 = Dimanche)
    const now = new Date();
    const todayIdx = (now.getDay() + 6) % 7;
    const nowPct = ((now.getHours() * 60 + now.getMinutes()) / 1440) * 100;

    let bodyHtml = "";
    if (!isCompact || this._scheduleExpanded) {
      const rowsHtml = DAY_LABELS_SHORT.map((label, dIdx) => {
        const slots = normalizeSlotsJS(
          this._draftDays[String(dIdx)],
          this._scheduleData?.min_temp ?? 15,
          this._scheduleData?.max_temp ?? 24
        );
        const segsHtml = slots
          .map((slot, i) => {
            const startM = parseMin(slot.start);
            const endM = i + 1 < slots.length ? parseMin(slots[i + 1].start) : 1440;
            const widthPct = ((endM - startM) / 1440) * 100;
            const bg = colorForSlot(slot, this._draftPresets);
            const txt = slotSummary(slot, this._draftPresets);
            const showLabel = endM - startM >= 180; // >= 3h
            const tooltip = `${slot.start} → ${endM === 1440 ? "24:00" : formatMin(endM)} : ${txt}`;
            return `<div class="seg" style="width:${widthPct.toFixed(3)}%;background:${bg}" title="${escapeHtml(tooltip)}">${showLabel ? escapeHtml(slot.mode === "off" ? "OFF" : slot.mode === "temp" ? `${formatTemp(slot.temp)}°` : `${formatTemp(this._draftPresets[slot.mode])}°`) : ""}</div>`;
          })
          .join("");

        return `
          <div class="day-row ${dIdx === todayIdx ? "today" : ""} ${this._selectedDay === dIdx ? "selected" : ""}" data-day="${dIdx}">
            <span class="day-label">${label}</span>
            <div class="day-bar">
              ${segsHtml}
              ${dIdx === todayIdx ? `<div class="now-marker" style="left:${nowPct.toFixed(2)}%"></div>` : ""}
            </div>
          </div>
        `;
      }).join("");

      bodyHtml = `
        <div class="hours-scale">
          <span></span>
          <div class="hours-ticks">
            <span>00h</span><span>06h</span><span>12h</span><span>18h</span><span>24h</span>
          </div>
        </div>
        <div class="week-rows">${rowsHtml}</div>
        <div class="legend">
          <span class="legend-item"><span class="dot" style="background:${MODE_META.off.color}"></span>Coupé</span>
          <span class="legend-item"><span class="dot" style="background:${MODE_META.away.color}"></span>Absent (${formatTemp(this._draftPresets.away)}°)</span>
          <span class="legend-item"><span class="dot" style="background:${MODE_META.eco.color}"></span>Éco (${formatTemp(this._draftPresets.eco)}°)</span>
          <span class="legend-item"><span class="dot" style="background:${MODE_META.comfort.color}"></span>Confort (${formatTemp(this._draftPresets.comfort)}°)</span>
          <span class="legend-item"><span class="dot" style="background:${MODE_META.boost.color}"></span>Boost (${formatTemp(this._draftPresets.boost)}°)</span>
          <span style="margin-left:auto;font-style:italic">Touchez un jour pour modifier</span>
        </div>
        ${this._showPresetTuning ? this._renderPresetDrawerHtml() : ""}
        ${this._selectedDay !== null ? this._renderDayEditorHtml(this._selectedDay) : ""}
        ${
          this._dirty
            ? `
          <div class="save-bar">
            <span>Modifications en attente</span>
            <div style="display:flex;gap:6px">
              <button class="pill-btn" id="btn-cancel-sched">Annuler</button>
              <button class="save-btn" id="btn-save-sched">${this._saving ? "Enregistrement…" : "Enregistrer"}</button>
            </div>
          </div>
        `
            : ""
        }
        ${this._toast ? `<div class="toast">${escapeHtml(this._toast)}</div>` : ""}
      `;
    }

    root.innerHTML = `
      ${showDivider ? `<div class="sched-divider"></div>` : ""}
      <div class="sched-header">
        <div class="sched-title-wrap" id="toggle-expand">
          <span class="sched-title">Planning hebdomadaire ${isCompact ? (this._scheduleExpanded ? "▾" : "▸") : ""}</span>
        </div>
        <div class="sched-actions-top">
          <button class="pill-btn" id="btn-toggle-presets" title="Régler les températures des modes">⚙️ Modes</button>
          <button class="pill-btn ${schedEnabled ? "on" : ""}" id="btn-toggle-enabled">
            ${schedEnabled ? "✓ Planning actif" : "Planning inactif"}
          </button>
        </div>
      </div>
      ${bodyHtml}
    `;

    this._bindScheduleEvents(root);
  }

  _renderPresetDrawerHtml() {
    const items = [
      ["comfort", "☀️ Confort"],
      ["eco", "🌙 Éco / Nuit"],
      ["boost", "🔥 Boost"],
      ["away", "🧳 Absent"],
    ];
    return `
      <div class="preset-drawer">
        ${items
          .map(
            ([key, label]) => `
          <div class="preset-row">
            <span>${label}</span>
            <span class="mini-temp">
              <button class="mini-btn" data-preset-step="${key}:-0.5">−</button>
              <span>${formatTemp(this._draftPresets[key])} °C</span>
              <button class="mini-btn" data-preset-step="${key}:0.5">+</button>
            </span>
          </div>
        `
          )
          .join("")}
      </div>
    `;
  }

  _renderDayEditorHtml(dayIdx) {
    const minT = this._scheduleData?.min_temp ?? 15;
    const maxT = this._scheduleData?.max_temp ?? 24;
    const slots = normalizeSlotsJS(this._draftDays[String(dayIdx)], minT, maxT);
    this._draftDays[String(dayIdx)] = slots;

    const allStarts = new Set(slots.map((s) => s.start));
    const slotsHtml = slots
      .map((slot, idx) => {
        const endLabel = idx + 1 < slots.length ? slots[idx + 1].start : "24:00";
        return `
          <div class="slot-item">
            <div class="slot-time">
              ${
                idx === 0
                  ? `<span>00:00</span>`
                  : `<select class="time-select" data-slot-time="${idx}">${buildTimeOptions(slot.start, allStarts)}</select>`
              }
              <span>→ ${endLabel}</span>
            </div>
            <select class="slot-select" data-slot-mode="${idx}">
              <option value="off" ${slot.mode === "off" ? "selected" : ""}>⏻ Coupé (OFF)</option>
              <option value="eco" ${slot.mode === "eco" ? "selected" : ""}>🌙 Éco (${formatTemp(this._draftPresets.eco)} °C)</option>
              <option value="comfort" ${slot.mode === "comfort" ? "selected" : ""}>☀️ Confort (${formatTemp(this._draftPresets.comfort)} °C)</option>
              <option value="boost" ${slot.mode === "boost" ? "selected" : ""}>🔥 Boost (${formatTemp(this._draftPresets.boost)} °C)</option>
              <option value="away" ${slot.mode === "away" ? "selected" : ""}>🧳 Absent (${formatTemp(this._draftPresets.away)} °C)</option>
              <option value="temp" ${slot.mode === "temp" ? "selected" : ""}>🌡️ Température libre…</option>
            </select>
            ${
              slot.mode === "temp"
                ? `
              <span class="mini-temp">
                <button class="mini-btn" data-slot-temp="${idx}:-0.5">−</button>
                <span>${formatTemp(slot.temp ?? 20)}°</span>
                <button class="mini-btn" data-slot-temp="${idx}:0.5">+</button>
              </span>
            `
                : ""
            }
            ${
              idx > 0
                ? `<button class="del-btn" data-slot-del="${idx}" title="Supprimer ce créneau">✕</button>`
                : ""
            }
          </div>
        `;
      })
      .join("");

    return `
      <div class="day-editor">
        <div class="editor-top">
          <span class="editor-title">Édition : ${DAY_LABELS_FULL[dayIdx]}</span>
          <div style="display:flex;gap:6px">
            <button class="pill-btn" id="btn-add-slot">+ Créneau</button>
            <button class="pill-btn" id="btn-close-day">Fermer</button>
          </div>
        </div>
        <div class="slot-list">${slotsHtml}</div>
        <div class="copy-bar">
          <span>Copier ${DAY_LABELS_SHORT[dayIdx]} sur :</span>
          <button class="pill-btn" data-copy-target="weekdays">Lun–Ven</button>
          <button class="pill-btn" data-copy-target="weekend">Sam–Dim</button>
          <button class="pill-btn" data-copy-target="all">Toute la semaine</button>
          ${DAY_LABELS_SHORT.map((lbl, i) =>
            i === dayIdx
              ? ""
              : `<button class="pill-btn" data-copy-day="${i}">${lbl}</button>`
          ).join("")}
        </div>
      </div>
    `;
  }

  _bindScheduleEvents(root) {
    root.querySelector("#toggle-expand")?.addEventListener("click", () => {
      if (this._config.layout_mode === "compact") {
        this._scheduleExpanded = !this._scheduleExpanded;
        this._renderSchedule();
      }
    });

    root.querySelector("#btn-toggle-presets")?.addEventListener("click", () => {
      this._scheduleExpanded = true;
      this._showPresetTuning = !this._showPresetTuning;
      this._renderSchedule();
    });

    root.querySelector("#btn-toggle-enabled")?.addEventListener("click", async () => {
      const nextEnabled = !Boolean(this._scheduleData?.enabled);
      const payload = { enabled: nextEnabled };
      if (this._dirty) {
        payload.days = this._draftDays;
        payload.presets = this._draftPresets;
      }
      await this._saveToBackend(payload);
    });

    root.querySelectorAll(".day-row").forEach((row) => {
      row.addEventListener("click", () => {
        const d = parseInt(row.dataset.day, 10);
        this._selectedDay = this._selectedDay === d ? null : d;
        this._renderSchedule();
      });
    });

    root.querySelectorAll("[data-preset-step]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const [key, rawDelta] = btn.dataset.presetStep.split(":");
        const minT = this._scheduleData?.min_temp ?? 15;
        const maxT = this._scheduleData?.max_temp ?? 24;
        const cur = Number(this._draftPresets[key] ?? 20);
        this._draftPresets[key] = Math.max(
          minT,
          Math.min(maxT, Math.round((cur + Number(rawDelta)) * 2) / 2)
        );
        this._dirty = true;
        this._renderSchedule();
      });
    });

    root.querySelector("#btn-close-day")?.addEventListener("click", () => {
      this._selectedDay = null;
      this._renderSchedule();
    });

    root.querySelector("#btn-add-slot")?.addEventListener("click", () => {
      if (this._selectedDay === null) return;
      const key = String(this._selectedDay);
      const slots = normalizeSlotsJS(this._draftDays[key]);
      // Trouve la plus grande plage horaire pour l'y insérer proprement au milieu
      let bestIdx = slots.length - 1;
      let bestSpan = -1;
      for (let i = 0; i < slots.length; i++) {
        const s = parseMin(slots[i].start);
        const e = i + 1 < slots.length ? parseMin(slots[i + 1].start) : 1440;
        if (e - s > bestSpan) {
          bestSpan = e - s;
          bestIdx = i;
        }
      }
      const sMin = parseMin(slots[bestIdx].start);
      const eMin = bestIdx + 1 < slots.length ? parseMin(slots[bestIdx + 1].start) : 1440;
      const mid = Math.round((sMin + eMin) / 2 / 15) * 15;
      if (mid > sMin && mid < eMin) {
        const nextMode = slots[bestIdx].mode === "comfort" ? "eco" : "comfort";
        slots.push({ start: formatMin(mid), mode: nextMode, temp: null });
        this._draftDays[key] = normalizeSlotsJS(slots);
        this._dirty = true;
        this._renderSchedule();
      }
    });

    root.querySelectorAll("[data-slot-time]").forEach((sel) => {
      sel.addEventListener("change", (e) => {
        const idx = parseInt(sel.dataset.slotTime, 10);
        const key = String(this._selectedDay);
        const slots = [...this._draftDays[key]];
        slots[idx] = { ...slots[idx], start: e.target.value };
        this._draftDays[key] = normalizeSlotsJS(slots);
        this._dirty = true;
        this._renderSchedule();
      });
    });

    root.querySelectorAll("[data-slot-mode]").forEach((sel) => {
      sel.addEventListener("change", (e) => {
        const idx = parseInt(sel.dataset.slotMode, 10);
        const key = String(this._selectedDay);
        const slots = [...this._draftDays[key]];
        const mode = e.target.value;
        slots[idx] = {
          ...slots[idx],
          mode,
          temp: mode === "temp" ? slots[idx].temp ?? 20.0 : null,
        };
        this._draftDays[key] = slots;
        this._dirty = true;
        this._renderSchedule();
      });
    });

    root.querySelectorAll("[data-slot-temp]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const [idxStr, deltaStr] = btn.dataset.slotTemp.split(":");
        const idx = parseInt(idxStr, 10);
        const minT = this._scheduleData?.min_temp ?? 15;
        const maxT = this._scheduleData?.max_temp ?? 24;
        const key = String(this._selectedDay);
        const slots = [...this._draftDays[key]];
        const cur = Number(slots[idx].temp ?? 20);
        slots[idx] = {
          ...slots[idx],
          temp: Math.max(minT, Math.min(maxT, Math.round((cur + Number(deltaStr)) * 2) / 2)),
        };
        this._draftDays[key] = slots;
        this._dirty = true;
        this._renderSchedule();
      });
    });

    root.querySelectorAll("[data-slot-del]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const idx = parseInt(btn.dataset.slotDel, 10);
        const key = String(this._selectedDay);
        const slots = this._draftDays[key].filter((_, i) => i !== idx);
        this._draftDays[key] = normalizeSlotsJS(slots);
        this._dirty = true;
        this._renderSchedule();
      });
    });

    root.querySelectorAll("[data-copy-target]").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (this._selectedDay === null) return;
        const src = JSON.stringify(this._draftDays[String(this._selectedDay)]);
        const target = btn.dataset.copyTarget;
        const indices =
          target === "weekdays"
            ? [0, 1, 2, 3, 4]
            : target === "weekend"
              ? [5, 6]
              : [0, 1, 2, 3, 4, 5, 6];
        for (const i of indices) {
          this._draftDays[String(i)] = JSON.parse(src);
        }
        this._dirty = true;
        this._showToast("Journée copiée (pensez à enregistrer)");
        this._renderSchedule();
      });
    });

    root.querySelectorAll("[data-copy-day]").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (this._selectedDay === null) return;
        const targetIdx = parseInt(btn.dataset.copyDay, 10);
        this._draftDays[String(targetIdx)] = JSON.parse(
          JSON.stringify(this._draftDays[String(this._selectedDay)])
        );
        this._dirty = true;
        this._showToast(`Copié sur ${DAY_LABELS_FULL[targetIdx]}`);
        this._renderSchedule();
      });
    });

    root.querySelector("#btn-cancel-sched")?.addEventListener("click", () => {
      if (this._scheduleData) {
        this._draftDays = JSON.parse(JSON.stringify(this._scheduleData.days || {}));
        this._draftPresets = JSON.parse(JSON.stringify(this._scheduleData.presets || {}));
      }
      this._dirty = false;
      this._renderSchedule();
    });

    root.querySelector("#btn-save-sched")?.addEventListener("click", async () => {
      await this._saveToBackend({
        days: this._draftDays,
        presets: this._draftPresets,
        enabled: true,
      });
    });
  }

  async _saveToBackend(payload) {
    if (!this._hass || this._saving) return;
    this._saving = true;
    this._renderSchedule();
    try {
      const res = await this._hass.callWS({
        type: `${DOMAIN}/set_schedule`,
        entity_id: this._config.entity,
        apply_now: true,
        ...payload,
      });
      this._scheduleData = res;
      this._lastLoadedVersion = Number(res.version ?? 0);
      this._draftDays = JSON.parse(JSON.stringify(res.days || {}));
      this._draftPresets = JSON.parse(JSON.stringify(res.presets || {}));
      this._dirty = false;
      this._showToast("Planning enregistré ✓");
    } catch (err) {
      console.error("Erreur lors de la sauvegarde du planning", err);
      this._showToast("Erreur lors de la sauvegarde");
    } finally {
      this._saving = false;
      this._renderHeader();
      this._renderSchedule();
    }
  }

  _showToast(msg) {
    this._toast = msg;
    if (this._toastTimer) clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => {
      this._toast = "";
      this._renderSchedule();
    }, 3000);
  }
}

if (!customElements.get("ha-thermostat-split-card-editor")) {
  customElements.define("ha-thermostat-split-card-editor", HAThermostatSplitCardEditor);
}
if (!customElements.get("ha-thermostat-split-card")) {
  customElements.define("ha-thermostat-split-card", HAThermostatSplitCard);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "ha-thermostat-split-card")) {
  window.customCards.push({
    type: "ha-thermostat-split-card",
    name: "HA Thermostat Split — Planning & Régulation",
    description:
      "Pilotage Inverter et planning hebdomadaire visuel 7 jours (façon Netatmo / Comfort Cloud).",
    preview: true,
    documentationURL: "https://github.com/maximeblanchard/ha-thermostat-split",
    getEntitySuggestion: (hass, entityId) => {
      if (!entityId || !entityId.startsWith("climate.")) return null;
      const st = hass?.states?.[entityId];
      if (!st || st.attributes?.planning_version === undefined) return null;
      return {
        config: {
          type: "custom:ha-thermostat-split-card",
          entity: entityId,
          layout_mode: "full",
        },
      };
    },
  });
}
