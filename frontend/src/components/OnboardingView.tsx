import React, { useState } from 'react';
import { Compass, Cpu, Home, Loader2, MapPin, Navigation, Radar, Search, Sparkles } from 'lucide-react';
import { api } from '../api';
import type { GeoSearchItem, PinnedLocation } from '../types';

interface OnboardingViewProps {
  onPinned: (loc: PinnedLocation) => void;
  onEphemeralSelect: (lat: number, lon: number, name?: string) => void;
}

export const OnboardingView: React.FC<OnboardingViewProps> = ({
  onPinned,
  onEphemeralSelect,
}) => {
  const [query, setQuery] = useState('');
  const [nickname, setNickname] = useState('');
  const [results, setResults] = useState<GeoSearchItem[]>([]);
  const [searching, setSearching] = useState(false);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleSearch = async (e: React.FormEvent) => {
    e.preventDefault();
    if (query.trim().length < 2) return;
    setSearching(true);
    setError(null);
    try {
      const res = await api.searchLocations(query.trim());
      setResults(res.results);
      if (res.results.length === 0) {
        setError(
          'No matching locations found. Try a street address, lat/lon (e.g., 35.222, -97.439), ZIP, or city.'
        );
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Search failed');
    } finally {
      setSearching(false);
    }
  };

  const handlePinItem = async (item: GeoSearchItem) => {
    setBusyAction(`pin:${item.lat}:${item.lon}`);
    setError(null);
    try {
      const cleanNick = nickname.trim();
      const res = await api.pinLocation({
        name: item.name,
        custom_name: cleanNick || undefined,
        address_label: item.display_address || item.name,
        admin1: item.admin1,
        country: item.country,
        lat: item.lat,
        lon: item.lon,
        elevation_m: item.elevation_m,
        timezone: item.timezone,
      });
      onPinned(res.location);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to pin location');
      setBusyAction(null);
    }
  };

  const handleUseGps = (pinDirectly: boolean) => {
    if (!navigator.geolocation) {
      setError('Browser geolocation is unavailable.');
      return;
    }
    setBusyAction(pinDirectly ? 'gps-pin' : 'gps-live');
    setError(null);
    navigator.geolocation.getCurrentPosition(
      async (pos) => {
        const { latitude, longitude } = pos.coords;
        try {
          if (pinDirectly) {
            const res = await api.pinLocation({
              lat: latitude,
              lon: longitude,
              custom_name: nickname.trim() || undefined,
            });
            onPinned(res.location);
          } else {
            onEphemeralSelect(latitude, longitude);
          }
        } catch (err) {
          setError(err instanceof Error ? err.message : 'GPS lookup failed');
          setBusyAction(null);
        }
      },
      (geoErr) => {
        setError(
          geoErr.message ||
            'Location permission denied. Ensure HTTPS (e.g., Tailscale) is active on mobile.'
        );
        setBusyAction(null);
      },
      { enableHighAccuracy: true, timeout: 10000 }
    );
  };

  return (
    <div className="min-h-screen flex flex-col items-center justify-center px-4 py-12 bg-[radial-gradient(ellipse_at_top,_rgba(14,116,144,0.22),_transparent_60%)]">
      <div className="w-full max-w-2xl space-y-8">
        <div className="text-center space-y-3">
          <div className="inline-flex items-center gap-2 px-3.5 py-1.5 rounded-full bg-cyan-500/10 border border-cyan-400/25 text-cyan-300 text-xs font-medium tracking-wide uppercase">
            <Radar className="w-3.5 h-3.5" />
            Self-Learning Mesoscale Engine
          </div>
          <h1 className="text-4xl sm:text-5xl font-bold tracking-tight text-white">
            Mesoscale
          </h1>
          <p className="text-slate-400 text-sm sm:text-base max-w-xl mx-auto leading-relaxed">
            Fuses <span className="text-slate-200 font-medium">HRRR 15-min</span>,{' '}
            <span className="text-slate-200 font-medium">RRFS v1</span>,{' '}
            <span className="text-slate-200 font-medium">NBM</span>, and{' '}
            <span className="text-slate-200 font-medium">ECMWF IFS/AIFS</span> with{' '}
            <span className="text-cyan-300 font-medium">5-minute Oklahoma Mesonet & ASOS</span>{' '}
            ground truth—training a local AI model calibrated to your exact coordinates.
          </p>
        </div>

        <div className="glass-panel rounded-2xl p-4 sm:p-8 shadow-2xl space-y-4 sm:space-y-5">
          <form onSubmit={handleSearch} className="flex flex-col sm:flex-row gap-2.5">
            <div className="relative flex-1">
              <Search className="w-4 h-4 text-slate-400 absolute left-3.5 top-1/2 -translate-y-1/2" />
              <input
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Street address, lat/lon, ZIP, or city (e.g., 123 Main St, Norman OK)..."
                className="w-full pl-10 pr-4 py-3 rounded-xl bg-slate-900/90 border border-slate-700/80 text-base sm:text-sm text-white placeholder-slate-500 focus:outline-none focus:border-cyan-400 transition"
                autoFocus
              />
            </div>
            <button
              type="submit"
              disabled={searching || query.trim().length < 2}
              className="px-5 py-3 rounded-xl bg-cyan-500 hover:bg-cyan-400 disabled:opacity-50 text-slate-950 font-semibold text-sm transition flex items-center justify-center gap-2 cursor-pointer"
            >
              {searching ? <Loader2 className="w-4 h-4 animate-spin" /> : 'Search'}
            </button>
          </form>

          <div className="flex items-center gap-2 px-3 py-2 rounded-xl bg-slate-900/60 border border-slate-800/80">
            <Home className="w-3.5 h-3.5 text-cyan-400 shrink-0" />
            <input
              type="text"
              value={nickname}
              onChange={(e) => setNickname(e.target.value)}
              placeholder='Optional label when pinning (e.g., "Home", "Garage", "Aruba Resort")'
              className="w-full bg-transparent text-base sm:text-xs text-slate-200 placeholder-slate-500 focus:outline-none"
            />
          </div>

          <div className="flex flex-wrap items-center gap-2.5 pt-1">
            <button
              type="button"
              onClick={() => handleUseGps(true)}
              disabled={busyAction !== null}
              className="flex-1 min-w-[200px] py-2.5 px-4 rounded-xl bg-indigo-500/15 hover:bg-indigo-500/25 border border-indigo-400/30 text-indigo-200 text-xs font-medium transition flex items-center justify-center gap-2 cursor-pointer"
            >
              {busyAction === 'gps-pin' ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Sparkles className="w-4 h-4 text-indigo-300" />
              )}
              Pin Current GPS & Train Local AI
            </button>
            <button
              type="button"
              onClick={() => handleUseGps(false)}
              disabled={busyAction !== null}
              className="flex-1 min-w-[180px] py-2.5 px-4 rounded-xl bg-slate-800/80 hover:bg-slate-800 border border-slate-700 text-slate-300 text-xs font-medium transition flex items-center justify-center gap-2 cursor-pointer"
            >
              {busyAction === 'gps-live' ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Navigation className="w-4 h-4 text-cyan-400" />
              )}
              Instant GPS Lookup (&lt;2s)
            </button>
          </div>

          {error && (
            <div className="p-3 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-300 text-xs">
              {error}
            </div>
          )}

          {results.length > 0 && (
            <div className="divide-y divide-slate-800/80 border border-slate-800 rounded-xl overflow-hidden bg-slate-950/50">
              {results.map((item) => {
                const key = `pin:${item.lat}:${item.lon}`;
                const isPinning = busyAction === key;
                return (
                  <div
                    key={`${item.name}-${item.lat}-${item.lon}`}
                    className="p-3.5 flex flex-col sm:flex-row sm:items-center justify-between gap-3 hover:bg-slate-900/60 transition"
                  >
                    <div className="flex items-start gap-2.5 min-w-0">
                      <MapPin className="w-4 h-4 text-cyan-400 mt-0.5 shrink-0" />
                      <div className="min-w-0">
                        <div className="text-sm font-semibold text-white flex flex-wrap items-center gap-1.5">
                          <span>
                            {item.name}
                            {item.admin1 && !item.name.includes(item.admin1) ? `, ${item.admin1}` : ''}
                            {item.country && item.country !== 'US' ? ` (${item.country})` : ''}
                          </span>
                          {item.result_type === 'address' && (
                            <span className="px-1.5 py-0.2 rounded bg-cyan-500/20 text-cyan-300 text-[10px] font-semibold">
                              Exact Address
                            </span>
                          )}
                        </div>
                        {item.display_address && item.display_address !== item.name && (
                          <div className="text-[11px] text-slate-300/90 truncate max-w-md">
                            {item.display_address}
                          </div>
                        )}
                        <div className="text-xs text-slate-400">
                          {item.lat.toFixed(4)}°, {item.lon.toFixed(4)}° • Elev{' '}
                          {Math.round(item.elevation_m * 3.28084)} ft
                        </div>
                      </div>
                    </div>
                    <div className="flex items-center gap-2 self-end sm:self-center">
                      <button
                        type="button"
                        onClick={() => onEphemeralSelect(item.lat, item.lon, item.name)}
                        disabled={busyAction !== null}
                        className="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-medium transition cursor-pointer"
                      >
                        Instant View
                      </button>
                      <button
                        type="button"
                        onClick={() => handlePinItem(item)}
                        disabled={busyAction !== null}
                        className="px-3.5 py-1.5 rounded-lg bg-cyan-500 hover:bg-cyan-400 text-slate-950 text-xs font-semibold transition flex items-center gap-1.5 cursor-pointer"
                      >
                        {isPinning ? (
                          <Loader2 className="w-3.5 h-3.5 animate-spin" />
                        ) : (
                          <Cpu className="w-3.5 h-3.5" />
                        )}
                        Pin & Train AI
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 text-xs text-slate-400">
          <div className="glass-panel rounded-xl p-4 space-y-1">
            <div className="text-cyan-300 font-semibold flex items-center gap-1.5">
              <Cpu className="w-3.5 h-3.5" /> Zero Cold-Start MOS
            </div>
            <p>Backfills 365 days of archived forecasts + local towers to train CatBoost & LightGBM on Day 1.</p>
          </div>
          <div className="glass-panel rounded-xl p-4 space-y-1">
            <div className="text-cyan-300 font-semibold flex items-center gap-1.5">
              <Compass className="w-3.5 h-3.5" /> 5-Min Kalman Frontal Filter
            </div>
            <p>Tracks real-time level & cooling-rate errors from nearby Mesonet/ASOS towers before NWP catches up.</p>
          </div>
          <div className="glass-panel rounded-xl p-4 space-y-1">
            <div className="text-cyan-300 font-semibold flex items-center gap-1.5">
              <Sparkles className="w-3.5 h-3.5" /> Auditable Scoreboard
            </div>
            <p>Grades Mesoscale AI head-to-head against NOAA NBM, HRRR, RRFS v1, and ECMWF every single day.</p>
          </div>
        </div>
      </div>
    </div>
  );
};
