export type UnitSystem = 'imperial' | 'metric';

export interface StationMeta {
  station_id: string;
  name: string;
  source: 'ok_mesonet' | 'nws_asos' | 'pws' | string;
  lat: number;
  lon: number;
  elevation_m: number;
  distance_km: number;
  blend_weight: number;
}

export interface PinnedLocation {
  id: number;
  name: string;
  admin1: string;
  country: string;
  lat: number;
  lon: number;
  elevation_m: number;
  timezone: string;
  stations: StationMeta[];
  backfill_status: 'pending' | 'running' | 'ready' | 'error' | 'not_pinned';
  backfill_progress: number;
  backfill_message: string;
  model_status: 'untrained' | 'training' | 'trained' | 'fallback' | 'ephemeral_consensus';
  created_at_utc?: string;
}

export interface GeoSearchItem {
  name: string;
  admin1: string;
  country: string;
  lat: number;
  lon: number;
  elevation_m: number;
  timezone: string;
}

export interface NotificationPrefs {
  enabled: boolean;
  nws_warnings: boolean;
  nws_watches: boolean;
  model_temp_shift_f: number;
  frontal_bust_risk: boolean;
  freezing_rain_risk: boolean;
  quiet_hours_enabled: boolean;
  quiet_start: string;
  quiet_end: string;
}

export interface PwsConfig {
  enabled: boolean;
  provider: 'none' | 'tempest' | 'ambient';
  station_id: string;
  has_api_key?: boolean;
  api_key?: string;
  application_key?: string;
}

export interface BootstrapPayload {
  app_name: string;
  pinned_locations: PinnedLocation[];
  vapid_public_key: string;
  notification_prefs: NotificationPrefs;
  pws_config: PwsConfig;
}

export interface CurrentObservation {
  valid_time_utc: string;
  age_minutes: number;
  temp_c: number;
  dewpoint_c: number;
  wet_bulb_c: number;
  feels_like_c: number;
  wind_ms: number | null;
  gust_ms: number | null;
  wind_dir_deg?: number | null;
  precip_1h_mm?: number | null;
  pressure_hpa?: number | null;
}

export interface SubhourlyPoint {
  valid_time_utc: string;
  temp_c: number | null;
  precip_mm: number;
  ring_precip_max_mm: number;
  gust_ms: number | null;
}

export interface HourlyForecast {
  valid_time_utc: string;
  lead_hours: number;
  temp_p05_c: number;
  temp_p10_c: number;
  temp_p25_c: number;
  temp_p50_c: number;
  temp_p75_c: number;
  temp_p90_c: number;
  temp_p95_c: number;
  feels_like_c: number;
  dewpoint_p50_c: number;
  wet_bulb_c: number;
  theta_e_k: number;
  wind_p50_ms: number;
  gust_p50_ms: number;
  gust_p90_ms: number;
  wind_dir_deg: number;
  pressure_hpa: number;
  cloud_pct: number;
  precip_prob: number;
  precip_p50_mm: number;
  precip_p90_mm: number;
  ring_precip_max_mm: number;
  precip_type: 'none' | 'rain' | 'freezing_rain' | 'sleet' | 'snow';
  cape_jkg: number;
  cin_jkg: number;
  freezing_level_m: number | null;
  hrrr_3run_temp_trend_c: number;
  ecmwf_2run_temp_trend_c: number;
  raw_models_temp_c: Record<string, number | null>;
  bust_risk_level: 'none' | 'moderate' | 'high';
  bust_summary: string;
  convective_proximity: boolean;
  freezing_rain_risk: boolean;
  cap_status: 'none' | 'strong_cap' | 'breakable_cap' | 'uncapped_initiation';
}

export interface DailyForecast {
  date: string;
  temp_max_c: number;
  temp_min_c: number;
  temp_max_p90_c: number;
  temp_min_p10_c: number;
  precip_prob_max: number;
  precip_total_mm: number;
  dominant_precip_type: string;
  max_gust_ms: number;
  cloud_mean_pct: number;
  has_high_bust_risk: boolean;
}

export interface NwsAlertItem {
  id: string;
  event: string;
  severity: string;
  certainty: string;
  urgency: string;
  headline: string;
  description: string;
  instruction: string;
  onset_utc: string | null;
  expires_utc: string | null;
  is_life_threatening: boolean;
}

export interface SpcRiskDay {
  code: string;
  rank: number;
  label: string;
  color: string;
}

export interface StormWinterSummary {
  auto_active: boolean;
  spc: {
    day1: SpcRiskDay;
    day2: SpcRiskDay;
    day3: SpcRiskDay;
    has_severe_risk: boolean;
    has_thunder_risk: boolean;
  };
  peak_cape_jkg: number;
  cin_at_peak_cape_jkg: number;
  winter_precip_hours_48h: number;
  freezing_rain_hours_48h: number;
  high_bust_hours_48h: number;
}

export interface ForecastBundle {
  mode: 'pinned_ai' | 'pinned_consensus' | 'ephemeral_consensus';
  location: Omit<PinnedLocation, 'id'> & { id: number | null };
  engine_info: {
    model_status: string;
    backfill_status: string;
    backfill_progress: number;
    backfill_message: string;
    model_version?: number;
    train_rows?: number;
    kalman_bias_c: number;
    kalman_trend_c_hr: number;
    hedge_weights?: Record<string, number>;
  };
  current_observation: CurrentObservation | null;
  nowcast_15m: SubhourlyPoint[];
  hourly: HourlyForecast[];
  daily: DailyForecast[];
  alerts: NwsAlertItem[];
  storm_winter: StormWinterSummary;
  recent_checkins: Array<{
    id: number;
    condition: string;
    reported_at_utc: string;
  }>;
}

export interface LeaderboardRow {
  model_id: string;
  label: string;
  samples: number;
  temp_mae_c: number;
  temp_mae_f: number;
  temp_rmse_f: number;
  temp_bias_f: number;
  wind_mae_mph: number | null;
  precip_brier: number | null;
  brier_skill_vs_nbm: number | null;
  coverage_80pct: number | null;
  verification_source: string;
}

export interface VerificationReport {
  lead_bucket: string;
  leaderboard: LeaderboardRow[];
  replay_72h: Array<Record<string, number | string | null>>;
  feature_importances: Array<{ feature: string; importance: number }>;
  holdout_summary: Record<string, unknown>;
}

export interface GeoJsonFeature {
  type: 'Feature';
  properties: Record<string, unknown>;
  geometry: {
    type: string;
    coordinates: unknown;
  } | null;
}

export interface GeoJsonFeatureCollection {
  type: 'FeatureCollection';
  features: GeoJsonFeature[];
}

export interface MapOverlayPayload {
  stations_geojson: GeoJsonFeatureCollection;
  alerts_geojson: GeoJsonFeatureCollection;
  spc_day1_geojson: GeoJsonFeatureCollection;
  spc_day2_geojson: GeoJsonFeatureCollection;
  ring_geojson: GeoJsonFeatureCollection;
  radar_frames: Array<{ offset_minutes: number; label: string; tile_url: string }>;
  mrms_tile_url: string;
}
