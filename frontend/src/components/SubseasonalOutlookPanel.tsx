import React from 'react';
import {
  CalendarRange,
  Droplets,
  Flame,
  Snowflake,
  Sparkles,
  Thermometer,
} from 'lucide-react';
import type { SubseasonalSummary, SubseasonalWeekCard, UnitSystem } from '../types';
import { convertDeltaTemp, formatPrecip, formatTemp } from '../units';

interface SubseasonalOutlookPanelProps {
  subseasonal?: SubseasonalSummary;
  units: UnitSystem;
}

function formatShortRange(startIso: string, endIso: string): string {
  const parseLocal = (iso: string) => {
    const [yr, mo, dy] = iso.split('-').map(Number);
    if (!yr || !mo || !dy) return null;
    return new Date(yr, mo - 1, dy);
  };
  const s = parseLocal(startIso);
  const e = parseLocal(endIso);
  if (!s || !e) return `${startIso} – ${endIso}`;
  const fmt = (d: Date) => d.toLocaleDateString([], { month: 'short', day: 'numeric' });
  return `${fmt(s)} – ${fmt(e)}`;
}

function tempSignalBadge(w: SubseasonalWeekCard, units: UnitSystem) {
  const delta = convertDeltaTemp(w.temp_anomaly_c, units);
  const sign = delta > 0 ? '+' : '';
  const unitSym = units === 'imperial' ? 'F' : 'C';

  if (w.temp_signal === 'warmer') {
    return (
      <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[11px] font-bold bg-amber-500/20 border border-amber-400/45 text-amber-200">
        <Flame className="w-3 h-3 text-amber-400 shrink-0" />
        Warmer ({sign}
        {delta.toFixed(1)}°{unitSym} · {w.warm_prob_pct}% prob)
      </span>
    );
  }
  if (w.temp_signal === 'colder') {
    return (
      <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[11px] font-bold bg-cyan-500/20 border border-cyan-400/45 text-cyan-200">
        <Snowflake className="w-3 h-3 text-cyan-300 shrink-0" />
        Colder ({sign}
        {delta.toFixed(1)}°{unitSym} · {w.cold_prob_pct}% prob)
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-slate-800/90 border border-slate-700 text-slate-200">
      <Thermometer className="w-3 h-3 text-emerald-400 shrink-0" />
      Near Normal ({sign}
      {delta.toFixed(1)}°{unitSym})
    </span>
  );
}

export const SubseasonalOutlookPanel: React.FC<SubseasonalOutlookPanelProps> = ({
  subseasonal,
  units,
}) => {
  if (!subseasonal?.available || !subseasonal.weeks || subseasonal.weeks.length === 0) {
    return null;
  }

  const memberCount = subseasonal.ensemble_members || 82;

  return (
    <section className="glass-panel rounded-2xl p-4 sm:p-6 space-y-4">
      <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-2">
        <div className="space-y-0.5">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm sm:text-base font-bold text-white flex items-center gap-2">
              <CalendarRange className="w-4 h-4 text-cyan-400 shrink-0" />
              Weeks 2–4 Subseasonal Regime Outlook (Days 8–30)
            </h3>
            <span className="px-2.5 py-0.5 rounded-full text-[10px] font-semibold bg-indigo-500/15 border border-indigo-400/35 text-indigo-200 inline-flex items-center gap-1">
              <Sparkles className="w-3 h-3 text-indigo-300 shrink-0" />
              {memberCount}-Member GEFS + ECMWF SEAS5 · EMOS Calibrated
            </span>
          </div>
          <p className="text-[11px] sm:text-xs text-slate-400">
            Beyond Day 10, atmosphere chaos limits exact daily timing — weekly ensemble regimes calibrated against 8-year local ERA5 normals &amp; soil moisture extract real pattern skill.
          </p>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-3.5 sm:gap-4">
        {subseasonal.weeks.map((w) => {
          const dateRange = formatShortRange(w.start_date, w.end_date);
          const precipBadgeClass =
            w.precip_signal === 'wetter'
              ? 'bg-sky-500/20 border-sky-400/45 text-sky-200'
              : w.precip_signal === 'drier'
                ? 'bg-amber-500/15 border-amber-400/35 text-amber-200'
                : 'bg-slate-800/80 border-slate-700 text-slate-300';
          const precipTitle =
            w.precip_signal === 'wetter'
              ? `Wetter Lean (${w.wet_prob_pct}% prob)`
              : w.precip_signal === 'drier'
                ? `Drier Lean (${100 - w.wet_prob_pct}% dry)`
                : 'Near-Normal Rain';

          return (
            <div
              key={w.week_index}
              className="p-4 rounded-xl bg-slate-950/65 border border-slate-800/85 flex flex-col justify-between gap-3"
            >
              <div className="space-y-3">
                {/* Card Header */}
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <div className="flex items-center gap-1.5">
                      <span className="text-sm font-bold text-white">{w.title}</span>
                      <span className="text-[11px] text-cyan-300 font-medium">
                        ({w.days_label})
                      </span>
                    </div>
                    <div className="text-[11px] text-slate-400">{dateRange}</div>
                  </div>
                  <span className="px-2 py-0.5 rounded-md text-[10px] font-medium bg-slate-900 border border-slate-800 text-slate-400 shrink-0">
                    {w.skill_tier}
                  </span>
                </div>

                {/* Temperature Anomaly Pill */}
                <div>{tempSignalBadge(w, units)}</div>

                {/* Calibrated Weekly High / Low vs 8-Yr Local Normal */}
                <div className="grid grid-cols-2 gap-2">
                  <div className="p-2.5 rounded-lg bg-slate-900/80 border border-slate-800/80">
                    <div className="text-[10px] text-slate-400">Calibrated Avg High</div>
                    <div className="text-base font-extrabold text-white font-mono mt-0.5">
                      {formatTemp(w.high_p50_c, units)}
                    </div>
                    <div className="text-[10px] text-slate-400 font-mono">
                      80%: {formatTemp(w.high_p10_c, units)}–{formatTemp(w.high_p90_c, units)}
                    </div>
                    <div className="text-[10px] text-slate-500">
                      Norm {formatTemp(w.normal_high_c, units)}
                    </div>
                  </div>

                  <div className="p-2.5 rounded-lg bg-slate-900/80 border border-slate-800/80">
                    <div className="text-[10px] text-slate-400">Calibrated Avg Low</div>
                    <div className="text-base font-extrabold text-cyan-200 font-mono mt-0.5">
                      {formatTemp(w.low_p50_c, units)}
                    </div>
                    <div className="text-[10px] text-slate-400 font-mono">
                      80%: {formatTemp(w.low_p10_c, units)}–{formatTemp(w.low_p90_c, units)}
                    </div>
                    <div className="text-[10px] text-slate-500">
                      Norm {formatTemp(w.normal_low_c, units)}
                    </div>
                  </div>
                </div>

                {/* Weekly Precipitation & Freeze Probability */}
                <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
                  <span
                    className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-lg border font-medium ${precipBadgeClass}`}
                  >
                    <Droplets className="w-3 h-3 shrink-0" />
                    {precipTitle}: {formatPrecip(w.precip_p50_mm, units)} (norm{' '}
                    {formatPrecip(w.normal_precip_mm, units)})
                  </span>
                  {w.freeze_prob_pct > 0 && (
                    <span
                      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-lg border font-medium ${
                        w.freeze_prob_pct >= 25
                          ? 'bg-indigo-500/20 border-indigo-400/45 text-indigo-200'
                          : 'bg-slate-900 border-slate-800 text-slate-400'
                      }`}
                    >
                      <Snowflake className="w-3 h-3 shrink-0" />
                      Freeze Risk {w.freeze_prob_pct}%
                    </span>
                  )}
                </div>

                {/* Official NOAA CPC 6-10 / 8-14 Day Cross-Check Badges (US) */}
                {(w.cpc_temp_badge || w.cpc_precip_badge) && (
                  <div className="flex flex-wrap items-center gap-1.5 text-[10px]">
                    {w.cpc_temp_badge && (
                      <span className="px-2 py-0.5 rounded bg-cyan-950/60 border border-cyan-500/30 text-cyan-200">
                        NOAA CPC Temp: {w.cpc_temp_badge}
                      </span>
                    )}
                    {w.cpc_precip_badge && (
                      <span className="px-2 py-0.5 rounded bg-sky-950/60 border border-sky-500/30 text-sky-200">
                        NOAA CPC Precip: {w.cpc_precip_badge}
                      </span>
                    )}
                  </div>
                )}
              </div>

              <p className="text-[11px] text-slate-400 leading-relaxed pt-2 border-t border-slate-800/70">
                {w.summary}
              </p>
            </div>
          );
        })}
      </div>
    </section>
  );
};
