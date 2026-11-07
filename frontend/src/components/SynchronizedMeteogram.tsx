import React, { useEffect, useMemo, useRef, useState } from 'react';
import * as echarts from 'echarts';
import { Layers, SlidersHorizontal } from 'lucide-react';
import type { HourlyForecast, UnitSystem } from '../types';
import { convertPrecip, convertTemp, convertWind, degToCompass } from '../units';

interface SynchronizedMeteogramProps {
  hourly: HourlyForecast[];
  units: UnitSystem;
}

const RAW_MODEL_META: Array<{ id: string; label: string; color: string }> = [
  { id: 'hrrr', label: 'HRRR 3km', color: '#f59e0b' },
  { id: 'rrfs', label: 'RRFS v1', color: '#f43f5e' },
  { id: 'nbm', label: 'NOAA NBM', color: '#a855f7' },
  { id: 'ecmwf_ifs', label: 'ECMWF IFS', color: '#10b981' },
  { id: 'ecmwf_aifs', label: 'ECMWF AIFS', color: '#14b8a6' },
  { id: 'gfs', label: 'GFS', color: '#64748b' },
];

function buildPolygonBandSeries(
  name: string,
  lowArr: Array<number | null>,
  highArr: Array<number | null>,
  fillColor: string,
  z: number
): echarts.CustomSeriesOption {
  const data = lowArr.map((lo, idx) => [idx, lo ?? 0, highArr[idx] ?? 0]);
  return {
    type: 'custom',
    name,
    silent: true,
    z,
    tooltip: { show: false },
    renderItem: (params, api) => {
      if (params.dataIndex !== 0) return null;
      const count = data.length;
      if (count < 2) return null;
      const upperPts: number[][] = [];
      const lowerPts: number[][] = [];
      for (let i = 0; i < count; i++) {
        upperPts.push(api.coord([i, data[i][2]]));
        lowerPts.push(api.coord([i, data[i][1]]));
      }
      const points = upperPts.concat(lowerPts.reverse());
      return {
        type: 'polygon',
        shape: { points },
        style: {
          fill: fillColor,
        },
      };
    },
    data,
  };
}

function formatCompactAxisLabel(dt: Date, horizonHours: 48 | 240): string {
  const wd = dt.toLocaleDateString([], { weekday: 'short' });
  if (horizonHours === 240) {
    return `${wd} ${dt.getMonth() + 1}/${dt.getDate()}`;
  }
  const h24 = dt.getHours();
  const suffix = h24 >= 12 ? 'p' : 'a';
  const h12 = h24 % 12 || 12;
  return `${wd} ${h12}${suffix}`;
}

