import type { UnitSystem } from './types';

export function convertTemp(tempC: number | null | undefined, units: UnitSystem): number | null {
  if (tempC === null || tempC === undefined || Number.isNaN(tempC)) return null;
  return units === 'imperial' ? tempC * 1.8 + 32 : tempC;
}

export function formatTemp(tempC: number | null | undefined, units: UnitSystem, decimals = 0): string {
  const val = convertTemp(tempC, units);
  if (val === null) return '—';
  return `${val.toFixed(decimals)}°${units === 'imperial' ? 'F' : 'C'}`;
}

export function convertDeltaTemp(deltaC: number, units: UnitSystem): number {
  return units === 'imperial' ? deltaC * 1.8 : deltaC;
}

export function convertWind(windMs: number | null | undefined, units: UnitSystem): number | null {
  if (windMs === null || windMs === undefined || Number.isNaN(windMs)) return null;
  return units === 'imperial' ? windMs * 2.23694 : windMs;
}

export function formatWind(windMs: number | null | undefined, units: UnitSystem): string {
  const val = convertWind(windMs, units);
  if (val === null) return '—';
  return `${Math.round(val)} ${units === 'imperial' ? 'mph' : 'm/s'}`;
}

export function convertPrecip(mm: number | null | undefined, units: UnitSystem): number {
  if (mm === null || mm === undefined || Number.isNaN(mm)) return 0;
  return units === 'imperial' ? mm / 25.4 : mm;
}

export function formatPrecip(mm: number | null | undefined, units: UnitSystem): string {
  const val = convertPrecip(mm, units);
  return units === 'imperial' ? `${val.toFixed(2)}"` : `${val.toFixed(1)} mm`;
}

export function degToCompass(deg: number | null | undefined): string {
  if (deg === null || deg === undefined || Number.isNaN(deg)) return '';
  const dirs = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
  return dirs[Math.round(((deg % 360) + 360) % 360 / 22.5) % 16];
}

export function urlBase64ToUint8Array(base64String: string): Uint8Array {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const rawData = window.atob(base64);
  const outputArray = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; ++i) {
    outputArray[i] = rawData.charCodeAt(i);
  }
  return outputArray;
}
