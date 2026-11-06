import React, { useState } from 'react';
import { Bell, CheckCircle2, Loader2, Radio, ShieldAlert, X } from 'lucide-react';
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
  const [prefs, setPrefs] = useState<NotificationPrefs>(initialNotifPrefs);
  const [pws, setPws] = useState<PwsConfig>({
    ...initialPwsConfig,
    api_key: '',
    application_key: '',
  });
  const [pushStatus, setPushStatus] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

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
      <div className="w-full max-w-lg bg-[#090f1d] border-l border-slate-800 h-full overflow-y-auto p-6 space-y-6">
        <div className="flex items-center justify-between pb-3 border-b border-slate-800">
          <div>
            <h2 className="text-lg font-bold text-white">Push Alerts & Station Settings</h2>
            <p className="text-xs text-slate-400">
              Native VAPID Web Push (Tailscale HTTPS ready) & backyard PWS integration
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="p-2 rounded-lg bg-slate-900 text-slate-400 hover:text-white cursor-pointer"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <form onSubmit={handleSaveAll} className="space-y-6 text-xs">
          {/* Web Push Subscription Box */}
          <div className="p-4 rounded-2xl bg-slate-900/80 border border-slate-800 space-y-3">
            <div className="flex items-center justify-between">
              <span className="font-bold text-sm text-white flex items-center gap-2">
                <Bell className="w-4 h-4 text-cyan-400" />
                Native Mobile / Desktop Web Push
              </span>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={handleEnableBrowserPush}
                  className="px-3 py-1.5 rounded-lg bg-cyan-500 hover:bg-cyan-400 text-slate-950 font-semibold cursor-pointer"
                >
                  Subscribe This Device
                </button>
                <button
                  type="button"
                  onClick={handleTestPush}
                  className="px-2.5 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 cursor-pointer"
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
                  <b>Life-Safety Override:</b> Tornado Warnings & Extreme Severe Warnings automatically bypass Quiet Hours 24/7.
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

            <div className="grid grid-cols-2 gap-2.5">
              <div>
                <span className="text-[11px] text-slate-400 block mb-1">Provider</span>
                <select
                  value={pws.provider}
                  onChange={(e) =>
                    setPws({ ...pws, provider: e.target.value as PwsConfig['provider'] })
                  }
                  className="w-full px-2.5 py-2 rounded-lg bg-slate-950 border border-slate-700 text-white"
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
                  className="w-full px-2.5 py-2 rounded-lg bg-slate-950 border border-slate-700 text-white"
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
                className="w-full px-2.5 py-2 rounded-lg bg-slate-950 border border-slate-700 text-white"
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
