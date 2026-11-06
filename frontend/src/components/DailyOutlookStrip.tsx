import React from 'react';
import {
  AlertTriangle,
  Calendar,
  Cloud,
  CloudRain,
  CloudSnow,
  Droplets,
  Snowflake,
  Sun,
} from 'lucide-react';
import type { DailyForecast, UnitSystem } from '../types';
import { formatPrecip, formatTemp, formatWind } from '../units';

interface DailyOutlookStripProps {
  daily: DailyForecast[];
  units: UnitSystem;
}

function dayIcon(d: DailyForecast) {
  if (d.dominant_precip_type === 'freezing_rain' && d.precip_prob_max >= 0.2) {
    return <Droplets className="w-4 h-4 text-fuchsia-400" />;
  }
  if (d.dominant_precip_type === 'sleet' && d.precip_prob_max >= 0.2) {
    return <Snowflake className="w-4 h-4 text-indigo-300" />;
  }
  if (d.dominant_precip_type === 'snow' && d.precip_prob_max >= 0.2) {
    return <CloudSnow className="w-4 h-4 text-cyan-200" />;
  }
  if (d.precip_prob_max >= 0.35) {
    return <CloudRain className="w-4 h-4 text-sky-400" />;
  }
  if (d.cloud_mean_pct >= 60) {
    return <Cloud className="w-4 h-4 text-slate-400" />;
  }
  return <Sun className="w-4 h-4 text-amber-300" />;
}

export const DailyOutlookStrip: React.FC<DailyOutlookStripProps> = ({ daily, units }) => {
  if (daily.length === 0) return null;

  const globalMin = Math.min(...daily.map((d) => d.temp_min_p10_c));
  const globalMax = Math.max(...daily.map((d) => d.temp_max_p90_c));
  const span = Math.max(1, globalMax - globalMin);

  return (
    <section className="glass-panel rounded-2xl p-4 sm:p-6 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-base font-bold text-white flex items-center gap-2">
          <Calendar className="w-4 h-4 text-cyan-400" />
          10-Day Probabilistic Daily Outlook
        </h3>
        <span className="text-[11px] text-slate-400">
          Translucent bar = P10–P90 tail uncertainty · Solid bar = Expected Min–Max
        </span>
      </div>

      <div className="divide-y divide-slate-800/70">
        {daily.map((d, idx) => {
          const [yr, mo, dy] = d.date.split('-').map(Number);
          const localDate = new Date(yr, mo - 1, dy);
          const dayLabel =
            idx === 0
              ? 'Today'
              : localDate.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });

          const outerLeft = ((d.temp_min_p10_c - globalMin) / span) * 100;
          const outerWidth = Math.max(4, ((d.temp_max_p90_c - d.temp_min_p10_c) / span) * 100);
          const innerLeft = ((d.temp_min_c - globalMin) / span) * 100;
          const innerWidth = Math.max(4, ((d.temp_max_c - d.temp_min_c) / span) * 100);

          return (
            <div
              key={d.date}
              className="py-2.5 flex flex-col sm:flex-row sm:items-center justify-between gap-2 text-xs"
            >
              <div className="flex items-center justify-between sm:w-48 shrink-0">
                <div className="flex items-center gap-2.5">
                  {dayIcon(d)}
                  <span className="font-semibold text-white">{dayLabel}</span>
                  {d.has_high_bust_risk && (
                    <span
                      title="High NWP model spread / frontal timing uncertainty"
                      className="text-amber-400"
                    >
                      <AlertTriangle className="w-3.5 h-3.5" />
                    </span>
                  )}
                </div>
                <span className="text-[11px] font-mono text-sky-300 w-12 text-right">
                  {d.precip_prob_max >= 0.05 ? `${Math.round(d.precip_prob_max * 100)}%` : ''}
                </span>
              </div>

              {/* Probabilistic Range Bar */}
              <div className="flex items-center gap-3 flex-1 max-w-md">
                <span className="w-10 text-right font-mono text-slate-400">
                  {formatTemp(d.temp_min_c, units)}
                </span>
                <div className="flex-1 h-2.5 rounded-full bg-slate-900 relative overflow-hidden">
                  <div
                    className="h-full rounded-full bg-cyan-400/20 absolute top-0"
                    style={{ left: `${outerLeft}%`, width: `${outerWidth}%` }}
                    title={`P10–P90 tail range: ${formatTemp(d.temp_min_p10_c, units)} to ${formatTemp(d.temp_max_p90_c, units)}`}
                  />
                  <div
                    className="h-full rounded-full bg-gradient-to-r from-cyan-400 to-amber-400 absolute top-0"
                    style={{ left: `${innerLeft}%`, width: `${innerWidth}%` }}
                  />
                </div>
                <span className="w-10 font-mono font-bold text-white">
                  {formatTemp(d.temp_max_c, units)}
                </span>
              </div>

              <div className="flex items-center justify-end gap-4 sm:w-44 text-[11px] text-slate-400 font-mono shrink-0">
                <span>
                  {d.precip_total_mm >= 0.25 ? formatPrecip(d.precip_total_mm, units) : '—'}
                </span>
                <span>Gust {formatWind(d.max_gust_ms, units)}</span>
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
};
