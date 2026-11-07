import type {
  BootstrapPayload,
  ForecastBundle,
  GeoSearchItem,
  MapOverlayPayload,
  NotificationPrefs,
  PinnedLocation,
  PwsConfig,
  VerificationReport,
} from './types';

async function jsonFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers || {}),
    },
  });
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(text || `HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  getBootstrap: () => jsonFetch<BootstrapPayload>('/api/bootstrap'),

  searchLocations: (q: string) =>
    jsonFetch<{ results: GeoSearchItem[] }>(`/api/locations/search?q=${encodeURIComponent(q)}`),

  reverseGeocode: (lat: number, lon: number) =>
    jsonFetch<{ result: GeoSearchItem }>(`/api/locations/reverse?lat=${lat}&lon=${lon}`),

  getEphemeralForecast: (lat: number, lon: number, name?: string) => {
    const params = new URLSearchParams({ lat: String(lat), lon: String(lon) });
    if (name) params.set('name', name);
    return jsonFetch<ForecastBundle>(`/api/ephemeral/forecast?${params.toString()}`);
  },

  pinLocation: (payload: {
    name?: string;
    custom_name?: string;
    address_label?: string;
    admin1?: string;
    country?: string;
    lat: number;
    lon: number;
    elevation_m?: number;
    timezone?: string;
    alerts_enabled?: boolean;
  }) =>
    jsonFetch<{ location: PinnedLocation }>('/api/locations/pin', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  updateLocation: (
    id: number,
    payload: { name?: string; address_label?: string; alerts_enabled?: boolean }
  ) =>
    jsonFetch<{ location: PinnedLocation }>(`/api/locations/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),

  getLocationStatus: (id: number) =>
    jsonFetch<{
      id: number;
      backfill_status: string;
      backfill_progress: number;
      backfill_message: string;
      model_status: string;
    }>(`/api/locations/${id}/status`),

  getPinnedForecast: (id: number) => jsonFetch<ForecastBundle>(`/api/locations/${id}/forecast`),

  refreshLocation: (id: number) =>
    jsonFetch<{ ok: boolean }>(`/api/locations/${id}/refresh`, { method: 'POST' }),

  retrainLocation: (id: number) =>
    jsonFetch<{ ok: boolean; report: Record<string, unknown> }>(`/api/locations/${id}/retrain`, {
      method: 'POST',
    }),

  deleteLocation: (id: number) =>
    jsonFetch<{ deleted: boolean }>(`/api/locations/${id}`, { method: 'DELETE' }),

  submitCheckin: (
    id: number,
    condition: 'dry' | 'rain' | 'tstorm' | 'freezing_rain' | 'sleet' | 'snow'
  ) =>
    jsonFetch<{ checkin: Record<string, unknown> }>(`/api/locations/${id}/checkin`, {
      method: 'POST',
      body: JSON.stringify({ condition }),
    }),

  getVerification: (id: number, days = 30, leadBucket = 'all') =>
    jsonFetch<VerificationReport>(
      `/api/locations/${id}/verification?days=${days}&lead_bucket=${encodeURIComponent(leadBucket)}`
    ),

  getMapOverlay: (lat: number, lon: number) =>
    jsonFetch<MapOverlayPayload>(`/api/map-overlay?lat=${lat}&lon=${lon}`),

  subscribePush: (sub: PushSubscriptionJSON) =>
    jsonFetch<{ subscribed: boolean }>('/api/push/subscribe', {
      method: 'POST',
      body: JSON.stringify({
        endpoint: sub.endpoint,
        keys: sub.keys,
        user_agent: navigator.userAgent,
      }),
    }),

  sendTestPush: () => jsonFetch<{ ok: boolean; sent: number }>('/api/push/test', { method: 'POST' }),

  saveNotificationPrefs: (prefs: Partial<NotificationPrefs>) =>
    jsonFetch<{ notification_prefs: NotificationPrefs }>('/api/settings/notifications', {
      method: 'PUT',
      body: JSON.stringify(prefs),
    }),

  savePwsConfig: (cfg: Partial<PwsConfig> & { keep_existing_key?: boolean }) =>
    jsonFetch<{ pws_config: PwsConfig }>('/api/settings/pws', {
      method: 'PUT',
      body: JSON.stringify(cfg),
    }),
};
