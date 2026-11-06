import React, { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import * as echarts from 'echarts';
import { Award, Cpu, Loader2, RefreshCw, Sparkles, TrendingDown } from 'lucide-react';
import { api } from '../api';
import type { UnitSystem } from '../types';
import { convertTemp } from '../units';

interface VerificationScoreboardProps {
  locationId: number | null;
  locationName: string;
  units: UnitSystem;
  onRetrained: () => void;
}

const LEAD_BUCKETS = [
  { id: 'all', label: 'All Leads' },
  { id: '1_6h', label: '1–6h Nowcast' },
  { id: '7_24h', label: '7–24h Day 1' },
  { id: '25_48h', label: '25–48h Day 2' },
  { id: '49_168h', label: '3–7 Days' },
];

export const VerificationScoreboard: React.FC<VerificationScoreboardProps> = ({
  locationId,
  locationName,
  units,
  onRetrained,
}) => {
  const [leadBucket, setLeadBucket] = useState('all');
  const [days, setDays] = useState(30);
  const [retraining, setRetraining] = useState(false);
  const replayRef = useRef<HTMLDivElement | null>(null);

  const verQuery = useQuery({
    queryKey: ['verification', locationId, days, leadBucket],
    queryFn: () => api.getVerification(locationId!, days, leadBucket),
    enabled: locationId !== null,
  });

  const handleRetrain = async () => {
    if (!locationId) return;
    setRetraining(true);
    try {
      await api.retrainLocation(locationId);
      await verQuery.refetch();
      onRetrained();
    } finally {
      setRetraining(false);
    }
  };

  useEffect(() => {
    const rows = verQuery.data?.replay_72h ?? [];
    if (!replayRef.current || rows.length === 0) return;

    const chart = echarts.init(replayRef.current);
    const tempUnit = units === 'imperial' ? '°F' : '°C';

    const labels = rows.map((r) => {
      const dt = new Date(String(r.valid_time_utc));
      return dt.toLocaleString([], { weekday: 'short', hour: 'numeric' });
    });

    const conv = (v: unknown) => {
      if (v === null || v === undefined) return null;
      const c = convertTemp(Number(v), units);
      return c === null ? null : Number(c.toFixed(1));
    };

    chart.setOption({
      backgroundColor: 'transparent',
      grid: { left: 48, right: 24, top: 32, bottom: 28 },
      legend: {
        top: 0,
        textStyle: { color: '#cbd5e1', fontSize: 11 },
      },
      tooltip: {
        trigger: 'axis',
        backgroundColor: 'rgba(9, 14, 26, 0.95)',
        borderColor: '#1e293b',
        textStyle: { color: '#e2e8f0', fontSize: 11 },
      },
      xAxis: {
        type: 'category',
        data: labels,
        axisLine: { lineStyle: { color: '#1e293b' } },
        axisLabel: { color: '#94a3b8', fontSize: 10, interval: 7 },
      },
      yAxis: {
        type: 'value',
        scale: true,
        axisLabel: { color: '#94a3b8', fontSize: 10, formatter: `{value}${tempUnit}` },
        splitLine: { lineStyle: { color: 'rgba(51,65,85,0.35)' } },
      },
      series: [
        {
          name: 'Observed Ground Truth',
          type: 'line',
          smooth: true,
          symbolSize: 4,
          lineStyle: { width: 2.8, color: '#f8fafc' },
          itemStyle: { color: '#f8fafc' },
          data: rows.map((r) => conv(r.obs_temp_c)),
        },
        {
          name: 'Mesoscale AI',
          type: 'line',
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 2.4, color: '#38bdf8' },
          itemStyle: { color: '#38bdf8' },
          data: rows.map((r) => conv(r.mesoscale_ai_temp_c)),
        },
        {
          name: 'NOAA NBM',
          type: 'line',
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 1.5, type: 'dashed', color: '#a855f7' },
          itemStyle: { color: '#a855f7' },
          data: rows.map((r) => conv(r.nbm_temp_c)),
        },
        {
          name: 'HRRR 3km',
          type: 'line',
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 1.5, type: 'dotted', color: '#f59e0b' },
          itemStyle: { color: '#f59e0b' },
          data: rows.map((r) => conv(r.hrrr_temp_c)),
        },
      ],
    });

    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(replayRef.current);
    return () => {
      ro.disconnect();
      chart.dispose();
    };
  }, [verQuery.data, units]);

  if (locationId === null) {
    return (
      <section className="glass-panel rounded-2xl p-8 text-center space-y-3">
        <Award className="w-8 h-8 text-cyan-400 mx-auto" />
        <h3 className="text-lg font-bold text-white">Pin {locationName} to Unlock Local Verification</h3>
        <p className="text-xs text-slate-400 max-w-lg mx-auto leading-relaxed">
          Instant Lookup mode streams live 6-model consensus without storing database rows. Pin this location to backfill 365 days of historical runs, train local CatBoost + LightGBM residual models, and audit accuracy against NOAA NBM and HRRR.
        </p>
      </section>
    );
  }

  const report = verQuery.data;
  const leaderboard = report?.leaderboard ?? [];
  const topImportances = report?.feature_importances ?? [];

  return (
    <section className="space-y-4">
      <div className="glass-panel rounded-2xl p-5 sm:p-6 space-y-5">
        <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-4">
          <div>
            <h3 className="text-base sm:text-lg font-bold text-white flex items-center gap-2">
              <Award className="w-4 h-4 text-cyan-400" />
              Out-of-Sample Accuracy Scoreboard — {locationName}
            </h3>
            <p className="text-xs text-slate-400">
              Strict point-in-time verification (ASOF non-leakage) against paired Oklahoma Mesonet / ASOS observations.
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <div className="inline-flex rounded-xl bg-slate-900 border border-slate-800 p-0.5 text-xs">
              {[14, 30, 90].map((d) => (
                <button
                  key={d}
                  type="button"
                  onClick={() => setDays(d)}
                  className={`px-2.5 py-1 rounded-lg font-medium cursor-pointer ${
                    days === d ? 'bg-slate-700 text-white' : 'text-slate-400 hover:text-white'
                  }`}
                >
                  {d}d
                </button>
              ))}
            </div>

            <button
              type="button"
              onClick={handleRetrain}
              disabled={retraining}
              className="px-3.5 py-1.5 rounded-xl bg-cyan-500 hover:bg-cyan-400 disabled:opacity-50 text-slate-950 font-semibold text-xs flex items-center gap-1.5 cursor-pointer"
            >
              {retraining ? (
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <RefreshCw className="w-3.5 h-3.5" />
              )}
              Retrain Model Now
            </button>
          </div>
        </div>

        {/* Lead-Time Bucket Filter */}
        <div className="flex flex-wrap items-center gap-1.5">
          {LEAD_BUCKETS.map((b) => (
            <button
              key={b.id}
              type="button"
              onClick={() => setLeadBucket(b.id)}
              className={`px-3 py-1 rounded-lg text-xs font-medium border transition cursor-pointer ${
                leadBucket === b.id
                  ? 'bg-cyan-500/20 border-cyan-400/40 text-cyan-200'
                  : 'bg-slate-900/70 border-slate-800 text-slate-400 hover:text-slate-200'
              }`}
            >
              {b.label}
            </button>
          ))}
        </div>

        {/* Leaderboard Table */}
        {verQuery.isLoading ? (
          <div className="py-12 flex items-center justify-center text-slate-400 text-xs gap-2">
            <Loader2 className="w-4 h-4 animate-spin text-cyan-400" /> Computing out-of-sample error metrics...
          </div>
        ) : leaderboard.length === 0 ? (
          <div className="p-6 rounded-xl bg-slate-900/60 border border-slate-800 text-xs text-slate-400 text-center">
            Historical backfill is still running or accumulating paired ground-truth hours. Refresh in a few moments!
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse text-xs">
              <thead>
                <tr className="border-b border-slate-800 text-slate-400 text-[11px] uppercase tracking-wider">
                  <th className="py-2.5 px-3">Rank & Model</th>
                  <th className="py-2.5 px-3">Temp MAE</th>
                  <th className="py-2.5 px-3">Temp RMSE</th>
                  <th className="py-2.5 px-3">Mean Bias</th>
                  <th className="py-2.5 px-3">Wind MAE</th>
                  <th className="py-2.5 px-3">Precip Brier</th>
                  <th className="py-2.5 px-3">Skill vs NBM</th>
                  <th className="py-2.5 px-3">80% ACI Cov</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/60">
                {leaderboard.map((row, idx) => {
                  const isMeso = row.model_id === 'mesoscale_ai';
                  const maeDisplay =
                    units === 'imperial' ? `${row.temp_mae_f.toFixed(2)}°F` : `${row.temp_mae_c.toFixed(2)}°C`;
                  const rmseDisplay =
                    units === 'imperial'
                      ? `${row.temp_rmse_f.toFixed(2)}°F`
                      : `${(row.temp_rmse_f / 1.8).toFixed(2)}°C`;
                  const biasVal = units === 'imperial' ? row.temp_bias_f : row.temp_bias_f / 1.8;
                  return (
                    <tr
                      key={row.model_id}
                      className={
                        isMeso
                          ? 'bg-cyan-500/10 text-white font-medium'
                          : 'hover:bg-slate-900/50 text-slate-300'
                      }
                    >
                      <td className="py-3 px-3 flex items-center gap-2">
                        <span
                          className={`w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-bold ${
                            idx === 0
                              ? 'bg-emerald-500/25 text-emerald-300 border border-emerald-400/40'
                              : 'bg-slate-800 text-slate-400'
                          }`}
                        >
                          {idx + 1}
                        </span>
                        <span className={isMeso ? 'text-cyan-300 font-bold' : ''}>{row.label}</span>
                        {isMeso && (
                          <span className="px-1.5 py-0.5 rounded bg-cyan-500/20 text-cyan-200 text-[10px]">
                            Stacked MOS
                          </span>
                        )}
                      </td>
                      <td className="py-3 px-3 font-mono font-semibold text-white">{maeDisplay}</td>
                      <td className="py-3 px-3 font-mono">{rmseDisplay}</td>
                      <td className="py-3 px-3 font-mono">
                        {biasVal > 0 ? '+' : ''}
                        {biasVal.toFixed(2)}°
                      </td>
                      <td className="py-3 px-3 font-mono">
                        {row.wind_mae_mph !== null ? `${row.wind_mae_mph.toFixed(1)} mph` : '—'}
                      </td>
                      <td className="py-3 px-3 font-mono">
                        {row.precip_brier !== null ? row.precip_brier.toFixed(4) : '—'}
                      </td>
                      <td className="py-3 px-3 font-mono">
                        {row.brier_skill_vs_nbm !== null ? (
                          <span
                            className={
                              row.brier_skill_vs_nbm >= 0 ? 'text-emerald-300' : 'text-rose-300'
                            }
                          >
                            {row.brier_skill_vs_nbm >= 0 ? '+' : ''}
                            {(row.brier_skill_vs_nbm * 100).toFixed(1)}%
                          </span>
                        ) : (
                          '—'
                        )}
                      </td>
                      <td className="py-3 px-3 font-mono">
                        {row.coverage_80pct !== null
                          ? `${row.coverage_80pct.toFixed(1)}%`
                          : '—'}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Bottom Grid: 72h Forecast-vs-Actual Replay + Feature Importances */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2 glass-panel rounded-2xl p-5 space-y-2">
          <div className="flex items-center justify-between">
            <h4 className="text-sm font-bold text-white flex items-center gap-1.5">
              <TrendingDown className="w-4 h-4 text-cyan-400" />
              Recent 72-Hour Forecast vs. Observed Ground-Truth Replay
            </h4>
            <span className="text-[11px] text-slate-400">Solid White = Actual Tower Obs</span>
          </div>
          {(report?.replay_72h.length ?? 0) > 0 ? (
            <div ref={replayRef} className="w-full h-64" />
          ) : (
            <div className="h-64 flex items-center justify-center text-xs text-slate-500">
              Replay series populates automatically as verified hours accumulate.
            </div>
          )}
        </div>

        <div className="glass-panel rounded-2xl p-5 space-y-3">
          <h4 className="text-sm font-bold text-white flex items-center gap-1.5">
            <Cpu className="w-4 h-4 text-indigo-400" />
            Learned Local Regime Drivers
          </h4>
          <p className="text-[11px] text-slate-400">
            Top predictors discovered by CatBoost + LightGBM for correcting NWP errors at {locationName}:
          </p>
          {topImportances.length === 0 ? (
            <div className="py-10 text-center text-xs text-slate-500 flex flex-col items-center gap-2">
              <Sparkles className="w-4 h-4 text-slate-600" />
              Feature importances appear once local MOS model training completes.
            </div>
          ) : (
            <div className="space-y-2">
              {topImportances.slice(0, 8).map((fi) => {
                const maxImp = topImportances[0]?.importance || 1;
                const pct = Math.max(6, Math.round((fi.importance / maxImp) * 100));
                return (
                  <div key={fi.feature} className="space-y-1">
                    <div className="flex justify-between text-[11px]">
                      <span className="text-slate-300 font-mono truncate">{fi.feature}</span>
                      <span className="text-slate-400 font-mono">
                        {(fi.importance * 100).toFixed(1)}%
                      </span>
                    </div>
                    <div className="h-1.5 rounded-full bg-slate-900 overflow-hidden">
                      <div
                        className="h-full bg-gradient-to-r from-cyan-400 to-indigo-400"
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </section>
  );
};
