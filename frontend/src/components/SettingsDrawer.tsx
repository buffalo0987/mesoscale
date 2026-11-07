import React, { useEffect, useState } from 'react';
import { Bell, Car, CheckCircle2, Loader2, Radio, ShieldAlert, X } from 'lucide-react';
import { api } from '../api';
import type { NotificationPrefs, PwsConfig } from '../types';
import { urlBase64ToUint8Array } from '../units';

interface SettingsDrawerProps {
  open: boolean;
  vapidPublicKey: string;
  initialNotifPrefs: NotificationPrefs;
  initialPwsConfig: PwsConfig;
  onClose: () => void;
  onUpdated: () => void;
}

export const SettingsDrawer: React.FC<SettingsDrawerProps> = ({
  open,
  vapidPublicKey,
  initialNotifPrefs,
  initialPwsConfig,
  onClose,
  onUpdated,
}) => {
  const [prefs, setPrefs] = useState<NotificationPrefs>({
    hail_alerts: true,
    hail_min_size_in: 0.5,
    hail_lead_minutes: 45,
    hail_bypass_quiet_hours: true,
    ...initialNotifPrefs,
  });
  const [pws, setPws] = useState<PwsConfig>({
    ...initialPwsConfig,
    api_key: '',
    application_key: '',
  });
  const [pushStatus, setPushStatus] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setPrefs({
      hail_alerts: true,
      hail_min_size_in: 0.5,
      hail_lead_minutes: 45,
      hail_bypass_quiet_hours: true,
      ...initialNotifPrefs,
    });
  }, [initialNotifPrefs, open]);

  if (!open) return null;

  const handleEnableBrowserPush = async () => {
    setPushStatus(null);
    try {
      if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
        setPushStatus('Web Push requires HTTPS (e.g., Tailscale Serve) and an installed/modern browser.');
        return;
      }
      const perm = await Notification.requestPermission();
      if (perm !== 'granted') {
        setPushStatus('Notification permission was not granted.');
        return;
      }
      const reg = await navigator.serviceWorker.ready;
      const appKey = urlBase64ToUint8Array(vapidPublicKey);
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: appKey as unknown as BufferSource,
      });
      await api.subscribePush(sub.toJSON());
      setPushStatus('This device is now subscribed to Native VAPID Web Push!');
    } catch (err) {
      setPushStatus(err instanceof Error ? err.message : 'Push subscription failed');
    }
  };

  const handleTestPush = async () => {
    try {
      const res = await api.sendTestPush();
      setPushStatus(
        res.sent > 0
          ? `Test notification dispatched to ${res.sent} subscribed device(s).`
          : 'No active push subscriptions yet — tap "Subscribe This Device" first.'
      );
    } catch (err) {
      setPushStatus(err instanceof Error ? err.message : 'Test push failed');
    }
  };

  const handleSaveAll = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    try {
      await Promise.all([
        api.saveNotificationPrefs(prefs),
        api.savePwsConfig({
          ...pws,
          keep_existing_key: pws.api_key === '',
        }),
      ]);
      onUpdated();
      onClose();
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/65 backdrop-blur-sm">
      <div className="w-full max-w-lg bg-[#090f1d] border-l border-slate-800 h-full overflow-y-auto p-4 sm:p-6 pb-10 safe-top safe-bottom space-y-5 sm:space-y-6">
        <div className="flex items-center justify-between gap-2 pb-3 border-b border-slate-800">
          <div>
            <h2 className="text-base sm:text-lg font-bold text-white">Push Alerts & Station Settings</h2>
            <p className="text-[11px] sm:text-xs text-slate-400">
              Native VAPID Web Push (Tailscale HTTPS ready) & backyard PWS integration
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="p-2 rounded-lg bg-slate-900 text-slate-400 hover:text-white cursor-pointer shrink-0"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <form onSubmit={handleSaveAll} className="space-y-5 sm:space-y-6 text-xs">
          {/* Web Push Subscription Box */}
          <div className="p-3.5 sm:p-4 rounded-2xl bg-slate-900/80 border border-slate-800 space-y-3">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2.5">
              <span className="font-bold text-sm text-white flex items-center gap-2">
                <Bell className="w-4 h-4 text-cyan-400 shrink-0" />
                Native Mobile / Desktop Web Push
              </span>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={handleEnableBrowserPush}
                  className="flex-1 sm:flex-none px-3 py-1.5 rounded-lg bg-cyan-500 hover:bg-cyan-400 text-slate-950 font-semibold cursor-pointer text-center"
                >
                  Subscribe This Device
                </button>
                <button
                  type="button"
                  onClick={handleTestPush}
                  className="px-2.5 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 cursor-pointer shrink-0"
                >
                  Send Test
                </button>
              </div>
            </div>

            {pushStatus && (
              <div className="p-2.5 rounded-xl bg-cyan-500/10 border border-cyan-400/30 text-cyan-200 flex items-center gap-2">
                <CheckCircle2 className="w-4 h-4 shrink-0 text-cyan-400" />
                <span>{pushStatus}</span>
              </div>
            )}

            <div className="space-y-2 pt-2 border-t border-slate-800/80">
              <label className="flex items-center justify-between py-1">
                <span className="text-slate-200">NWS Severe & Tornado Warnings</span>
                <input
                  type="checkbox"
                  checked={prefs.nws_warnings}
                  onChange={(e) => setPrefs({ ...prefs, nws_warnings: e.target.checked })}
                  className="accent-cyan-400"
                />
              </label>
              <label className="flex items-center justify-between py-1">
                <span className="text-slate-200">NWS Watches & Winter Advisories</span>
                <input
                  type="checkbox"
                  checked={prefs.nws_watches}
                  onChange={(e) => setPrefs({ ...prefs, nws_watches: e.target.checked })}
                  className="accent-cyan-400"
                />
              </label>
              <label className="flex items-center justify-between py-1">
                <span className="text-slate-200">Frontal / Cap Bust Alerts (≥8°F NWP spread)</span>
                <input
                  type="checkbox"
                  checked={prefs.frontal_bust_risk}
                  onChange={(e) => setPrefs({ ...prefs, frontal_bust_risk: e.target.checked })}
                  className="accent-cyan-400"
                />
              </label>
              <label className="flex items-center justify-between py-1">
                <span className="text-slate-200">Wet-Bulb Freezing Rain / Glaze Alerts</span>
                <input
                  type="checkbox"
                  checked={prefs.freezing_rain_risk}
                  onChange={(e) => setPrefs({ ...prefs, freezing_rain_risk: e.target.checked })}
                  className="accent-cyan-400"
                />
              </label>
              <div className="pt-1">
                <div className="flex justify-between text-slate-300 mb-1">
                  <span>Smart Model-Shift Threshold</span>
                  <span className="font-mono text-cyan-300">≥ {prefs.model_temp_shift_f}°F shift</span>
                </div>
                <input
                  type="range"
                  min={5}
                  max={15}
                  step={1}
                  value={prefs.model_temp_shift_f}
                  onChange={(e) =>
                    setPrefs({ ...prefs, model_temp_shift_f: Number(e.target.value) })
                  }
                  className="w-full accent-cyan-400"
                />
              </div>
            </div>

            {/* Garage Hail Shield (2-Min NEXRAD + NWS Watchdog) */}
            <div className="pt-3 border-t border-slate-800/80 space-y-2.5">
              <label className="flex items-center justify-between">
                <span className="font-bold text-slate-100 flex items-center gap-1.5">
                  <Car className="w-4 h-4 text-fuchsia-400 shrink-0" />
                  Garage Hail Shield (2-Min NEXRAD Watchdog)
                </span>
                <input
                  type="checkbox"
                  checked={prefs.hail_alerts !== false}
                  onChange={(e) => setPrefs({ ...prefs, hail_alerts: e.target.checked })}
                  className="accent-fuchsia-400"
                />
              </label>
              <p className="text-[11px] text-slate-400 leading-relaxed">
                Polls live WSR-88D NEXRAD Level-III storm cell hail attributes + NWS warnings every 2 minutes and alerts you when a hail core enters your 18° driveway arrival cone.
              </p>

              {prefs.hail_alerts !== false && (
                <div className="p-3 rounded-xl bg-slate-950/75 border border-fuchsia-500/25 space-y-2.5">
                  <div>
                    <span className="text-[11px] text-slate-300 block mb-1">
                      Minimum Hail Size Trigger
                    </span>
                    <select
                      value={String(prefs.hail_min_size_in ?? 0.5)}
                      onChange={(e) =>
                        setPrefs({ ...prefs, hail_min_size_in: Number(e.target.value) })
                      }
                      className="w-full px-2.5 py-2 rounded-lg bg-slate-900 border border-slate-700 text-base sm:text-xs text-white"
                    >
                      <option value="0.25">0.25&quot; (Pea — Any Radar Hail)</option>
                      <option value="0.5">0.50&quot; (Marble — Recommended Truck Shield)</option>
                      <option value="0.75">0.75&quot; (Penny)</option>
                      <option value="1">1.00&quot; (Quarter — NWS Severe Criteria)</option>
                      <option value="1.75">1.75&quot; (Golf Ball — Major Damage)</option>
                    </select>
                  </div>

                  <div>
                    <div className="flex justify-between text-slate-300 mb-1">
                      <span>Driveway Arrival Lead Window</span>
                      <span className="font-mono text-fuchsia-300">
                        ≤ {prefs.hail_lead_minutes ?? 45} min ETA
                      </span>
                    </div>
                    <input
                      type="range"
                      min={20}
                      max={60}
                      step={5}
                      value={prefs.hail_lead_minutes ?? 45}
                      onChange={(e) =>
                        setPrefs({ ...prefs, hail_lead_minutes: Number(e.target.value) })
                      }
                      className="w-full accent-fuchsia-400"
                    />
                  </div>

                  <label className="flex items-center justify-between pt-0.5">
                    <span className="text-slate-200">
                      Bypass Quiet Hours for Approaching Hail
                    </span>
                    <input
                      type="checkbox"
                      checked={prefs.hail_bypass_quiet_hours !== false}
                      onChange={(e) =>
                        setPrefs({ ...prefs, hail_bypass_quiet_hours: e.target.checked })
                      }
                      className="accent-fuchsia-400"
                    />
                  </label>
                </div>
              )}
            </div>

            {/* Quiet Hours with Tornado Life-Safety Bypass */}
            <div className="pt-3 border-t border-slate-800/80 space-y-2">
              <label className="flex items-center justify-between">
                <span className="font-semibold text-slate-200">Quiet Hours (Suppress Routine Shifts)</span>
                <input
                  type="checkbox"
                  checked={prefs.quiet_hours_enabled}
                  onChange={(e) => setPrefs({ ...prefs, quiet_hours_enabled: e.target.checked })}
                  className="accent-cyan-400"
                />
              </label>
              {prefs.quiet_hours_enabled && (
                <div className="grid grid-cols-2 gap-2">
                  <div>
                    <span className="text-[11px] text-slate-400 block mb-1">Start</span>
                    <input
                      type="time"
                      value={prefs.quiet_start}
                      onChange={(e) => setPrefs({ ...prefs, quiet_start: e.target.value })}
                      className="w-full px-2.5 py-1.5 rounded-lg bg-slate-950 border border-slate-700 text-white"
                    />
                  </div>
                  <div>
                    <span className="text-[11px] text-slate-400 block mb-1">End</span>
                    <input
                      type="time"
                      value={prefs.quiet_end}
                      onChange={(e) => setPrefs({ ...prefs, quiet_end: e.target.value })}
                      className="w-full px-2.5 py-1.5 rounded-lg bg-slate-950 border border-slate-700 text-white"
                    />
                  </div>
                </div>
              )}
              <div className="p-2.5 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-200 text-[11px] flex items-start gap-2">
                <ShieldAlert className="w-4 h-4 text-rose-400 shrink-0 mt-0.5" />
                <span>
                  <b>Life-Safety &amp; Garage Override:</b> Tornado Warnings, Extreme Severe Warnings, and (when enabled) Garage Hail Shield alerts bypass Quiet Hours 24/7.
                </span>
              </div>
            </div>
          </div>

          {/* Personal Weather Station (PWS) Adapter */}
          <div className="p-4 rounded-2xl bg-slate-900/80 border border-slate-800 space-y-3">
            <div className="flex items-center justify-between">
              <span className="font-bold text-sm text-white flex items-center gap-2">
                <Radio className="w-4 h-4 text-emerald-400" />
                Personal Weather Station (Optional)
              </span>
              <input
                type="checkbox"
                checked={pws.enabled}
                onChange={(e) => setPws({ ...pws, enabled: e.target.checked })}
                className="accent-emerald-400"
              />
            </div>
            <p className="text-[11px] text-slate-400 leading-relaxed">
              Connect a backyard WeatherFlow Tempest or Ambient Weather station. Mesoscale automatically runs solar radiation-spike QC against nearby Oklahoma Mesonet / ASOS towers before blending.
            </p>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
              <div>
                <span className="text-[11px] text-slate-400 block mb-1">Provider</span>
                <select
                  value={pws.provider}
                  onChange={(e) =>
                    setPws({ ...pws, provider: e.target.value as PwsConfig['provider'] })
                  }
                  className="w-full px-2.5 py-2 rounded-lg bg-slate-950 border border-slate-700 text-base sm:text-xs text-white"
                >
                  <option value="none">Disabled</option>
                  <option value="tempest">WeatherFlow Tempest</option>
                  <option value="ambient">Ambient Weather</option>
                </select>
              </div>
              <div>
                <span className="text-[11px] text-slate-400 block mb-1">Station ID / MAC</span>
                <input
                  type="text"
                  value={pws.station_id}
                  onChange={(e) => setPws({ ...pws, station_id: e.target.value })}
                  placeholder="e.g., 98421"
                  className="w-full px-2.5 py-2 rounded-lg bg-slate-950 border border-slate-700 text-base sm:text-xs text-white"
                />
              </div>
            </div>

            <div>
              <span className="text-[11px] text-slate-400 block mb-1">
                API Token {pws.has_api_key ? '(Saved — leave blank to keep)' : ''}
              </span>
              <input
                type="password"
                value={pws.api_key || ''}
                onChange={(e) => setPws({ ...pws, api_key: e.target.value })}
                placeholder="Paste personal access token..."
                className="w-full px-2.5 py-2 rounded-lg bg-slate-950 border border-slate-700 text-base sm:text-xs text-white"
              />
            </div>
          </div>

          <button
            type="submit"
            disabled={saving}
            className="w-full py-3 rounded-xl bg-cyan-500 hover:bg-cyan-400 text-slate-950 font-bold text-sm flex items-center justify-center gap-2 cursor-pointer"
          >
            {saving && <Loader2 className="w-4 h-4 animate-spin" />}
            Save Settings
          </button>
        </form>
      </div>
    </div>
  );
};
