import { describe, expect, it } from 'vitest';
import {
  convertDeltaTemp,
  convertPrecip,
  convertTemp,
  convertWind,
  degToCompass,
  formatTemp,
  urlBase64ToUint8Array,
} from './units';

describe('Mesoscale unit conversions & helpers', () => {
  it('converts temperatures accurately between Celsius and Fahrenheit', () => {
    expect(convertTemp(0, 'imperial')).toBeCloseTo(32, 5);
    expect(convertTemp(20, 'imperial')).toBeCloseTo(68, 5);
    expect(convertTemp(20, 'metric')).toBeCloseTo(20, 5);
    expect(formatTemp(null, 'imperial')).toBe('—');
  });

  it('scales Kalman temperature deltas without adding +32 offset', () => {
    expect(convertDeltaTemp(-2.0, 'imperial')).toBeCloseTo(-3.6, 5);
    expect(convertDeltaTemp(-2.0, 'metric')).toBeCloseTo(-2.0, 5);
  });

  it('converts wind speed and precipitation cleanly', () => {
    expect(convertWind(10, 'imperial')).toBeCloseTo(22.3694, 3);
    expect(convertPrecip(25.4, 'imperial')).toBeCloseTo(1.0, 4);
    expect(degToCompass(0)).toBe('N');
    expect(degToCompass(180)).toBe('S');
    expect(degToCompass(225)).toBe('SW');
  });

  it('decodes VAPID base64url public keys into Uint8Array', () => {
    const arr = urlBase64ToUint8Array('SGVsbG8');
    expect(Array.from(arr)).toEqual([72, 101, 108, 108, 111]);
  });
});
