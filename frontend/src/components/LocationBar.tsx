import React, { useState } from 'react';
import {
  Bell,
  BellOff,
  Cpu,
  Home,
  Loader2,
  MapPin,
  Navigation,
  Plus,
  Radar,
  Search,
  Settings,
  Trash2,
  X,
} from 'lucide-react';
import { api } from '../api';
import type { GeoSearchItem, PinnedLocation, UnitSystem } from '../types';

interface LocationBarProps {
  pinnedLocations: PinnedLocation[];
  activeLocationId: number | null;
  ephemeralLabel: string | null;
  units: UnitSystem;
  onSelectPinned: (id: number) => void;
  onSelectEphemeral: (lat: number, lon: number, name?: string) => void;
  onPinnedNew: (loc: PinnedLocation) => void;
  onDeletePinned: (id: number) => void;
  onLocationUpdated: () => void;
  onToggleUnits: () => void;
  onOpenSettings: () => void;
}

export const LocationBar: React.FC<LocationBarProps> = ({
  pinnedLocations,
  activeLocationId,
  ephemeralLabel,
  units,
  onSelectPinned,
  onSelectEphemeral,
  onPinnedNew,
  onDeletePinned,
  onLocationUpdated,
  onToggleUnits,
  onOpenSettings,
}) => {
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [nickname, setNickname] = useState('');
  const [results, setResults] = useState<GeoSearchItem[]>([]);
  const [busy, setBusy] = useState(false);

  const runSearch = async (e: React.FormEvent) => {
    e.preventDefault();
    if (query.trim().length < 2) return;
    setBusy(true);
    try {
      const res = await api.searchLocations(query.trim());
      setResults(res.results);
    } finally {
      setBusy(false);
    }
  };

  const handleGps = () => {
    if (!navigator.geolocation) return;
    navigator.geolocation.getCurrentPosition((pos) => {
      onSelectEphemeral(pos.coords.latitude, pos.coords.longitude);
      setSearchOpen(false);
    });
  };

  return (
    <header className="sticky top-0 z-30 safe-top border-b border-slate-800/80 bg-[#070b14]/90 backdrop-blur-xl">
      <div className="max-w-7xl mx-auto px-3 sm:px-6 h-14 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 sm:gap-3 min-w-0 overflow-x-auto no-scrollbar">
          <div className="flex items-center gap-2 shrink-0 pr-2 border-r border-slate-800">
            <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-cyan-400 to-indigo-500 flex items-center justify-center text-slate-950 font-bold">
              <Radar className="w-4 h-4" />
            </div>
            <span className="font-bold tracking-tight text-white hidden sm:inline">Mesoscale</span>
          </div>

          <div className="flex items-center gap-1.5">
            {pinnedLocations.map((loc) => {
              const isActive = activeLocationId === loc.id && !ephemeralLabel;
              const isTraining = loc.backfill_status === 'running' || loc.backfill_status === 'pending';
              return (
                <div
                  key={loc.id}
                  className={`group flex items-center gap-1.5 px-2.5 sm:px-3 py-1.5 rounded-lg text-xs font-medium transition shrink-0 cursor-pointer ${
                    isActive
                      ? 'bg-cyan-500/15 text-cyan-200 border border-cyan-400/35'
                      : 'bg-slate-900/70 text-slate-400 hover:text-slate-200 border border-slate-800/70'
                  }`}
                  onClick={() => onSelectPinned(loc.id)}
                >
                  <span className="max-w-[110px] sm:max-w-none truncate">{loc.name}</span>
                  {isTraining ? (
                    <Loader2 className="w-3 h-3 text-amber-400 animate-spin" />
                  ) : loc.model_status === 'trained' ? (
                    <span className="px-1.5 py-0.2 rounded bg-emerald-500/20 text-emerald-300 text-[10px] font-semibold">
                      AI
                    </span>
                  ) : null}
                  {isActive && pinnedLocations.length > 0 && (
                    <>
                      <button
                        type="button"
                        title={
                          loc.alerts_enabled !== false
                            ? 'Push & Hail alerts active for this location (tap to mute)'
                            : 'Push & Hail alerts muted for this location (tap to enable)'
                        }
                        onClick={async (e) => {
                          e.stopPropagation();
                          await api.updateLocation(loc.id, {
                            alerts_enabled: loc.alerts_enabled === false,
                          });
                          onLocationUpdated();
                        }}
                        className="opacity-70 hover:opacity-100 ml-0.5 cursor-pointer"
                      >
                        {loc.alerts_enabled !== false ? (
                          <Bell className="w-3 h-3 text-cyan-300" />
                        ) : (
                          <BellOff className="w-3 h-3 text-slate-500" />
                        )}
                      </button>
                      <button
                        type="button"
                        title="Remove pinned location"
                        onClick={(e) => {
                          e.stopPropagation();
                          onDeletePinned(loc.id);
                        }}
                        className="opacity-60 hover:opacity-100 hover:text-rose-400 ml-0.5 cursor-pointer"
                      >
                        <Trash2 className="w-3 h-3" />
                      </button>
                    </>
                  )}
                </div>
              );
            })}

            {ephemeralLabel && (
              <div className="flex items-center gap-1.5 px-2.5 sm:px-3 py-1.5 rounded-lg text-xs font-medium bg-indigo-500/20 text-indigo-200 border border-indigo-400/40 shrink-0">
                <Navigation className="w-3 h-3 text-indigo-300" />
                <span className="max-w-[100px] sm:max-w-none truncate">{ephemeralLabel}</span>
                <span className="text-[10px] uppercase tracking-wider text-indigo-300/80">Live</span>
              </div>
            )}

            <button
              type="button"
              onClick={() => setSearchOpen((v) => !v)}
              className="p-1.5 px-2 sm:px-2.5 rounded-lg bg-slate-900 hover:bg-slate-800 border border-slate-800 text-slate-300 text-xs flex items-center gap-1 shrink-0 cursor-pointer"
              title="Search or add location"
            >
              <Plus className="w-3.5 h-3.5 text-cyan-400" />
              <span className="hidden md:inline">Location</span>
            </button>
          </div>
        </div>

        <div className="flex items-center gap-1.5 sm:gap-2 shrink-0">
          <button
            type="button"
            onClick={handleGps}
            title="Instant Current GPS Forecast"
            className="p-2 rounded-lg bg-slate-900 hover:bg-slate-800 border border-slate-800 text-slate-300 transition cursor-pointer"
          >
            <Navigation className="w-4 h-4 text-cyan-400" />
          </button>
          <button
            type="button"
            onClick={onToggleUnits}
            className="px-2 sm:px-2.5 py-1.5 rounded-lg bg-slate-900 hover:bg-slate-800 border border-slate-800 text-[11px] sm:text-xs font-semibold text-slate-200 transition cursor-pointer"
          >
            {units === 'imperial' ? '°F · mph' : '°C · m/s'}
          </button>
          <button
            type="button"
            onClick={onOpenSettings}
            title="Push Alerts & PWS Settings"
            className="p-2 rounded-lg bg-slate-900 hover:bg-slate-800 border border-slate-800 text-slate-300 transition cursor-pointer"
          >
            <Settings className="w-4 h-4" />
          </button>
        </div>
      </div>

      {searchOpen && (
        <div className="border-t border-slate-800 bg-slate-950/95 px-3 sm:px-4 py-3.5 sm:py-4">
          <div className="max-w-2xl mx-auto space-y-3">
            <form onSubmit={runSearch} className="flex items-center gap-2">
              <div className="relative flex-1 min-w-0">
                <Search className="w-4 h-4 text-slate-400 absolute left-3 top-1/2 -translate-y-1/2" />
                <input
                  type="text"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Street address, lat/lon, ZIP, or city (US or global e.g. Aruba)..."
                  className="w-full pl-9 pr-3 py-2 rounded-xl bg-slate-900 border border-slate-700 text-base sm:text-sm text-white focus:outline-none focus:border-cyan-400"
                  autoFocus
                />
              </div>
              <button
                type="submit"
                disabled={busy}
                className="px-3.5 sm:px-4 py-2 rounded-xl bg-cyan-500 text-slate-950 font-semibold text-xs cursor-pointer shrink-0"
              >
                {busy ? '...' : 'Search'}
              </button>
              <button
                type="button"
                onClick={() => setSearchOpen(false)}
                className="p-2 text-slate-400 hover:text-white cursor-pointer shrink-0"
              >
                <X className="w-4 h-4" />
              </button>
            </form>

            <div className="flex items-center gap-2 px-3 py-1.5 rounded-xl bg-slate-900/70 border border-slate-800">
              <Home className="w-3.5 h-3.5 text-cyan-400 shrink-0" />
              <input
                type="text"
                value={nickname}
                onChange={(e) => setNickname(e.target.value)}
                placeholder='Optional pin nickname (e.g., "Home", "Parents", "Aruba")'
                className="w-full bg-transparent text-base sm:text-xs text-slate-200 placeholder-slate-500 focus:outline-none"
              />
            </div>

            {results.length > 0 && (
              <div className="divide-y divide-slate-800/80 rounded-xl border border-slate-800 bg-slate-900/60 max-h-64 overflow-y-auto">
                {results.map((r) => (
                  <div
                    key={`${r.name}-${r.lat}-${r.lon}`}
                    className="p-2.5 px-3 sm:px-3.5 flex flex-col sm:flex-row sm:items-center justify-between gap-2 text-xs"
                  >
                    <div className="flex items-start gap-2 min-w-0">
                      <MapPin className="w-3.5 h-3.5 text-cyan-400 mt-0.5 shrink-0" />
                      <div className="min-w-0">
                        <div className="font-medium text-white truncate flex items-center gap-1.5">
                          <span className="truncate">
                            {r.name}
                            {r.admin1 && !r.name.includes(r.admin1) ? `, ${r.admin1}` : ''}
                            {r.country && r.country !== 'US' ? ` (${r.country})` : ''}
                          </span>
                          {r.result_type === 'address' && (
                            <span className="px-1.5 py-0.2 rounded bg-cyan-500/20 text-cyan-300 text-[10px] font-semibold shrink-0">
                              Address
                            </span>
                          )}
                        </div>
                        <div className="text-[11px] text-slate-400 truncate">
                          {r.display_address && r.display_address !== r.name
                            ? `${r.display_address} • `
                            : ''}
                          {r.lat.toFixed(4)}°, {r.lon.toFixed(4)}°
                        </div>
                      </div>
                    </div>
                    <div className="flex items-center justify-end gap-2 shrink-0">
                      <button
                        type="button"
                        onClick={() => {
                          onSelectEphemeral(r.lat, r.lon, r.name);
                          setSearchOpen(false);
                        }}
                        className="px-2.5 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 cursor-pointer"
                      >
                        Instant Lookup
                      </button>
                      <button
                        type="button"
                        onClick={async () => {
                          const cleanNick = nickname.trim();
                          const pinned = await api.pinLocation({
                            name: r.name,
                            custom_name: cleanNick || undefined,
                            address_label: r.display_address || r.name,
                            admin1: r.admin1,
                            country: r.country,
                            lat: r.lat,
                            lon: r.lon,
                            elevation_m: r.elevation_m,
                            timezone: r.timezone,
                          });
                          setNickname('');
                          onPinnedNew(pinned.location);
                          setSearchOpen(false);
                        }}
                        className="px-2.5 py-1 rounded-lg bg-cyan-500 hover:bg-cyan-400 text-slate-950 font-semibold flex items-center gap-1 cursor-pointer"
                      >
                        <Cpu className="w-3 h-3" /> Pin & Train
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </header>
  );
};
