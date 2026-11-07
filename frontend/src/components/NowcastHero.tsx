import React, { useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  CloudLightning,
  CloudRain,
  CloudSnow,
  Cpu,
  Droplets,
  Gauge,
  Loader2,
  RefreshCw,
  Snowflake,
  Sun,
  Wind,
} from 'lucide-react';
import { api } from '../api';
import type { ForecastBundle, PinnedLocation, UnitSystem } from '../types';
import {
  convertDeltaTemp,
  degToCompass,
  formatPrecip,
  formatTemp,
  formatWind,
} from '../units';

interface NowcastHeroProps {
  bundle: ForecastBundle;
  units: UnitSystem;
  onRefresh: () => void;
  onPinnedFromEphemeral: (loc: PinnedLocation) => void;
  isRefreshing: boolean;
}

const CHECKIN_OPTIONS: Array<{
  id: 'dry' | 'rain' | 'tstorm' | 'freezing_rain' | 'sleet' | 'snow';
  label: string;
  icon: React.ReactNode;
}> = [
  { id: 'dry', label: 'Dry Here', icon: <Sun className="w-3.5 h-3.5 text-amber-300" /> },
  { id: 'rain', label: 'Rain', icon: <CloudRain className="w-3.5 h-3.5 text-sky-400" /> },
  { id: 'tstorm', label: 'T-Storm', icon: <CloudLightning className="w-3.5 h-3.5 text-yellow-400" /> },
  { id: 'freezing_rain', label: 'Freezing Rain', icon: <Droplets className="w-3.5 h-3.5 text-fuchsia-400" /> },
  { id: 'sleet', label: 'Sleet', icon: <Snowflake className="w-3.5 h-3.5 text-indigo-300" /> },
  { id: 'snow', label: 'Snow', icon: <CloudSnow className="w-3.5 h-3.5 text-white" /> },
];

