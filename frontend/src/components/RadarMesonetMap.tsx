import React, { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import maplibregl from 'maplibre-gl';
import { Layers, Pause, Play, Radar, Radio, ShieldAlert } from 'lucide-react';
import { api } from '../api';
import type { UnitSystem } from '../types';
import { degToCompass } from '../units';

interface RadarMesonetMapProps {
  lat: number;
  lon: number;
  locationName: string;
  units: UnitSystem;
}

export const RadarMesonetMap: React.FC<RadarMesonetMapProps> = ({
  lat,
  lon,
  locationName,
  units,
}) => {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const markersRef = useRef<maplibregl.Marker[]>([]);

  const [playing, setPlaying] = useState(false);
  const [frameIdx, setFrameIdx] = useState(10); // 10 == Live (0m)
  const [radarProduct, setRadarProduct] = useState<'nexrad' | 'mrms'>('nexrad');
  const [spcLayer, setSpcLayer] = useState<'none' | 'day1' | 'day2'>('day1');
  const [showStations, setShowStations] = useState(true);
  const [mapReady, setMapReady] = useState(false);

  const overlayQuery = useQuery({
    queryKey: ['map-overlay', lat.toFixed(3), lon.toFixed(3)],
    queryFn: () => api.getMapOverlay(lat, lon),
    refetchInterval: 5 * 60 * 1000,
  });

  const frames = overlayQuery.data?.radar_frames ?? [];

  // Initialize MapLibre GL map
  useEffect(() => {
    if (!containerRef.current) return;

    const map = new maplibregl.Map({
      container: containerRef.current,
      style: 'https://tiles.openfreemap.org/styles/dark',
      center: [lon, lat],
      zoom: 7.7,
      attributionControl: false,
    });

    map.addControl(new maplibregl.NavigationControl({ showCompass: true }), 'top-right');

    map.on('load', () => {
      setMapReady(true);
    });

    mapRef.current = map;
    return () => {
      markersRef.current.forEach((m) => m.remove());
      markersRef.current = [];
      setMapReady(false);
      map.remove();
      mapRef.current = null;
    };
  }, [lat, lon]);

  // Populate / update raster radar & GeoJSON vector layers once loaded
  useEffect(() => {
    const map = mapRef.current;
    const data = overlayQuery.data;
    if (!map || !mapReady || !data) return;

    // 1. Add NEXRAD animated frame sources/layers
    data.radar_frames.forEach((fr, idx) => {
      const srcId = `nexrad-src-${idx}`;
      const lyrId = `nexrad-lyr-${idx}`;
      if (!map.getSource(srcId)) {
        map.addSource(srcId, {
          type: 'raster',
          tiles: [fr.tile_url],
          tileSize: 256,
        });
        map.addLayer({
          id: lyrId,
          type: 'raster',
          source: srcId,
          paint: {
            'raster-opacity': radarProduct === 'nexrad' && idx === frameIdx ? 0.72 : 0,
            'raster-fade-duration': 0,
          },
        });
      }
    });

    // 2. MRMS Hybrid Scan Reflectivity layer
    if (!map.getSource('mrms-src')) {
      map.addSource('mrms-src', {
        type: 'raster',
        tiles: [data.mrms_tile_url],
        tileSize: 256,
      });
      map.addLayer({
        id: 'mrms-lyr',
        type: 'raster',
        source: 'mrms-src',
        paint: {
          'raster-opacity': radarProduct === 'mrms' ? 0.75 : 0,
        },
      });
    }

    // 3. SPC Convective Outlook GeoJSON
    const spcData =
      spcLayer === 'day1'
        ? data.spc_day1_geojson
        : spcLayer === 'day2'
          ? data.spc_day2_geojson
          : { type: 'FeatureCollection', features: [] };

    type MapGeoJsonData = Parameters<maplibregl.GeoJSONSource['setData']>[0];

    const existingSpc = map.getSource('spc-src') as maplibregl.GeoJSONSource | undefined;
    if (existingSpc) {
      existingSpc.setData(spcData as unknown as MapGeoJsonData);
    } else {
      map.addSource('spc-src', {
        type: 'geojson',
        data: spcData as unknown as MapGeoJsonData,
      });
      map.addLayer({
        id: 'spc-fill',
        type: 'fill',
        source: 'spc-src',
        paint: {
          'fill-color': ['coalesce', ['get', 'fill'], '#f59e0b'],
          'fill-opacity': 0.16,
        },
      });
      map.addLayer({
        id: 'spc-outline',
        type: 'line',
        source: 'spc-src',
        paint: {
          'line-color': ['coalesce', ['get', 'stroke'], '#fbbf24'],
          'line-width': 2,
        },
      });
    }

    // 4. Active NWS Warning Polygons
    const existingAlerts = map.getSource('alerts-src') as maplibregl.GeoJSONSource | undefined;
    if (existingAlerts) {
      existingAlerts.setData(data.alerts_geojson as unknown as MapGeoJsonData);
    } else {
      map.addSource('alerts-src', {
        type: 'geojson',
        data: data.alerts_geojson as unknown as MapGeoJsonData,
      });
      map.addLayer({
        id: 'alerts-fill',
        type: 'fill',
        source: 'alerts-src',
        paint: {
          'fill-color': [
            'case',
            ['boolean', ['get', 'is_life_threatening'], false],
            '#f43f5e',
            '#f59e0b',
          ],
          'fill-opacity': 0.24,
        },
      });
      map.addLayer({
        id: 'alerts-outline',
        type: 'line',
        source: 'alerts-src',
        paint: {
          'line-color': [
            'case',
            ['boolean', ['get', 'is_life_threatening'], false],
            '#fb7185',
            '#fbbf24',
          ],
          'line-width': 2.5,
        },
      });
    }

    // 5. 8 km Spatial Sampling Ring
    const existingRing = map.getSource('ring-src') as maplibregl.GeoJSONSource | undefined;
    if (existingRing) {
      existingRing.setData(data.ring_geojson as unknown as MapGeoJsonData);
    } else {
      map.addSource('ring-src', {
        type: 'geojson',
        data: data.ring_geojson as unknown as MapGeoJsonData,
      });
      map.addLayer({
        id: 'ring-line',
        type: 'line',
        source: 'ring-src',
        filter: ['==', ['geometry-type'], 'Polygon'],
        paint: {
          'line-color': '#38bdf8',
          'line-width': 1.5,
          'line-dasharray': [3, 2],
        },
      });
      map.addLayer({
        id: 'ring-pts',
        type: 'circle',
        source: 'ring-src',
        filter: ['==', ['geometry-type'], 'Point'],
        paint: {
          'circle-radius': 4,
          'circle-color': '#38bdf8',
          'circle-stroke-width': 1.5,
          'circle-stroke-color': '#0f172a',
        },
      });
    }
  }, [mapReady, overlayQuery.data, spcLayer, radarProduct, frameIdx]);

  // Smoothly toggle active radar frame opacity
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady || frames.length === 0) return;

    frames.forEach((_, idx) => {
      const lyrId = `nexrad-lyr-${idx}`;
      if (map.getLayer(lyrId)) {
        map.setPaintProperty(
          lyrId,
          'raster-opacity',
          radarProduct === 'nexrad' && idx === frameIdx ? 0.72 : 0
        );
      }
    });
    if (map.getLayer('mrms-lyr')) {
      map.setPaintProperty('mrms-lyr', 'raster-opacity', radarProduct === 'mrms' ? 0.75 : 0);
    }
  }, [frameIdx, radarProduct, mapReady, frames]);

  // Radar animation loop
  useEffect(() => {
    if (!playing || radarProduct !== 'nexrad' || frames.length === 0) return;
    const timer = window.setInterval(() => {
      setFrameIdx((prev) => (prev + 1) % frames.length);
    }, 650);
    return () => window.clearInterval(timer);
  }, [playing, radarProduct, frames.length]);

  // Render live 5-min Oklahoma Mesonet + ASOS station markers
  useEffect(() => {
    const map = mapRef.current;
    const data = overlayQuery.data;
    markersRef.current.forEach((m) => m.remove());
    markersRef.current = [];

    if (!map || !mapReady || !data || !showStations) return;

    for (const feat of data.stations_geojson.features) {
      if (!feat.geometry || feat.geometry.type !== 'Point') continue;
      const coords = feat.geometry.coordinates as [number, number];
      const p = feat.properties;
      const isMesonet = p.source === 'ok_mesonet';
      const tempVal =
        units === 'imperial'
          ? `${p.temp_f ?? ''}°`
          : `${p.temp_c ?? ''}°`;
      const windVal =
        units === 'imperial'
          ? p.wind_mph !== null && p.wind_mph !== undefined
            ? `${p.wind_mph}mph`
            : ''
          : p.wind_ms !== null && p.wind_ms !== undefined
            ? `${p.wind_ms}m/s`
            : '';
      const dir = Number(p.wind_dir_deg || 0);
      const compass = degToCompass(dir);

      const el = document.createElement('button');
      el.type = 'button';
      el.className = `px-2 py-0.5 rounded-lg text-[10px] font-bold shadow-lg border flex items-center gap-1 cursor-pointer ${
        isMesonet
          ? 'bg-slate-950/90 border-cyan-400/60 text-cyan-200'
          : 'bg-slate-950/85 border-indigo-400/50 text-indigo-200'
      }`;
      el.innerHTML = `<span>${String(p.station_id)}</span><span class="text-white">${tempVal}</span>${
        windVal ? `<span class="text-slate-400 font-normal">${compass} ${windVal}</span>` : ''
      }`;

      const popupHtml = `
        <div style="font-size:11px;line-height:1.45">
          <div style="font-weight:700;color:#38bdf8">${String(p.name)} (${String(p.station_id)})</div>
          <div style="color:#94a3b8;font-size:10px;margin-bottom:4px">${
            isMesonet ? 'Oklahoma Mesonet · 5-Min Research Tower' : 'NWS / FAA ASOS Automated Station'
          }</div>
          <div>Temp: <b>${p.temp_f}°F (${p.temp_c}°C)</b></div>
          <div>Wind: <b>${compass} ${p.wind_mph ?? 0} mph</b>${
            p.gust_mph ? ` (Gust ${p.gust_mph} mph)` : ''
          }</div>
        </div>
      `;

      const marker = new maplibregl.Marker({ element: el })
        .setLngLat(coords)
        .setPopup(new maplibregl.Popup({ offset: 12 }).setHTML(popupHtml))
        .addTo(map);

      markersRef.current.push(marker);
    }
  }, [mapReady, overlayQuery.data, showStations, units]);

  const currentFrame = frames[frameIdx];

  return (
    <section className="glass-panel rounded-2xl overflow-hidden">
      <div className="p-4 sm:px-6 border-b border-slate-800/80 flex flex-col lg:flex-row lg:items-center justify-between gap-3">
        <div>
          <h3 className="text-base font-bold text-white flex items-center gap-2">
            <Radar className="w-4 h-4 text-cyan-400" />
            Live NEXRAD / MRMS Radar & 5-Min Mesonet Towers
          </h3>
          <p className="text-xs text-slate-400">
            Centered on {locationName} (dashed cyan ring = 8 km 5-point convective sampling perimeter).
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2 text-xs">
          <div className="inline-flex rounded-xl bg-slate-900 border border-slate-800 p-0.5">
            <button
              type="button"
              onClick={() => setRadarProduct('nexrad')}
              className={`px-2.5 py-1 rounded-lg font-medium cursor-pointer ${
                radarProduct === 'nexrad' ? 'bg-cyan-500 text-slate-950 font-semibold' : 'text-slate-400'
              }`}
            >
              NEXRAD 5m Loop
            </button>
            <button
              type="button"
              onClick={() => {
                setPlaying(false);
                setRadarProduct('mrms');
              }}
              className={`px-2.5 py-1 rounded-lg font-medium cursor-pointer ${
                radarProduct === 'mrms' ? 'bg-cyan-500 text-slate-950 font-semibold' : 'text-slate-400'
              }`}
            >
              NOAA MRMS Hybrid
            </button>
          </div>

          <div className="inline-flex rounded-xl bg-slate-900 border border-slate-800 p-0.5">
            {(['none', 'day1', 'day2'] as const).map((mode) => (
              <button
                key={mode}
                type="button"
                onClick={() => setSpcLayer(mode)}
                className={`px-2.5 py-1 rounded-lg font-medium cursor-pointer flex items-center gap-1 ${
                  spcLayer === mode ? 'bg-amber-500/25 text-amber-200 font-semibold' : 'text-slate-400'
                }`}
              >
                {mode === 'day1' && <ShieldAlert className="w-3 h-3 text-amber-400" />}
                {mode === 'none' ? 'SPC Off' : mode === 'day1' ? 'SPC Day 1' : 'SPC Day 2'}
              </button>
            ))}
          </div>

          <button
            type="button"
            onClick={() => setShowStations((v) => !v)}
            className={`px-2.5 py-1.5 rounded-xl border flex items-center gap-1.5 cursor-pointer ${
              showStations
                ? 'bg-emerald-500/15 border-emerald-400/40 text-emerald-200'
                : 'bg-slate-900 border-slate-800 text-slate-400'
            }`}
          >
            <Radio className="w-3.5 h-3.5" />
            Live Towers ({overlayQuery.data?.stations_geojson.features.length ?? 0})
          </button>
        </div>
      </div>

      <div className="relative w-full h-[470px] bg-slate-950">
        <div ref={containerRef} className="w-full h-full" />

        {/* Radar Timeline Scrubber Bar */}
        {radarProduct === 'nexrad' && frames.length > 0 && (
          <div className="absolute bottom-3 left-3 right-3 sm:left-6 sm:right-6 z-10 p-2.5 px-3.5 rounded-xl bg-slate-950/90 border border-slate-800 backdrop-blur-md flex items-center gap-3">
            <button
              type="button"
              onClick={() => setPlaying((p) => !p)}
              className="p-2 rounded-lg bg-cyan-500 hover:bg-cyan-400 text-slate-950 font-bold cursor-pointer"
              title={playing ? 'Pause radar loop' : 'Play 50-minute NEXRAD loop'}
            >
              {playing ? <Pause className="w-3.5 h-3.5" /> : <Play className="w-3.5 h-3.5" />}
            </button>
            <input
              type="range"
              min={0}
              max={frames.length - 1}
              value={frameIdx}
              onChange={(e) => {
                setPlaying(false);
                setFrameIdx(Number(e.target.value));
              }}
              className="flex-1 accent-cyan-400 cursor-pointer"
            />
            <span className="text-xs font-mono font-semibold text-cyan-300 w-14 text-right">
              {currentFrame?.label || 'Live'}
            </span>
          </div>
        )}

        <div className="absolute top-3 left-3 z-10 px-2.5 py-1 rounded-lg bg-slate-950/85 border border-slate-800 text-[10px] text-slate-300 flex items-center gap-2">
          <Layers className="w-3 h-3 text-cyan-400" />
          <span>Cyan pins: OK Mesonet (5m) · Indigo pins: ASOS</span>
        </div>
      </div>
    </section>
  );
};
