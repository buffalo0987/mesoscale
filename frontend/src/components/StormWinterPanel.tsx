import React, { useEffect, useState } from 'react';
import {
  ChevronDown,
  ChevronUp,
  CloudLightning,
  Flame,
  ShieldAlert,
  Snowflake,
  ThermometerSnowflake,
} from 'lucide-react';
import type { ForecastBundle, SpcRiskDay, UnitSystem } from '../types';
import { formatTemp } from '../units';

interface StormWinterPanelProps {
  bundle: ForecastBundle;
  units: UnitSystem;
}

const CAP_STATUS_LABELS: Record<
  string,
  { label: string; badgeClass: string; desc: string }
> = {
  none: {
    label: 'Low Instability',
    badgeClass: 'bg-slate-800 text-slate-300 border-slate-700',
    desc: 'CAPE below 500 J/kg — surface-based severe storms unlikely.',
  },
  strong_cap: {
    label: 'Strong Cap Holding',
    badgeClass: 'bg-amber-500/15 text-amber-300 border-amber-400/35',
    desc: 'Elevated warm nose (|CIN| ≥ 100 J/kg) suppressing storm initiation despite instability.',
  },
  breakable_cap: {
    label: 'Eroding / Breakable Cap',
    badgeClass: 'bg-orange-500/20 text-orange-200 border-orange-400/40',
    desc: 'Moderate inhibition (35–99 J/kg) — dryline/frontal convergence could breach the cap.',
  },
  uncapped_initiation: {
    label: 'Uncapped Initiation',
    badgeClass: 'bg-rose-500/20 text-rose-200 border-rose-400/50',
    desc: 'Minimal convective inhibition (< 35 J/kg) over rich CAPE — explosive storm development favored.',
  },
};

const PTYPE_STYLE: Record<string, { label: string; color: string }> = {
  freezing_rain: {
    label: 'Freezing Rain',
    color: 'bg-fuchsia-500/25 border-fuchsia-400/50 text-fuchsia-200',
  },
  sleet: {
    label: 'Sleet / Ice Pellets',
    color: 'bg-indigo-500/25 border-indigo-400/50 text-indigo-200',
  },
  snow: {
    label: 'Snow',
    color: 'bg-cyan-500/20 border-cyan-300/40 text-cyan-100',
  },
  rain: {
    label: 'Cold Rain',
    color: 'bg-sky-500/15 border-sky-400/30 text-sky-200',
  },
};

function renderSpcPill(dayLabel: string, risk: SpcRiskDay) {
  const isNone = risk.code === 'NONE';
  return (
    <div
      key={dayLabel}
      className="flex items-center justify-between p-2.5 rounded-xl bg-slate-900/80 border border-slate-800/90"
    >
      <span className="text-xs text-slate-400 font-medium">{dayLabel}</span>
      <span
        className="px-2.5 py-0.5 rounded-full text-[11px] font-bold tracking-wide border"
        style={{
          backgroundColor: isNone ? 'rgba(30,41,59,0.7)' : `${risk.color}26`,
          borderColor: isNone ? 'rgba(71,85,105,0.5)' : risk.color,
          color: isNone ? '#94a3b8' : risk.color,
        }}
      >
        {isNone ? 'No Thunder Risk' : `${risk.code} · ${risk.label}`}
      </span>
    </div>
  );
}

