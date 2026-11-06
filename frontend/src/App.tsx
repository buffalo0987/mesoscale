import React, { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Award, BarChart3, Loader2, Radar } from 'lucide-react';
import { api } from './api';
import { DailyOutlookStrip } from './components/DailyOutlookStrip';
import { LocationBar } from './components/LocationBar';
import { NowcastHero } from './components/NowcastHero';
import { OnboardingView } from './components/OnboardingView';
import { RadarMesonetMap } from './components/RadarMesonetMap';
import { SettingsDrawer } from './components/SettingsDrawer';
import { StormWinterPanel } from './components/StormWinterPanel';
import { SynchronizedMeteogram } from './components/SynchronizedMeteogram';
import { VerificationScoreboard } from './components/VerificationScoreboard';
import type { PinnedLocation, UnitSystem } from './types';

type ActiveTab = 'forecast' | 'radar' | 'verification';

export const App: React.FC = () => {
  const queryClient = useQueryClient();

  const [units, setUnits] = useState<UnitSystem>(() => {
    const saved = localStorage.getItem('mesoscale_units');
    return saved === 'metric' ? 'metric' : 'imperial';
  });
  const [activeTab, setActiveTab] = useState<ActiveTab>('forecast');
  const [activeLocationId, setActiveLocationId] = useState<number | null>(null);
  const [ephemeralTarget, setEphemeralTarget] = useState<{
    lat: number;
    lon: number;
    name?: string;
  } | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [manualRefreshing, setManualRefreshing] = useState(false);

  const bootstrapQuery = useQuery({
    queryKey: ['bootstrap'],
    queryFn: () => api.getBootstrap(),
  });

  const pinnedLocations = bootstrapQuery.data?.pinned_locations ?? [];

  // Select first pinned location automatically if none selected and not in ephemeral mode
  useEffect(() => {
    if (activeLocationId === null && ephemeralTarget === null && pinnedLocations.length > 0) {
      setActiveLocationId(pinnedLocations[0].id);
    }
  }, [pinnedLocations, activeLocationId, ephemeralTarget]);

  const activePinnedMeta =
    pinnedLocations.find((l) => l.id === activeLocationId) ?? null;

  const isBackfilling =
    activePinnedMeta !== null &&
    (activePinnedMeta.backfill_status === 'running' ||
      activePinnedMeta.backfill_status === 'pending');

  // Poll Day-1 365-day historical backfill & CatBoost/LightGBM training status every 2.5s
  const statusPollQuery = useQuery({
    queryKey: ['location-status', activeLocationId],
    queryFn: () => api.getLocationStatus(activeLocationId!),
    enabled: activeLocationId !== null && ephemeralTarget === null && isBackfilling,
    refetchInterval: 2500,
  });

  useEffect(() => {
    const st = statusPollQuery.data;
    if (!st) return;
    if (st.backfill_status === 'ready' || st.backfill_status === 'error') {
      queryClient.invalidateQueries({ queryKey: ['bootstrap'] });
      queryClient.invalidateQueries({ queryKey: ['pinned-forecast', activeLocationId] });
      queryClient.invalidateQueries({ queryKey: ['verification', activeLocationId] });
    }
  }, [statusPollQuery.data, activeLocationId, queryClient]);

  const pinnedForecastQuery = useQuery({
    queryKey: ['pinned-forecast', activeLocationId],
    queryFn: () => api.getPinnedForecast(activeLocationId!),
    enabled: activeLocationId !== null && ephemeralTarget === null,
    refetchInterval: isBackfilling ? 4000 : 5 * 60 * 1000,
  });

  const ephemeralForecastQuery = useQuery({
    queryKey: [
      'ephemeral-forecast',
      ephemeralTarget?.lat.toFixed(4),
      ephemeralTarget?.lon.toFixed(4),
    ],
    queryFn: () =>
      api.getEphemeralForecast(
        ephemeralTarget!.lat,
        ephemeralTarget!.lon,
        ephemeralTarget!.name
      ),
    enabled: ephemeralTarget !== null,
  });

  const toggleUnits = () => {
    const next: UnitSystem = units === 'imperial' ? 'metric' : 'imperial';
    setUnits(next);
    localStorage.setItem('mesoscale_units', next);
  };

  const handlePinnedNew = async (loc: PinnedLocation) => {
    setEphemeralTarget(null);
    setActiveLocationId(loc.id);
    await queryClient.invalidateQueries({ queryKey: ['bootstrap'] });
  };

  const handleDeletePinned = async (id: number) => {
    await api.deleteLocation(id);
    const remaining = pinnedLocations.filter((l) => l.id !== id);
    setActiveLocationId(remaining[0]?.id ?? null);
    await queryClient.invalidateQueries({ queryKey: ['bootstrap'] });
  };

  const handleManualRefresh = async () => {
    setManualRefreshing(true);
    try {
      if (ephemeralTarget) {
        await ephemeralForecastQuery.refetch();
      } else if (activeLocationId !== null) {
        await api.refreshLocation(activeLocationId);
        await pinnedForecastQuery.refetch();
      }
    } finally {
      setManualRefreshing(false);
    }
  };

  if (bootstrapQuery.isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center text-slate-400 text-sm gap-2.5">
        <Loader2 className="w-5 h-5 animate-spin text-cyan-400" />
        Initializing Mesoscale Engine...
      </div>
    );
  }

  // Clean First-Boot Onboarding Experience (zero pre-seeded cities)
  if (pinnedLocations.length === 0 && ephemeralTarget === null) {
    return (
      <OnboardingView
        onPinned={handlePinnedNew}
        onEphemeralSelect={(lat, lon, name) => setEphemeralTarget({ lat, lon, name })}
      />
    );
  }

  const activeBundle = ephemeralTarget
    ? ephemeralForecastQuery.data
    : pinnedForecastQuery.data;

  // Merge live status poll progress if backfill is actively running
  if (activeBundle && statusPollQuery.data && !ephemeralTarget) {
    activeBundle.engine_info = {
      ...activeBundle.engine_info,
      backfill_status: statusPollQuery.data.backfill_status,
      backfill_progress: statusPollQuery.data.backfill_progress,
      backfill_message: statusPollQuery.data.backfill_message,
      model_status: statusPollQuery.data.model_status,
    };
  }

  const isLoadingBundle = ephemeralTarget
    ? ephemeralForecastQuery.isLoading
    : pinnedForecastQuery.isLoading;

  const bundleError = ephemeralTarget
    ? ephemeralForecastQuery.error
    : pinnedForecastQuery.error;

  return (
    <div className="min-h-screen flex flex-col bg-[radial-gradient(ellipse_at_top,_rgba(14,116,144,0.16),_transparent_55%)]">
      <LocationBar
        pinnedLocations={pinnedLocations}
        activeLocationId={activeLocationId}
        ephemeralLabel={
          ephemeralTarget
            ? activeBundle?.location.name || ephemeralTarget.name || 'GPS Lookup'
            : null
        }
        units={units}
        onSelectPinned={(id) => {
          setEphemeralTarget(null);
          setActiveLocationId(id);
        }}
        onSelectEphemeral={(lat, lon, name) => setEphemeralTarget({ lat, lon, name })}
        onPinnedNew={handlePinnedNew}
        onDeletePinned={handleDeletePinned}
        onToggleUnits={toggleUnits}
        onOpenSettings={() => setSettingsOpen(true)}
      />

      <main className="max-w-7xl w-full mx-auto px-3 sm:px-6 py-5 space-y-5 flex-1">
        {isLoadingBundle ? (
          <div className="glass-panel rounded-2xl p-16 flex flex-col items-center justify-center gap-3 text-slate-300 text-sm">
            <Loader2 className="w-6 h-6 animate-spin text-cyan-400" />
            <span>
              {ephemeralTarget
                ? 'Querying live 6-model ensemble + nearby 5-min Mesonet/ASOS towers...'
                : 'Loading calibrated hyper-local forecast...'}
            </span>
          </div>
        ) : bundleError ? (
          <div className="p-6 rounded-2xl bg-rose-950/50 border border-rose-500/40 text-rose-200 text-sm">
            Forecast fetch error:{' '}
            {bundleError instanceof Error ? bundleError.message : 'Unknown error'}
          </div>
        ) : activeBundle ? (
          <>
            <NowcastHero
              bundle={activeBundle}
              units={units}
              onRefresh={handleManualRefresh}
              onPinnedFromEphemeral={handlePinnedNew}
              isRefreshing={manualRefreshing}
            />

            {/* Primary View Switcher */}
            <div className="flex items-center gap-2 overflow-x-auto pb-1">
              <button
                type="button"
                onClick={() => setActiveTab('forecast')}
                className={`px-4 py-2 rounded-xl text-xs font-semibold flex items-center gap-2 border transition cursor-pointer shrink-0 ${
                  activeTab === 'forecast'
                    ? 'bg-cyan-500 text-slate-950 border-cyan-400'
                    : 'bg-slate-900/80 text-slate-300 border-slate-800 hover:bg-slate-800'
                }`}
              >
                <BarChart3 className="w-4 h-4" />
                Forecast & Meteogram
              </button>
              <button
                type="button"
                onClick={() => setActiveTab('radar')}
                className={`px-4 py-2 rounded-xl text-xs font-semibold flex items-center gap-2 border transition cursor-pointer shrink-0 ${
                  activeTab === 'radar'
                    ? 'bg-cyan-500 text-slate-950 border-cyan-400'
                    : 'bg-slate-900/80 text-slate-300 border-slate-800 hover:bg-slate-800'
                }`}
              >
                <Radar className="w-4 h-4" />
                Live Radar & Mesonet Map
              </button>
              <button
                type="button"
                onClick={() => setActiveTab('verification')}
                className={`px-4 py-2 rounded-xl text-xs font-semibold flex items-center gap-2 border transition cursor-pointer shrink-0 ${
                  activeTab === 'verification'
                    ? 'bg-cyan-500 text-slate-950 border-cyan-400'
                    : 'bg-slate-900/80 text-slate-300 border-slate-800 hover:bg-slate-800'
                }`}
              >
                <Award className="w-4 h-4" />
                Accuracy Scoreboard
              </button>
            </div>

            {activeTab === 'forecast' && (
              <div className="space-y-5">
                <StormWinterPanel bundle={activeBundle} units={units} />
                <SynchronizedMeteogram hourly={activeBundle.hourly} units={units} />
                <DailyOutlookStrip daily={activeBundle.daily} units={units} />
              </div>
            )}

            {activeTab === 'radar' && (
              <RadarMesonetMap
                lat={activeBundle.location.lat}
                lon={activeBundle.location.lon}
                locationName={activeBundle.location.name}
                units={units}
              />
            )}

            {activeTab === 'verification' && (
              <VerificationScoreboard
                locationId={ephemeralTarget ? null : activeBundle.location.id}
                locationName={activeBundle.location.name}
                units={units}
                onRetrained={() => {
                  queryClient.invalidateQueries({
                    queryKey: ['pinned-forecast', activeLocationId],
                  });
                }}
              />
            )}
          </>
        ) : null}
      </main>

      {bootstrapQuery.data && (
        <SettingsDrawer
          open={settingsOpen}
          vapidPublicKey={bootstrapQuery.data.vapid_public_key}
          initialNotifPrefs={bootstrapQuery.data.notification_prefs}
          initialPwsConfig={bootstrapQuery.data.pws_config}
          onClose={() => setSettingsOpen(false)}
          onUpdated={() => queryClient.invalidateQueries({ queryKey: ['bootstrap'] })}
        />
      )}
    </div>
  );
};
export default App;