export const SynchronizedMeteogram: React.FC<SynchronizedMeteogramProps> = ({
  hourly,
  units,
}) => {
  const [horizonHours, setHorizonHours] = useState<48 | 240>(48);
  const [showSpaghetti, setShowSpaghetti] = useState<boolean>(true);

  const tempRef = useRef<HTMLDivElement | null>(null);
  const precipRef = useRef<HTMLDivElement | null>(null);
  const windRef = useRef<HTMLDivElement | null>(null);

  const slice = useMemo(() => hourly.slice(0, horizonHours), [hourly, horizonHours]);

  useEffect(() => {
    if (!tempRef.current || !precipRef.current || !windRef.current || slice.length === 0) {
      return;
    }

    const cTemp = echarts.init(tempRef.current);
    const cPrecip = echarts.init(precipRef.current);
    const cWind = echarts.init(windRef.current);

    cTemp.group = 'mesoscale-sync';
    cPrecip.group = 'mesoscale-sync';
    cWind.group = 'mesoscale-sync';
    echarts.connect('mesoscale-sync');

    const desktopLabels = slice.map((h) => {
      const dt = new Date(h.valid_time_utc);
      return dt.toLocaleString([], {
        weekday: 'short',
        hour: 'numeric',
      });
    });

    const compactLabels = slice.map((h) =>
      formatCompactAxisLabel(new Date(h.valid_time_utc), horizonHours)
    );

    const tooltipLabels = slice.map((h) => {
      const dt = new Date(h.valid_time_utc);
      return dt.toLocaleString([], {
        weekday: 'short',
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
      });
    });

    const tempUnit = units === 'imperial' ? '°F' : '°C';
    const windUnit = units === 'imperial' ? 'mph' : 'm/s';
    const precipUnit = units === 'imperial' ? 'in' : 'mm';
    const freezeLine = units === 'imperial' ? 32 : 0;

    const p10 = slice.map((h) => Number(convertTemp(h.temp_p10_c, units)?.toFixed(1)));
    const p25 = slice.map((h) => Number(convertTemp(h.temp_p25_c, units)?.toFixed(1)));
    const p50 = slice.map((h) => Number(convertTemp(h.temp_p50_c, units)?.toFixed(1)));
    const p75 = slice.map((h) => Number(convertTemp(h.temp_p75_c, units)?.toFixed(1)));
    const p90 = slice.map((h) => Number(convertTemp(h.temp_p90_c, units)?.toFixed(1)));
    const wetBulb = slice.map((h) => Number(convertTemp(h.wet_bulb_c, units)?.toFixed(1)));

    // Frontal bust highlight intervals
    const bustAreas: Array<[{ xAxis: number }, { xAxis: number }]> = [];
    slice.forEach((h, i) => {
      if (h.bust_risk_level === 'high') {
        bustAreas.push([
          { xAxis: Math.max(0, i - 0.5) as unknown as number },
          { xAxis: Math.min(slice.length - 1, i + 0.5) as unknown as number },
        ]);
      }
    });

    const spaghettiSeries: echarts.SeriesOption[] = showSpaghetti
      ? RAW_MODEL_META.map((m) => ({
          name: m.label,
          type: 'line',
          showSymbol: false,
          smooth: true,
          lineStyle: { width: 1.25, opacity: 0.65, color: m.color },
          itemStyle: { color: m.color },
          data: slice.map((h) => {
            const raw = h.raw_models_temp_c?.[m.id];
            const conv = convertTemp(raw, units);
            return conv === null ? null : Number(conv.toFixed(1));
          }),
        }))
      : [];

    const allTempVals = [...p10, ...p90, ...wetBulb].filter(
      (v): v is number => typeof v === 'number' && !Number.isNaN(v)
    );
    const yMinTemp = Math.floor((Math.min(...allTempVals) - 4) / 5) * 5;
    const yMaxTemp = Math.ceil((Math.max(...allTempVals) + 4) / 5) * 5;

    // Panel 2 data: Calibrated PoP (%) + Expected Point QPF vs 8km Spatial Ring Max
    const popPct = slice.map((h) => Math.round(h.precip_prob * 100));
    const qpfPoint = slice.map((h) =>
      Number(convertPrecip(h.precip_p50_mm, units).toFixed(units === 'imperial' ? 2 : 1))
    );
    const qpfRing = slice.map((h) =>
      Number(convertPrecip(h.ring_precip_max_mm, units).toFixed(units === 'imperial' ? 2 : 1))
    );

    // Panel 3 data: Sustained Wind + P90 Gust Envelope + MSL Pressure
    const windSust = slice.map((h) => Math.round(convertWind(h.wind_p50_ms, units) ?? 0));
    const gustP90 = slice.map((h) => Math.round(convertWind(h.gust_p90_ms, units) ?? 0));
    const pressure = slice.map((h) => Math.round(h.pressure_hpa));
    const minPres = Math.min(...pressure) - 3;
    const maxPres = Math.max(...pressure) + 3;

    const renderCharts = (isCompact: boolean) => {
      const axisLabels = isCompact ? compactLabels : desktopLabels;
      const tickInterval = isCompact
        ? horizonHours === 48
          ? 7
          : 47
        : horizonHours === 48
          ? 5
          : 23;

      const commonGrid = isCompact
        ? { left: 36, right: 30, top: 18, bottom: 24 }
        : { left: 52, right: 48, top: 26, bottom: 26 };

      const commonXAxis: echarts.XAXisComponentOption = {
        type: 'category',
        data: axisLabels,
        axisLine: { lineStyle: { color: '#1e293b' } },
        axisTick: { alignWithLabel: true, lineStyle: { color: '#334155' } },
        axisLabel: {
          color: '#94a3b8',
          fontSize: isCompact ? 9.5 : 10,
          interval: tickInterval,
          hideOverlap: true,
          margin: 8,
        },
        axisPointer: {
          show: true,
          lineStyle: { color: '#38bdf8', width: 1, type: 'dashed' },
        },
      };

      const baseTooltip = {
        trigger: 'axis' as const,
        confine: true,
        backgroundColor: 'rgba(9, 14, 26, 0.95)',
        borderColor: '#1e293b',
        textStyle: { color: '#e2e8f0', fontSize: 11 },
        extraCssText: 'max-width: min(295px, 86vw); white-space: normal;',
      };

      cTemp.setOption(
        {
          backgroundColor: 'transparent',
          animationDuration: 350,
          grid: commonGrid,
          tooltip: {
            ...baseTooltip,
            formatter: (params: unknown) => {
              const items = Array.isArray(params) ? params : [];
              const idx = items[0]?.dataIndex ?? 0;
              const hr = slice[idx];
              if (!hr) return '';
              const lines = [
                `<div style="font-weight:700;margin-bottom:4px;color:#fff">${tooltipLabels[idx]}</div>`,
                `<div><span style="color:#38bdf8;font-weight:700">Mesoscale P50:</span> <b>${p50[idx]}${tempUnit}</b> <span style="color:#94a3b8">(80% ACI: ${p10[idx]}–${p90[idx]}${tempUnit})</span></div>`,
                `<div><span style="color:#67e8f9">Wet-Bulb:</span> ${wetBulb[idx]}${tempUnit} · Phase: ${hr.precip_type}</div>`,
              ];
              if (showSpaghetti) {
                const modelBits = RAW_MODEL_META.map((m) => {
                  const v = convertTemp(hr.raw_models_temp_c?.[m.id], units);
                  return v !== null
                    ? `<span style="color:${m.color}">${m.label}: ${v.toFixed(0)}°</span>`
                    : null;
                }).filter(Boolean);
                if (modelBits.length > 0) {
                  lines.push(
                    `<div style="margin-top:3px;font-size:10px;line-height:1.4">${modelBits.join(' · ')}</div>`
                  );
                }
              }
              if (hr.bust_summary) {
                lines.push(
                  `<div style="margin-top:4px;color:#fbbf24;font-size:10px">⚠ ${hr.bust_summary}</div>`
                );
              }
              return lines.join('');
            },
          },
          xAxis: commonXAxis,
          yAxis: {
            type: 'value',
            min: yMinTemp,
            max: yMaxTemp,
            axisLabel: {
              color: '#94a3b8',
              fontSize: isCompact ? 9.5 : 10,
              formatter: isCompact ? '{value}°' : `{value}${tempUnit}`,
            },
            splitLine: { lineStyle: { color: 'rgba(51,65,85,0.35)' } },
          },
          series: [
            buildPolygonBandSeries(
              '80% Confidence (P10-P90)',
              p10,
              p90,
              'rgba(56, 189, 248, 0.13)',
              1
            ),
            buildPolygonBandSeries(
              '50% IQR (P25-P75)',
              p25,
              p75,
              'rgba(56, 189, 248, 0.24)',
              2
            ),
            ...spaghettiSeries,
            {
              name: 'Wet-Bulb',
              type: 'line',
              showSymbol: false,
              smooth: true,
              z: 8,
              lineStyle: { width: 1.5, type: 'dashed', color: '#67e8f9' },
              data: wetBulb,
            },
            {
              name: 'Mesoscale P50',
              type: 'line',
              showSymbol: false,
              smooth: true,
              z: 10,
              lineStyle: { width: 3, color: '#38bdf8' },
              itemStyle: { color: '#38bdf8' },
              data: p50,
              markLine: {
                silent: true,
                symbol: 'none',
                lineStyle: { color: 'rgba(244, 114, 182, 0.55)', type: 'dotted', width: 1.2 },
                label: {
                  formatter: isCompact ? `Freeze ${freezeLine}°` : `Freeze ${freezeLine}${tempUnit}`,
                  color: '#f472b6',
                  fontSize: 9,
                  position: 'insideEndTop',
                },
                data: [{ yAxis: freezeLine }],
              },
              markArea:
                bustAreas.length > 0
                  ? {
                      silent: true,
                      itemStyle: { color: 'rgba(245, 158, 11, 0.10)' },
                      data: bustAreas,
                    }
                  : undefined,
            },
          ],
        },
        true
      );

      cPrecip.setOption(
        {
          backgroundColor: 'transparent',
          animationDuration: 350,
          grid: commonGrid,
          tooltip: {
            ...baseTooltip,
            formatter: (params: unknown) => {
              const items = Array.isArray(params) ? params : [];
              const idx = items[0]?.dataIndex ?? 0;
              return [
                `<div style="font-weight:700;color:#fff">${tooltipLabels[idx]}</div>`,
                `<div>Calibrated PoP: <b style="color:#818cf8">${popPct[idx]}%</b></div>`,
                `<div>Point QPF: <b style="color:#38bdf8">${qpfPoint[idx]} ${precipUnit}</b></div>`,
                `<div>8 km Ring Max: <b style="color:#c084fc">${qpfRing[idx]} ${precipUnit}</b></div>`,
              ].join('');
            },
          },
          xAxis: commonXAxis,
          yAxis: [
            {
              type: 'value',
              min: 0,
              max: 100,
              axisLabel: {
                color: '#94a3b8',
                fontSize: isCompact ? 9.5 : 10,
                formatter: '{value}%',
              },
              splitLine: { lineStyle: { color: 'rgba(51,65,85,0.35)' } },
            },
            {
              type: 'value',
              min: 0,
              axisLabel: {
                color: '#64748b',
                fontSize: isCompact ? 9 : 10,
                formatter: `{value}`,
              },
              splitLine: { show: false },
            },
          ],
          series: [
            {
              name: '8 km Ring Max',
              type: 'bar',
              yAxisIndex: 1,
              barGap: '-100%',
              itemStyle: { color: 'rgba(168, 85, 247, 0.28)', borderRadius: [2, 2, 0, 0] },
              data: qpfRing,
            },
            {
              name: 'Point QPF',
              type: 'bar',
              yAxisIndex: 1,
              itemStyle: { color: '#38bdf8', borderRadius: [2, 2, 0, 0] },
              data: qpfPoint,
            },
            {
              name: 'Calibrated PoP',
              type: 'line',
              yAxisIndex: 0,
              smooth: true,
              showSymbol: false,
              lineStyle: { width: 2.2, color: '#818cf8' },
              areaStyle: { color: 'rgba(129, 140, 248, 0.14)' },
              data: popPct,
            },
          ],
        },
        true
      );

      cWind.setOption(
        {
          backgroundColor: 'transparent',
          animationDuration: 350,
          grid: commonGrid,
          tooltip: {
            ...baseTooltip,
            formatter: (params: unknown) => {
              const items = Array.isArray(params) ? params : [];
              const idx = items[0]?.dataIndex ?? 0;
              const hr = slice[idx];
              const comp = degToCompass(hr?.wind_dir_deg);
              return [
                `<div style="font-weight:700;color:#fff">${tooltipLabels[idx]}</div>`,
                `<div>Sustained Wind: <b>${comp} ${windSust[idx]} ${windUnit}</b></div>`,
                `<div>P90 Peak Gust: <b style="color:#fbbf24">${gustP90[idx]} ${windUnit}</b></div>`,
                `<div>MSL Pressure: <span style="color:#34d399">${pressure[idx]} hPa</span></div>`,
              ].join('');
            },
          },
          xAxis: commonXAxis,
          yAxis: [
            {
              type: 'value',
              min: 0,
              axisLabel: {
                color: '#94a3b8',
                fontSize: isCompact ? 9.5 : 10,
                formatter: isCompact ? '{value}' : `{value} ${windUnit}`,
              },
              splitLine: { lineStyle: { color: 'rgba(51,65,85,0.35)' } },
            },
            {
              type: 'value',
              min: minPres,
              max: maxPres,
              axisLabel: { color: '#64748b', fontSize: 9, formatter: '{value}' },
              splitLine: { show: false },
            },
          ],
          series: [
            {
              name: 'P90 Gust Envelope',
              type: 'line',
              smooth: true,
              showSymbol: false,
              lineStyle: { width: 1.5, type: 'dashed', color: '#fbbf24' },
              areaStyle: { color: 'rgba(251, 191, 36, 0.08)' },
              data: gustP90,
            },
            {
              name: 'Sustained Wind',
              type: 'line',
              smooth: true,
              showSymbol: false,
              lineStyle: { width: 2.2, color: '#38bdf8' },
              data: windSust,
            },
            {
              name: 'Pressure (hPa)',
              type: 'line',
              yAxisIndex: 1,
              smooth: true,
              showSymbol: false,
              lineStyle: { width: 1.3, color: '#34d399', opacity: 0.7 },
              data: pressure,
            },
          ],
        },
        true
      );
    };

    let currentCompact = tempRef.current.clientWidth < 520;
    renderCharts(currentCompact);

    const ro = new ResizeObserver(() => {
      const nextCompact = (tempRef.current?.clientWidth ?? 800) < 520;
      if (nextCompact !== currentCompact) {
        currentCompact = nextCompact;
        renderCharts(currentCompact);
      }
      cTemp.resize();
      cPrecip.resize();
      cWind.resize();
    });
    ro.observe(tempRef.current);

    return () => {
      ro.disconnect();
      cTemp.dispose();
      cPrecip.dispose();
      cWind.dispose();
    };
  }, [slice, units, horizonHours, showSpaghetti]);

  const windUnitLabel = units === 'imperial' ? 'mph' : 'm/s';

  return (
    <section className="glass-panel rounded-2xl p-3.5 sm:p-6 space-y-3.5 sm:space-y-4">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div>
          <h3 className="text-sm sm:text-base font-bold text-white flex items-center gap-2">
            <SlidersHorizontal className="w-4 h-4 text-cyan-400 shrink-0" />
            Synchronized Probabilistic Meteogram
          </h3>
          <p className="text-[11px] sm:text-xs text-slate-400 mt-0.5">
            Shaded 50% / 80% Adaptive Conformal bands + Wet-Bulb + NWP model spaghetti (tap/scrub linked across panels).
          </p>
        </div>

        <div className="flex flex-wrap items-center justify-between sm:justify-end gap-2">
          <div className="inline-flex rounded-xl bg-slate-900 border border-slate-800 p-0.5 text-xs">
            <button
              type="button"
              onClick={() => setHorizonHours(48)}
              className={`px-2.5 sm:px-3 py-1 rounded-lg font-medium transition cursor-pointer ${
                horizonHours === 48 ? 'bg-cyan-500 text-slate-950 font-semibold' : 'text-slate-400 hover:text-white'
              }`}
            >
              48h Mesoscale
            </button>
            <button
              type="button"
              onClick={() => setHorizonHours(240)}
              className={`px-2.5 sm:px-3 py-1 rounded-lg font-medium transition cursor-pointer ${
                horizonHours === 240 ? 'bg-cyan-500 text-slate-950 font-semibold' : 'text-slate-400 hover:text-white'
              }`}
            >
              10-Day Full
            </button>
          </div>

          <button
            type="button"
            onClick={() => setShowSpaghetti((v) => !v)}
            className={`px-2.5 sm:px-3 py-1.5 rounded-xl border text-xs font-medium flex items-center gap-1.5 transition cursor-pointer ${
              showSpaghetti
                ? 'bg-indigo-500/20 border-indigo-400/40 text-indigo-200'
                : 'bg-slate-900 border-slate-800 text-slate-400'
            }`}
          >
            <Layers className="w-3.5 h-3.5 shrink-0" />
            <span>6-Model Spaghetti</span>
          </button>
        </div>
      </div>

      {showSpaghetti && (
        <div className="flex flex-wrap items-center gap-x-3 sm:gap-x-4 gap-y-1 text-[10px] sm:text-[11px] text-slate-400 px-0.5">
          <span className="flex items-center gap-1.5 text-cyan-300 font-semibold">
            <span className="w-3 h-1 rounded bg-cyan-400 inline-block" /> Mesoscale P50
          </span>
          {RAW_MODEL_META.map((m) => (
            <span key={m.id} className="flex items-center gap-1">
              <span className="w-2.5 h-0.5 inline-block" style={{ backgroundColor: m.color }} />
              {m.label}
            </span>
          ))}
        </div>
      )}

      <div className="space-y-2">
        <div className="text-[10px] sm:text-[11px] font-semibold uppercase tracking-wider text-slate-400 px-0.5">
          Temperature · Wet-Bulb · 80% Conformal Fan & Frontal Bust Risk
        </div>
        <div ref={tempRef} className="w-full h-56 sm:h-64" />

        <div className="text-[10px] sm:text-[11px] font-semibold uppercase tracking-wider text-slate-400 px-0.5 pt-2 border-t border-slate-800/60">
          Calibrated PoP (%) & Point QPF vs. 8 km Convective Ring Max
        </div>
        <div ref={precipRef} className="w-full h-40 sm:h-44" />

        <div className="text-[10px] sm:text-[11px] font-semibold uppercase tracking-wider text-slate-400 px-0.5 pt-2 border-t border-slate-800/60">
          Sustained Wind ({windUnitLabel}) · P90 Gust Envelope & Pressure (hPa)
        </div>
        <div ref={windRef} className="w-full h-40 sm:h-44" />
      </div>
    </section>
  );
};
