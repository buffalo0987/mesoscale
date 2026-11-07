import React, { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import maplibregl from 'maplibre-gl';
import {
  Cpu,
  Eye,
  Layers,
  Loader2,
  MapPin,
  Pause,
  Play,
  Radar,
  Radio,
  ShieldAlert,
  X,
} from 'lucide-react';
import { api } from '../api';
import type { GeoSearchItem, PinnedLocation, UnitSystem } from '../types';
import { degToCompass } from '../units';

interface RadarMesonetMapProps {
  lat: number;
  lon: number;
  locationName: string;
  units: UnitSystem;
  onSelectEphemeral?: (lat: number, lon: number, name?: string) => void;
  onPinnedNew?: (loc: PinnedLocation) => void;
}

export const RadarMesonetMap: React.FC<RadarMesonetMapProps> = ({
  lat,
  lon,
  locationName,
  units,
  onSelectEphemeral,
  onPinnedNew,
}) => {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const markersRef = useRef<maplibregl.Marker[]>([]);
  const homeMarkerRef = useRef<maplibregl.Marker | null>(null);
  const tappedMarkerRef = useRef<maplibregl.Marker | null>(null);

  const [playing, setPlaying] = useState(false);
  const [frameIdx, setFrameIdx] = useState(10); // 10 == Live (0m)
  const [radarProduct, setRadarProduct] = useState<'nexrad' | 'mrms'>('nexrad');
  const [spcLayer, setSpcLayer] = useState<'none' | 'day1' | 'day2' | 'hail'>('day1');
  const [showStations, setShowStations] = useState(true);
  const [mapReady, setMapReady] = useState(false);

  // Tap-on-Map Rooftop Pinning state
  const [tappedCoords, setTappedCoords] = useState<{ lat: number; lon: number } | null>(null);
  const [tappedGeo, setTappedGeo] = useState<GeoSearchItem | null>(null);
  const [resolvingTap, setResolvingTap] = useState(false);
  const [tappedNickname, setTappedNickname] = useState('');
  const [pinningTap, setPinningTap] = useState(false);

  const overlayQuery = useQuery({
    queryKey: ['map-overlay', lat.toFixed(3), lon.toFixed(3)],
    queryFn: () => api.getMapOverlay(lat, lon),
    refetchInterval: 2 * 60 * 1000,
  });

  const frames = overlayQuery.data?.radar_frames ?? [];
  const hailCoreCount =
    overlayQuery.data?.hail_cells_geojson?.features.filter(
      (f) => f.properties?.feature_kind === 'hail_core'
    ).length ?? 0;

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

    // Exact active rooftop pin marker at center
    const homeEl = document.createElement('div');
    homeEl.className =
      'w-3.5 h-3.5 rounded-full bg-cyan-400 border-2 border-slate-950 shadow-[0_0_10px_rgba(34,211,238,0.9)]';
    homeEl.title = `${locationName} (${lat.toFixed(4)}°, ${lon.toFixed(4)}°)`;
    homeMarkerRef.current = new maplibregl.Marker({ element: homeEl })
      .setLngLat([lon, lat])
      .addTo(map);

    map.on('load', () => {
      setMapReady(true);
    });

    // Tap anywhere on map to inspect / pin exact rooftop coordinates, or inspect a hail core
    map.on('click', (e) => {
      if (map.getLayer('hail-cores')) {
        const hits = map.queryRenderedFeatures(e.point, { layers: ['hail-cores'] });
        if (hits.length > 0) {
          const hp = hits[0].properties || {};
          const html = `
            <div style="font-size:11px;line-height:1.45">
              <div style="font-weight:700;color:#f472b6">NEXRAD Cell ${String(hp.cell_key || '')}</div>
              <div>Est. Max Hail: <b>${String(hp.hail_label || '')}</b></div>
              <div>POSH (Severe ≥1"): <b>${Number(hp.posh_pct || 0)}%</b> · POH: <b>${Number(hp.poh_pct || 0)}%</b></div>
              <div>Storm Motion: <b>${Math.round(Number(hp.speed_mph || 0))} mph</b></div>
            </div>
          `;
          new maplibregl.Popup({ offset: 10 })
            .setLngLat(e.lngLat)
            .setHTML(html)
            .addTo(map);
          return;
        }
      }

      const clickLat = Number(e.lngLat.lat.toFixed(5));
      const clickLon = Number(e.lngLat.lng.toFixed(5));
      setTappedCoords({ lat: clickLat, lon: clickLon });
      setTappedGeo(null);
      setResolvingTap(true);

      if (tappedMarkerRef.current) {
        tappedMarkerRef.current.setLngLat([clickLon, clickLat]);
      } else {
        const pinEl = document.createElement('div');
        pinEl.className =
          'w-4 h-4 rounded-full bg-emerald-400 border-2 border-white shadow-[0_0_12px_rgba(52,211,153,0.95)]';
        tappedMarkerRef.current = new maplibregl.Marker({ element: pinEl })
          .setLngLat([clickLon, clickLat])
          .addTo(map);
      }

      api
        .reverseGeocode(clickLat, clickLon)
        .then((res) => {
          setTappedGeo(res.result);
        })
        .catch(() => {
          setTappedGeo(null);
        })
        .finally(() => {
          setResolvingTap(false);
        });
    });

    mapRef.current = map;
    return () => {
      markersRef.current.forEach((m) => m.remove());
      markersRef.current = [];
      homeMarkerRef.current?.remove();
      homeMarkerRef.current = null;
      tappedMarkerRef.current?.remove();
      tappedMarkerRef.current = null;
      setTappedCoords(null);
      setMapReady(false);
      map.remove();
      mapRef.current = null;
    };
  }, [lat, lon, locationName]);

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

    // 3. SPC Convective / Hail Outlook GeoJSON
    const spcData =
      spcLayer === 'day1'
        ? data.spc_day1_geojson
        : spcLayer === 'day2'
          ? data.spc_day2_geojson
          : spcLayer === 'hail'
            ? (data.spc_day1_hail_geojson ?? { type: 'FeatureCollection', features: [] })
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
          'fill-color': [
            'coalesce',
            ['get', 'fill'],
            ['get', 'FILL'],
            ['get', 'mesoscale_color'],
            '#f59e0b',
          ],
          'fill-opacity': 0.16,
        },
      });
      map.addLayer({
        id: 'spc-outline',
        type: 'line',
        source: 'spc-src',
        paint: {
          'line-color': [
            'coalesce',
            ['get', 'stroke'],
            ['get', 'STROKE'],
            ['get', 'mesoscale_color'],
            '#fbbf24',
          ],
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

    // 6. Live NEXRAD Level-III Hail Cores + 45-Minute Forward Track Vectors
    const hailData = data.hail_cells_geojson ?? { type: 'FeatureCollection', features: [] };
    const existingHail = map.getSource('hail-src') as maplibregl.GeoJSONSource | undefined;
    if (existingHail) {
      existingHail.setData(hailData as unknown as MapGeoJsonData);
    } else {
      map.addSource('hail-src', {
        type: 'geojson',
        data: hailData as unknown as MapGeoJsonData,
      });
      map.addLayer({
        id: 'hail-tracks',
        type: 'line',
        source: 'hail-src',
        filter: ['==', ['get', 'feature_kind'], 'hail_track'],
        paint: {
          'line-color': '#e879f9',
          'line-width': 2.5,
          'line-dasharray': [2, 2],
        },
      });
      map.addLayer({
        id: 'hail-cores',
        type: 'circle',
        source: 'hail-src',
        filter: ['==', ['get', 'feature_kind'], 'hail_core'],
        paint: {
          'circle-radius': ['case', ['>=', ['to-number', ['get', 'hail_in']], 1.0], 7.5, 6],
          'circle-color': [
            'case',
            ['>=', ['to-number', ['get', 'hail_in']], 1.0],
            '#f43f5e',
            '#d946ef',
          ],
          'circle-stroke-width': 2,
          'circle-stroke-color': '#ffffff',
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
      el.addEventListener('click', (ev) => ev.stopPropagation());

      const popupHtml = `
        <div style="font-size:11px;line-height:1.45">
          <div style="font-weight:700;color:#38bdf8">${String(p.name)} (${String(p.station_id)})</div>
          <div style="color:#94a3b8;font-size:10px;margin-bottom:4px">${
            isMesonet ? 'Oklahoma Mesonet · 5-Min Research Tower' : 'NWS / FAA / ICAO Automated Station'
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

  const clearTappedPin = () => {
    tappedMarkerRef.current?.remove();
    tappedMarkerRef.current = null;
    setTappedCoords(null);
    setTappedGeo(null);
    setTappedNickname('');
  };

  const handlePinTappedRooftop = async () => {
    if (!tappedCoords || !onPinnedNew) return;
    setPinningTap(true);
    try {
      const nick = tappedNickname.trim();
      const res = await api.pinLocation({
        name: tappedGeo?.name || `${tappedCoords.lat.toFixed(4)}, ${tappedCoords.lon.toFixed(4)}`,
        custom_name: nick || undefined,
        address_label: tappedGeo?.display_address || tappedGeo?.name,
        admin1: tappedGeo?.admin1,
        country: tappedGeo?.country,
        lat: tappedCoords.lat,
        lon: tappedCoords.lon,
        elevation_m: tappedGeo?.elevation_m,
        timezone: tappedGeo?.timezone,
      });
      clearTappedPin();
      onPinnedNew(res.location);
    } finally {
      setPinningTap(false);
    }
  };

  const handleInstantTappedForecast = () => {
    if (!tappedCoords || !onSelectEphemeral) return;
    const nick = tappedNickname.trim();
    const label = nick || tappedGeo?.name || `${tappedCoords.lat.toFixed(4)}, ${tappedCoords.lon.toFixed(4)}`;
    clearTappedPin();
    onSelectEphemeral(tappedCoords.lat, tappedCoords.lon, label);
  };

  const currentFrame = frames[frameIdx];

  return (
    <section className="glass-panel rounded-2xl overflow-hidden">
      <div className="p-3.5 sm:p-4 sm:px-6 border-b border-slate-800/80 flex flex-col lg:flex-row lg:items-center justify-between gap-2.5 sm:gap-3">
        <div>
          <h3 className="text-sm sm:text-base font-bold text-white flex items-center gap-2">
            <Radar className="w-4 h-4 text-cyan-400 shrink-0" />
            Live NEXRAD / MRMS Radar, Hail Tracks &amp; Rooftop Pinning
          </h3>
          <p className="text-[11px] sm:text-xs text-slate-400 mt-0.5">
            Centered on {locationName} (8 km convective ring · tap anywhere on the map to drop an exact rooftop pin).
          </p>
        </div>

        <div className="flex items-center gap-2 overflow-x-auto no-scrollbar pb-0.5 lg:flex-wrap text-xs">
          <div className="inline-flex rounded-xl bg-slate-900 border border-slate-800 p-0.5 shrink-0">
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

          <div className="inline-flex rounded-xl bg-slate-900 border border-slate-800 p-0.5 shrink-0">
            {(['none', 'day1', 'day2', 'hail'] as const).map((mode) => (
              <button
                key={mode}
                type="button"
                onClick={() => setSpcLayer(mode)}
                className={`px-2.5 py-1 rounded-lg font-medium cursor-pointer flex items-center gap-1 ${
                  spcLayer === mode
                    ? mode === 'hail'
                      ? 'bg-fuchsia-500/25 text-fuchsia-200 font-semibold'
                      : 'bg-amber-500/25 text-amber-200 font-semibold'
                    : 'text-slate-400'
                }`}
              >
                {(mode === 'day1' || mode === 'hail') && (
                  <ShieldAlert
                    className={`w-3 h-3 ${mode === 'hail' ? 'text-fuchsia-400' : 'text-amber-400'}`}
                  />
                )}
                {mode === 'none'
                  ? 'SPC Off'
                  : mode === 'day1'
                    ? 'SPC Day 1'
                    : mode === 'day2'
                      ? 'SPC Day 2'
                      : 'SPC Hail'}
              </button>
            ))}
          </div>

          <button
            type="button"
            onClick={() => setShowStations((v) => !v)}
            className={`px-2.5 py-1.5 rounded-xl border flex items-center gap-1.5 cursor-pointer shrink-0 ${
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

      <div className="relative w-full h-[calc(100dvh-250px)] min-h-[360px] max-h-[520px] sm:h-[490px] sm:max-h-none bg-slate-950">
        <div ref={containerRef} className="w-full h-full" />

        {/* Legend Pill */}
        <div className="absolute top-3 left-3 z-10 max-w-[calc(100%-56px)] px-2.5 py-1 rounded-lg bg-slate-950/85 border border-slate-800 text-[10px] text-slate-300 flex items-center gap-1.5">
          <Layers className="w-3 h-3 text-cyan-400 shrink-0" />
          <span className="truncate">
            Cyan: OK Mesonet · Indigo: ASOS/METAR
            {hailCoreCount > 0
              ? ` · Magenta: ${hailCoreCount} Hail Core(s) + 45m Track`
              : ' · Tap map to pin rooftop'}
          </span>
        </div>

        {/* Tap-on-Map Rooftop Pinning Card */}
        {tappedCoords && (
          <div className="absolute top-11 left-3 right-12 sm:left-auto sm:right-14 sm:w-80 z-20 p-3 rounded-2xl bg-slate-950/95 border border-emerald-400/45 backdrop-blur-md shadow-2xl space-y-2.5 text-xs">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <div className="font-bold text-emerald-300 flex items-center gap-1.5">
                  <MapPin className="w-3.5 h-3.5 shrink-0" />
                  <span className="truncate">
                    {resolvingTap
                      ? 'Resolving rooftop address...'
                      : tappedGeo?.name || 'Dropped Rooftop Pin'}
                  </span>
                </div>
                {tappedGeo?.display_address &&
                  tappedGeo.display_address !== tappedGeo.name && (
                    <div className="text-[11px] text-slate-300 line-clamp-2 mt-0.5">
                      {tappedGeo.display_address}
                    </div>
                  )}
                <div className="text-[10px] font-mono text-slate-400 mt-0.5">
                  {tappedCoords.lat.toFixed(5)}°, {tappedCoords.lon.toFixed(5)}°
                  {tappedGeo?.elevation_m !== undefined && (
                    <>
                      {' · Elev '}
                      {units === 'imperial'
                        ? `${Math.round(tappedGeo.elevation_m * 3.28084)} ft`
                        : `${Math.round(tappedGeo.elevation_m)} m`}
                    </>
                  )}
                </div>
              </div>
              <button
                type="button"
                onClick={clearTappedPin}
                className="p-1 rounded-lg bg-slate-900 text-slate-400 hover:text-white cursor-pointer shrink-0"
                title="Cancel dropped pin"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>

            <input
              type="text"
              value={tappedNickname}
              onChange={(e) => setTappedNickname(e.target.value)}
              placeholder='Optional label (e.g., "Home", "Ranch")'
              className="w-full px-2.5 py-1.5 rounded-lg bg-slate-900 border border-slate-700 text-base sm:text-xs text-white placeholder-slate-500 focus:outline-none focus:border-emerald-400"
            />

            <div className="flex items-center gap-2">
              {onSelectEphemeral && (
                <button
                  type="button"
                  onClick={handleInstantTappedForecast}
                  className="flex-1 py-1.5 px-2.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-200 font-medium flex items-center justify-center gap-1 cursor-pointer"
                >
                  <Eye className="w-3.5 h-3.5" />
                  Instant View
                </button>
              )}
              {onPinnedNew && (
                <button
                  type="button"
                  onClick={handlePinTappedRooftop}
                  disabled={pinningTap}
                  className="flex-1 py-1.5 px-2.5 rounded-xl bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold flex items-center justify-center gap-1 cursor-pointer"
                >
                  {pinningTap ? (
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  ) : (
                    <Cpu className="w-3.5 h-3.5" />
                  )}
                  Pin Rooftop
                </button>
              )}
            </div>
          </div>
        )}

        {/* Radar Timeline Scrubber Bar */}
        {radarProduct === 'nexrad' && frames.length > 0 && (
          <div className="absolute bottom-3 left-3 right-3 sm:left-6 sm:right-6 z-10 p-2 sm:p-2.5 px-3 sm:px-3.5 rounded-xl bg-slate-950/90 border border-slate-800 backdrop-blur-md flex items-center gap-2.5 sm:gap-3">
            <button
              type="button"
              onClick={() => setPlaying((p) => !p)}
              className="p-2 rounded-lg bg-cyan-500 hover:bg-cyan-400 text-slate-950 font-bold cursor-pointer shrink-0"
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
            <span className="text-xs font-mono font-semibold text-cyan-300 w-12 sm:w-14 text-right shrink-0">
              {currentFrame?.label || 'Live'}
            </span>
          </div>
        )}
      </div>
    </section>
  );
};