export const NowcastHero: React.FC<NowcastHeroProps> = ({
  bundle,
  units,
  onRefresh,
  onPinnedFromEphemeral,
  isRefreshing,
}) => {
  const [checkinSent, setCheckinSent] = useState<string | null>(null);
  const [pinning, setPinning] = useState(false);

  const curHour = bundle.hourly[0];
  const obs = bundle.current_observation;
  const loc = bundle.location;
  const eng = bundle.engine_info;
  const displayTempC = obs?.temp_c ?? curHour?.temp_p50_c ?? null;
  const feelsC = obs?.feels_like_c ?? curHour?.feels_like_c ?? null;
  const wetBulbC = obs?.wet_bulb_c ?? curHour?.wet_bulb_c ?? null;
  const dewpointC = obs?.dewpoint_c ?? curHour?.dewpoint_p50_c ?? null;
  const windMs = obs?.wind_ms ?? curHour?.wind_p50_ms ?? null;
  const gustMs = obs?.gust_ms ?? curHour?.gust_p50_ms ?? null;
  const windDir = obs?.wind_dir_deg ?? curHour?.wind_dir_deg ?? null;

  const kalmanDelta = convertDeltaTemp(eng.kalman_bias_c || 0, units);

  const handleCheckin = async (
    cond: 'dry' | 'rain' | 'tstorm' | 'freezing_rain' | 'sleet' | 'snow'
  ) => {
    if (!loc.id) return;
    await api.submitCheckin(loc.id, cond);
    setCheckinSent(cond);
    setTimeout(() => setCheckinSent(null), 4000);
    onRefresh();
  };

  const handlePinEphemeral = async () => {
    setPinning(true);
    try {
      const res = await api.pinLocation({
        name: loc.name,
        admin1: loc.admin1,
        lat: loc.lat,
        lon: loc.lon,
        elevation_m: loc.elevation_m,
        timezone: loc.timezone,
      });
      onPinnedFromEphemeral(res.location);
    } finally {
      setPinning(false);
    }
  };

  return (
    <section className="space-y-4">
      {/* Severe NWS Alert Banners */}
      {bundle.alerts.map((alert) => (
        <details
          key={alert.id || alert.event}
          className={`rounded-2xl border p-4 ${
            alert.is_life_threatening
              ? 'bg-rose-950/70 border-rose-500/60 text-rose-100'
              : 'bg-amber-950/50 border-amber-500/40 text-amber-100'
          }`}
        >
          <summary className="cursor-pointer font-semibold text-sm flex items-center justify-between gap-2">
            <span className="flex items-center gap-2">
              <AlertTriangle className="w-4 h-4 shrink-0 text-rose-400" />
              {alert.event}: {alert.headline}
            </span>
            <span className="text-xs opacity-75 shrink-0">Details</span>
          </summary>
          <p className="mt-2.5 text-xs leading-relaxed whitespace-pre-line opacity-90">
            {alert.description}
          </p>
          {alert.instruction && (
            <p className="mt-2 text-xs font-medium text-white bg-black/25 p-2.5 rounded-lg">
              Action: {alert.instruction}
            </p>
          )}
        </details>
      ))}

      {/* Day-1 Cold-Start Backfill Progress OR Ephemeral Pin CTA */}
      {bundle.mode === 'ephemeral_consensus' && (
        <div className="rounded-2xl p-4 bg-indigo-500/12 border border-indigo-400/30 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
          <div className="text-xs text-indigo-100 space-y-0.5">
            <div className="font-semibold text-sm text-white">
              Instant 6-Model Consensus + Live Station Kalman Mode
            </div>
            <p className="text-indigo-200/80">
              Pin {loc.name} to backfill 365 days of local forecast errors, train dedicated CatBoost + LightGBM models, and enable 24/7 push alerts.
            </p>
          </div>
          <button
            type="button"
            onClick={handlePinEphemeral}
            disabled={pinning}
            className="px-4 py-2.5 rounded-xl bg-cyan-500 hover:bg-cyan-400 text-slate-950 font-semibold text-xs shrink-0 flex items-center gap-2 cursor-pointer"
          >
            {pinning ? <Loader2 className="w-4 h-4 animate-spin" /> : <Cpu className="w-4 h-4" />}
            Pin & Train Local AI
          </button>
        </div>
      )}

      {loc.id && (eng.backfill_status === 'running' || eng.backfill_status === 'pending') && (
        <div className="rounded-2xl p-4 bg-cyan-950/40 border border-cyan-500/30 space-y-2">
          <div className="flex items-center justify-between text-xs">
            <span className="font-semibold text-cyan-200 flex items-center gap-2">
              <Loader2 className="w-3.5 h-3.5 animate-spin text-cyan-400" />
              Day-1 Historical Backfill & Local AI Training
            </span>
            <span className="text-cyan-300 font-mono">
              {Math.round((eng.backfill_progress || 0.05) * 100)}%
            </span>
          </div>
          <div className="w-full h-2 rounded-full bg-slate-900 overflow-hidden">
            <div
              className="h-full bg-gradient-to-r from-cyan-400 to-indigo-400 transition-all duration-500"
              style={{ width: `${Math.max(6, Math.round((eng.backfill_progress || 0.05) * 100))}%` }}
            />
          </div>
          <div className="text-[11px] text-slate-300">{eng.backfill_message}</div>
        </div>
      )}

      {/* Main Telemetry Hero Card */}
      <div className="glass-panel rounded-2xl p-5 sm:p-7">
        <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-6">
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-xl sm:text-2xl font-bold text-white">
                {loc.name}
                {loc.admin1 ? `, ${loc.admin1}` : ''}
              </h2>
              <span
                className={`px-2.5 py-0.5 rounded-full text-[11px] font-semibold border ${
                  eng.model_status === 'trained'
                    ? 'bg-emerald-500/15 border-emerald-400/30 text-emerald-300'
                    : 'bg-cyan-500/15 border-cyan-400/30 text-cyan-300'
                }`}
              >
                {eng.model_status === 'trained'
                  ? `Learned MOS v${eng.model_version || 1} (${(eng.train_rows || 0).toLocaleString()} hrs)`
                  : 'Multi-Model Consensus + Kalman'}
              </span>
              {Math.abs(kalmanDelta) >= 0.4 && (
                <span className="px-2.5 py-0.5 rounded-full text-[11px] font-medium bg-amber-500/15 border border-amber-400/30 text-amber-200">
                  5m Kalman Nudge {kalmanDelta > 0 ? '+' : ''}
                  {kalmanDelta.toFixed(1)}°{units === 'imperial' ? 'F' : 'C'}
                </span>
              )}
              <button
                type="button"
                onClick={onRefresh}
                disabled={isRefreshing}
                title="Refresh live models & Mesonet observations"
                className="p-1.5 rounded-lg bg-slate-800/80 hover:bg-slate-700 text-slate-300 cursor-pointer"
              >
                <RefreshCw className={`w-3.5 h-3.5 ${isRefreshing ? 'animate-spin text-cyan-400' : ''}`} />
              </button>
            </div>

            <div className="flex items-baseline gap-4">
              <div className="text-5xl sm:text-6xl font-extrabold tracking-tight text-white">
                {formatTemp(displayTempC, units, 0)}
              </div>
              {curHour && (
                <div className="space-y-0.5 text-xs text-slate-400">
                  <div>
                    Feels like <span className="text-slate-200 font-semibold">{formatTemp(feelsC, units)}</span>
                  </div>
                  <div>
                    Next hr 80% band:{' '}
                    <span className="text-cyan-300 font-medium">
                      {formatTemp(curHour.temp_p10_c, units)} – {formatTemp(curHour.temp_p90_c, units)}
                    </span>
                  </div>
                </div>
              )}
            </div>

            {curHour?.bust_risk_level !== 'none' && curHour?.bust_summary && (
              <div className="inline-flex items-center gap-2 px-3 py-1.5 rounded-xl bg-amber-500/15 border border-amber-400/35 text-amber-200 text-xs">
                <AlertTriangle className="w-3.5 h-3.5 text-amber-400 shrink-0" />
                {curHour.bust_summary}
              </div>
            )}

            {/* Paired Research-Grade Ground Truth Towers */}
            {loc.stations?.length > 0 && (
              <div className="text-[11px] text-slate-400 flex flex-wrap items-center gap-x-2 gap-y-1">
                <span className="text-slate-500">Ground truth blend:</span>
                {loc.stations.map((s) => (
                  <span
                    key={s.station_id}
                    className="px-2 py-0.5 rounded bg-slate-900/90 border border-slate-800 text-slate-300"
                  >
                    {s.station_id} ({s.source === 'ok_mesonet' ? 'OK Mesonet 5m' : 'ASOS'} ·{' '}
                    {(s.distance_km * 0.621371).toFixed(1)} mi · {Math.round(s.blend_weight * 100)}%)
                  </span>
                ))}
                {obs && (
                  <span className="text-emerald-400">• Obs {obs.age_minutes}m ago</span>
                )}
              </div>
            )}
          </div>

          {/* Right-hand Meteorological Vitals Grid */}
          <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-2 gap-3 shrink-0 lg:w-80">
            <div className="p-3 rounded-xl bg-slate-900/70 border border-slate-800/80">
              <div className="text-[11px] text-slate-400 flex items-center gap-1">
                <Droplets className="w-3 h-3 text-cyan-400" /> Dewpoint / Wet-Bulb
              </div>
              <div className="text-sm font-semibold text-white mt-0.5">
                {formatTemp(dewpointC, units)} /{' '}
                <span className="text-cyan-300">{formatTemp(wetBulbC, units)}</span>
              </div>
            </div>
            <div className="p-3 rounded-xl bg-slate-900/70 border border-slate-800/80">
              <div className="text-[11px] text-slate-400 flex items-center gap-1">
                <Wind className="w-3 h-3 text-sky-400" /> Wind & Gust
              </div>
              <div className="text-sm font-semibold text-white mt-0.5">
                {degToCompass(windDir)} {formatWind(windMs, units)}
                {gustMs ? <span className="text-slate-400 text-xs"> (G {formatWind(gustMs, units)})</span> : null}
              </div>
            </div>
            <div className="p-3 rounded-xl bg-slate-900/70 border border-slate-800/80">
              <div className="text-[11px] text-slate-400 flex items-center gap-1">
                <CloudRain className="w-3 h-3 text-indigo-400" /> Next-Hr Calibrated PoP
              </div>
              <div className="text-sm font-semibold text-white mt-0.5">
                {curHour ? `${Math.round(curHour.precip_prob * 100)}%` : '—'}
                {curHour && curHour.ring_precip_max_mm >= 1.0 && (
                  <span className="text-[11px] text-amber-300 ml-1.5">
                    (8km: {formatPrecip(curHour.ring_precip_max_mm, units)})
                  </span>
                )}
              </div>
            </div>
            <div className="p-3 rounded-xl bg-slate-900/70 border border-slate-800/80">
              <div className="text-[11px] text-slate-400 flex items-center gap-1">
                <Gauge className="w-3 h-3 text-emerald-400" /> Pressure / θe
              </div>
              <div className="text-sm font-semibold text-white mt-0.5">
                {curHour ? `${Math.round(curHour.pressure_hpa)} hPa · ${Math.round(curHour.theta_e_k)}K` : '—'}
              </div>
            </div>
          </div>
        </div>

        {/* HRRR 15-Minute Sub-Hourly Precipitation Nowcast Strip */}
        {bundle.nowcast_15m.length > 0 && (() => {
          const first16 = bundle.nowcast_15m.slice(0, 16);
          const total15mPrecip = first16.reduce(
            (acc, p) => acc + (p.precip_mm || 0) + (p.ring_precip_max_mm || 0),
            0
          );
          return (
            <div className="mt-5 pt-4 border-t border-slate-800/80">
              <div className="flex items-center justify-between text-xs text-slate-400 mb-2">
                <div className="flex flex-wrap items-center gap-1.5 sm:gap-2">
                  <span className="font-medium text-slate-300">
                    HRRR 15-Minute Precipitation Nowcast
                  </span>
                  <span className="text-[10px] text-slate-500">
                    (Cyan = Exact Point · Purple = 8 km Vicinity)
                  </span>
                </div>
                <span className="text-[11px] font-mono shrink-0">
                  {total15mPrecip === 0 ? (
                    <span className="text-emerald-400/90 font-sans font-medium">
                      All Dry Next 4h
                    </span>
                  ) : (
                    <span>Next {Math.round((first16.length * 15) / 60)} hrs</span>
                  )}
                </span>
              </div>
              <div className="grid grid-cols-8 sm:grid-cols-12 md:grid-cols-16 gap-1.5">
                {first16.map((pt) => {
                  const dt = new Date(pt.valid_time_utc);
                  const label = dt.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
                  const ringH = Math.min(100, Math.round((pt.ring_precip_max_mm / 4.0) * 100));
                  const ptH = Math.min(100, Math.round((pt.precip_mm / 4.0) * 100));
                  const hasRain = pt.precip_mm > 0 || pt.ring_precip_max_mm > 0;
                  const tooltipText = hasRain
                    ? `${label}: Point ${formatPrecip(pt.precip_mm, units)} · 8 km Vicinity ${formatPrecip(pt.ring_precip_max_mm, units)}`
                    : `${label}: Dry (${formatPrecip(0, units)})`;

                  return (
                    <div
                      key={pt.valid_time_utc}
                      className="flex flex-col items-center gap-1 group cursor-help"
                      title={tooltipText}
                    >
                      <div className="w-full h-10 rounded bg-slate-900/90 border border-slate-800/70 group-hover:border-slate-700 relative overflow-hidden flex items-end transition">
                        {ringH > 0 && (
                          <div
                            className="w-full bg-indigo-500/35 absolute bottom-0"
                            style={{ height: `${Math.max(12, ringH)}%` }}
                          />
                        )}
                        {ptH > 0 && (
                          <div
                            className="w-full bg-cyan-400 relative z-10"
                            style={{ height: `${Math.max(12, ptH)}%` }}
                          />
                        )}
                        {!hasRain && (
                          <div className="w-full h-0.5 bg-slate-800/80 absolute bottom-0" />
                        )}
                      </div>
                      <span className="text-[9px] text-slate-500 group-hover:text-slate-300 truncate w-full text-center transition">
                        {label.replace(' ', '')}
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          );
        })()}

        {/* 1-Tap Backyard Ground-Truth Precip Check-In */}
        {loc.id && (
          <div className="mt-4 pt-3.5 border-t border-slate-800/80 flex flex-wrap items-center justify-between gap-2">
            <span className="text-xs text-slate-400">
              Backyard check-in (teaches local PoP & ice/rain boundary):
            </span>
            <div className="flex flex-wrap items-center gap-1.5">
              {checkinSent ? (
                <span className="text-xs text-emerald-300 flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-emerald-500/15 border border-emerald-500/30">
                  <CheckCircle2 className="w-3.5 h-3.5" /> Logged "{checkinSent}" to ground truth!
                </span>
              ) : (
                CHECKIN_OPTIONS.map((opt) => (
                  <button
                    key={opt.id}
                    type="button"
                    onClick={() => handleCheckin(opt.id)}
                    className="px-2.5 py-1 rounded-lg bg-slate-900 hover:bg-slate-800 border border-slate-800 text-xs text-slate-200 flex items-center gap-1.5 transition cursor-pointer"
                  >
                    {opt.icon}
                    {opt.label}
                  </button>
                ))
              )}
            </div>
          </div>
        )}
      </div>
    </section>
  );
};