export const StormWinterPanel: React.FC<StormWinterPanelProps> = ({ bundle, units }) => {
  const sw = bundle.storm_winter;
  const hailRisk = bundle.hail_risk;
  const spcHail = sw.spc.day1_hail;
  const [expanded, setExpanded] = useState<boolean>(sw.auto_active);

  useEffect(() => {
    setExpanded(sw.auto_active);
  }, [sw.auto_active, bundle.location.lat, bundle.location.lon]);

  const hours48 = bundle.hourly.slice(0, 48);
  const peakCapeHour = hours48.reduce(
    (best, h) => (h.cape_jkg > (best?.cape_jkg ?? -1) ? h : best),
    hours48[0]
  );
  const capMeta = CAP_STATUS_LABELS[peakCapeHour?.cap_status || 'none'] || CAP_STATUS_LABELS.none;
  const capePct = Math.min(100, Math.round((sw.peak_cape_jkg / 4000) * 100));
  const cinMag = Math.abs(sw.cin_at_peak_cape_jkg);
  const cinPct = Math.min(100, Math.round((cinMag / 250) * 100));

  const winterPrecipHours = hours48.filter(
    (h) =>
      h.precip_type === 'freezing_rain' ||
      h.precip_type === 'sleet' ||
      h.precip_type === 'snow' ||
      (h.precip_prob >= 0.25 && h.wet_bulb_c <= 3.0)
  );

  return (
    <section className="glass-panel rounded-2xl overflow-hidden">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full px-3.5 sm:px-5 py-3 sm:py-3.5 flex items-center justify-between gap-2.5 text-left hover:bg-slate-900/40 transition cursor-pointer"
      >
        <div className="flex flex-wrap items-center gap-2">
          <span className="inline-flex items-center gap-1.5 text-xs sm:text-sm font-semibold text-white">
            <CloudLightning className="w-4 h-4 text-amber-400 shrink-0" />
            Storm & Winter Diagnostics
          </span>
          {sw.auto_active ? (
            <span className="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wider bg-amber-500/20 text-amber-300 border border-amber-400/40">
              Active Mesoscale Signal
            </span>
          ) : (
            <span className="px-2 py-0.5 rounded-full text-[10px] font-medium bg-slate-800/90 text-slate-400 border border-slate-700/70">
              <span className="sm:hidden">Quiet Regime · Tap to Inspect</span>
              <span className="hidden sm:inline">
                Quiet Regime · Tap to Inspect SPC / CAPE / Wet-Bulb
              </span>
            </span>
          )}
          {sw.freezing_rain_hours_48h > 0 && (
            <span className="px-2 py-0.5 rounded-full text-[10px] font-bold bg-fuchsia-500/25 text-fuchsia-200 border border-fuchsia-400/50">
              Ice Glaze Risk ({sw.freezing_rain_hours_48h}h)
            </span>
          )}
          {hailRisk &&
            (hailRisk.status === 'imminent' ||
              hailRisk.status === 'approaching' ||
              hailRisk.status === 'watch') && (
              <span
                className={`px-2 py-0.5 rounded-full text-[10px] font-bold border ${
                  hailRisk.status === 'watch'
                    ? 'bg-amber-500/20 text-amber-200 border-amber-400/40'
                    : 'bg-rose-500/25 text-rose-200 border-rose-400/60'
                }`}
              >
                {hailRisk.status === 'watch'
                  ? `Hail Watch (${
                      hailRisk.spc_day1_hail_prob_pct > 0
                        ? `${hailRisk.spc_day1_hail_prob_pct}% SPC`
                        : `${hailRisk.nearby_cells_count} Cell`
                    })`
                  : `Hail Shield · ${
                      hailRisk.eta_minutes !== null
                        ? `~${hailRisk.eta_minutes}m`
                        : hailRisk.hail_label
                    }`}
              </span>
            )}
        </div>
        <span className="text-slate-400 shrink-0">
          {expanded ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
        </span>
      </button>

      {expanded && (
        <div className="px-3.5 sm:px-5 pb-4 sm:pb-5 pt-2 border-t border-slate-800/80 grid grid-cols-1 lg:grid-cols-3 gap-3.5 sm:gap-4">
          {/* Column 1: NOAA SPC Day 1-3 Convective + Hail Watchdog */}
          <div className="p-4 rounded-xl bg-slate-950/60 border border-slate-800/80 space-y-2.5">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-slate-200 flex items-center gap-1.5">
                <ShieldAlert className="w-3.5 h-3.5 text-amber-400" />
                NOAA SPC & Hail Watchdog
              </span>
              <span className="text-[10px] text-slate-500">Polygon + NEXRAD L3</span>
            </div>
            <div className="space-y-2">
              {renderSpcPill('Day 1 (Today)', sw.spc.day1)}
              {renderSpcPill('Day 2 (Tomorrow)', sw.spc.day2)}
              {renderSpcPill('Day 3', sw.spc.day3)}
              {spcHail && (
                <div className="flex items-center justify-between gap-2 p-2.5 rounded-xl bg-slate-900/80 border border-slate-800/90">
                  <span className="text-xs text-slate-400 font-medium">Day 1 Hail Prob</span>
                  <span
                    className={`px-2.5 py-0.5 rounded-full text-[11px] font-bold border ${
                      spcHail.sig_hail
                        ? 'bg-fuchsia-500/25 border-fuchsia-400/60 text-fuchsia-200'
                        : spcHail.prob_pct >= 15
                          ? 'bg-amber-500/20 border-amber-400/50 text-amber-200'
                          : spcHail.prob_pct >= 5
                            ? 'bg-emerald-500/15 border-emerald-400/40 text-emerald-200'
                            : 'bg-slate-800/70 border-slate-700/60 text-slate-400'
                    }`}
                  >
                    {spcHail.label}
                  </span>
                </div>
              )}
              {hailRisk && (
                <div
                  className={`p-2.5 rounded-xl border text-[11px] leading-snug ${
                    hailRisk.status === 'imminent' || hailRisk.status === 'approaching'
                      ? 'bg-rose-950/60 border-rose-500/50 text-rose-100'
                      : hailRisk.status === 'watch'
                        ? 'bg-amber-950/40 border-amber-500/35 text-amber-100'
                        : 'bg-slate-900/60 border-slate-800 text-slate-400'
                  }`}
                >
                  <span className="font-semibold text-slate-200">2-Min NEXRAD Shield: </span>
                  {hailRisk.headline}
                </div>
              )}
            </div>
          </div>

          {/* Column 2: HRRR CAPE vs CIN Cap Strength Gauge */}
          <div className="p-4 rounded-xl bg-slate-950/60 border border-slate-800/80 space-y-3">
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs font-semibold text-slate-200 flex items-center gap-1.5">
                <Flame className="w-3.5 h-3.5 text-orange-400" />
                HRRR CAPE vs. Cap (CIN)
              </span>
              <span className={`px-2 py-0.5 rounded-full text-[10px] font-semibold border ${capMeta.badgeClass}`}>
                {capMeta.label}
              </span>
            </div>

            <div className="space-y-2 text-xs">
              <div>
                <div className="flex justify-between text-[11px] mb-1">
                  <span className="text-slate-400">48h Peak Surface CAPE</span>
                  <span className="font-mono font-semibold text-amber-300">
                    {Math.round(sw.peak_cape_jkg).toLocaleString()} J/kg
                  </span>
                </div>
                <div className="h-2 rounded-full bg-slate-900 overflow-hidden">
                  <div
                    className="h-full bg-gradient-to-r from-emerald-400 via-amber-400 to-rose-500"
                    style={{ width: `${Math.max(3, capePct)}%` }}
                  />
                </div>
              </div>

              <div>
                <div className="flex justify-between text-[11px] mb-1">
                  <span className="text-slate-400">Cap Inhibition (|CIN| at Peak)</span>
                  <span className="font-mono font-semibold text-cyan-300">
                    -{Math.round(cinMag)} J/kg
                  </span>
                </div>
                <div className="h-2 rounded-full bg-slate-900 overflow-hidden">
                  <div
                    className="h-full bg-gradient-to-r from-cyan-400 to-indigo-500"
                    style={{ width: `${Math.max(3, cinPct)}%` }}
                  />
                </div>
              </div>
            </div>

            <p className="text-[11px] text-slate-400 leading-relaxed">{capMeta.desc}</p>
          </div>

          {/* Column 3: Wet-Bulb Winter Phase Classification */}
          <div className="p-4 rounded-xl bg-slate-950/60 border border-slate-800/80 space-y-2.5">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-slate-200 flex items-center gap-1.5">
                <ThermometerSnowflake className="w-3.5 h-3.5 text-cyan-400" />
                Wet-Bulb (Tw) Winter Phase
              </span>
              <span className="text-[10px] text-slate-500">48h Thermal Profile</span>
            </div>

            {winterPrecipHours.length === 0 ? (
              <div className="h-28 rounded-xl bg-slate-900/50 border border-slate-800/60 flex flex-col items-center justify-center text-center px-4 gap-1">
                <Snowflake className="w-4 h-4 text-slate-500" />
                <span className="text-xs text-slate-400">
                  No sub-freezing or mixed-phase precipitation modeled in the next 48 hours.
                </span>
              </div>
            ) : (
              <div className="space-y-1.5 max-h-36 overflow-y-auto pr-1">
                {winterPrecipHours.slice(0, 10).map((h) => {
                  const dt = new Date(h.valid_time_utc);
                  const style = PTYPE_STYLE[h.precip_type] || PTYPE_STYLE.rain;
                  return (
                    <div
                      key={h.valid_time_utc}
                      className="flex items-center justify-between text-[11px] p-2 rounded-lg bg-slate-900/80 border border-slate-800"
                    >
                      <span className="text-slate-300 font-medium">
                        {dt.toLocaleDateString([], { weekday: 'short' })}{' '}
                        {dt.toLocaleTimeString([], { hour: 'numeric' })}
                      </span>
                      <span className="text-slate-400 font-mono">
                        T {formatTemp(h.temp_p50_c, units)} · Tw {formatTemp(h.wet_bulb_c, units)}
                      </span>
                      <span className={`px-2 py-0.5 rounded text-[10px] font-semibold border ${style.color}`}>
                        {style.label}
                      </span>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      )}
    </section>
  );
};
