#!/usr/bin/env python3
"""Verify archived DryWetCast forecasts against provisional IMD rainfall."""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import tempfile
from urllib.request import Request, urlopen

import imdlib as imd
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection
from matplotlib.colors import BoundaryNorm, ListedColormap
import numpy as np

from update_dashboard import (
    CONFIGS, DATE_PATTERN, DRY_COLORS, FOLDER_MIME, PROBABILITY_BOUNDS,
    SSL_CONTEXT, USER_AGENT, WET_COLORS, boundary_lines, download_file,
    drive_children,
)


DEFAULT_START = '20261015'
THRESHOLD = 0.50
MODEL_COMMIT = '22cdbe5beccd3a776ce9e6039a1f603e3d404604'
MODEL_BASE = f'https://raw.githubusercontent.com/pdasiitism/DryWetCast-India/{MODEL_COMMIT}/models'
TARGET_LATS = np.arange(6.5, 38.51, 0.25)
TARGET_LONS = np.arange(66.5, 100.01, 0.25)
TARGET_SHAPE = (len(TARGET_LATS), len(TARGET_LONS))
OUTCOME_COLORS = ['#E8EEED', '#2B8C6B', '#E6A63C', '#C9473B']
OUTCOME_BOUNDS = [-0.5, 0.5, 1.5, 2.5, 3.5]
BSS_COLORS = ['#8E2344', '#CE5866', '#F2B5A7', '#F4F1E8', '#AAD7CB', '#45A68E']
BSS_BOUNDS = [-1.00001, -0.5, -0.2, 0, 0.2, 0.5, 1.00001]


def _download_url(url: str, destination: Path) -> None:
    request = Request(url, headers={'User-Agent': USER_AGENT})
    with urlopen(request, timeout=180, context=SSL_CONTEXT) as response, destination.open('wb') as handle:
        shutil.copyfileobj(response, handle)


def _model_file(path: Path | None, name: str, work_dir: Path) -> Path:
    if path is not None:
        if not path.exists():
            raise FileNotFoundError(path)
        return path
    destination = work_dir / name
    _download_url(f'{MODEL_BASE}/{name}', destination)
    return destination


def discover_date_folders(parent_id: str, start: str) -> dict[tuple[str, str], dict]:
    """Find date folders directly under the parent and one level of archive folders."""
    direct = drive_children(parent_id)
    candidates = list(direct)
    for item in direct:
        if item.get('mimeType') != FOLDER_MIME or DATE_PATTERN.fullmatch(item.get('name', '')):
            continue
        if 'archive' in item.get('name', '').lower():
            candidates.extend(drive_children(item['id']))

    found = {}
    for item in candidates:
        match = DATE_PATTERN.fullmatch(item.get('name', ''))
        if item.get('mimeType') != FOLDER_MIME or not match:
            continue
        forecast_date = match.group(1)
        cycle = match.group(2) or '00'
        if forecast_date >= start and cycle == '00':
            found[(forecast_date, cycle)] = item
    return found


def forecast_files(folder: dict) -> dict[str, dict] | None:
    children = drive_children(folder['id'])
    subfolders = {item['name'].lower(): item for item in children if item.get('mimeType') == FOLDER_MIME}
    selected = {}
    for config in CONFIGS:
        subfolder = subfolders.get(config)
        if subfolder is None:
            return None
        files = [item for item in drive_children(subfolder['id'])
                 if item.get('name', '').lower().endswith('.npz')]
        if len(files) != 1:
            return None
        selected[config] = files[0]
    return selected


def latest_imd_date(as_of: date, cache_dir: Path, lookback: int = 7) -> date:
    for offset in range(lookback + 1):
        candidate = as_of - timedelta(days=offset)
        value = candidate.isoformat()
        try:
            imd.load('rain', value, value, source='realtime', cache_dir=str(cache_dir), progress=False)
            return candidate
        except imd.DataNotAvailableError:
            continue
    raise RuntimeError(f'No IMD real-time rainfall was available in the {lookback + 1}-day lookback')


def _sigmoid(values: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-values))


def _safe_ratio(numerator: float, denominator: float) -> float | None:
    return None if denominator == 0 else float(numerator / denominator)


def score(probability: np.ndarray, observed: np.ndarray, climatology: np.ndarray,
          valid_observation: np.ndarray, land: np.ndarray) -> tuple[dict, dict]:
    use = land & valid_observation & np.isfinite(probability) & np.isfinite(climatology)
    if not np.any(use):
        raise ValueError('No common valid land cells are available for verification')
    p = probability[use].astype(float)
    y = observed[use].astype(bool)
    c = climatology[use].astype(float)
    model_se = (p - y.astype(float)) ** 2
    climatology_se = (c - y.astype(float)) ** 2

    forecast_event = p >= THRESHOLD
    hits = int(np.count_nonzero(forecast_event & y))
    misses = int(np.count_nonzero(~forecast_event & y))
    false_alarms = int(np.count_nonzero(forecast_event & ~y))
    correct_negatives = int(np.count_nonzero(~forecast_event & ~y))
    model_sum = float(model_se.sum())
    climatology_sum = float(climatology_se.sum())
    metrics = {
        'bss': None if climatology_sum == 0 else float(1.0 - model_sum / climatology_sum),
        'pod': _safe_ratio(hits, hits + misses),
        'far': _safe_ratio(false_alarms, hits + false_alarms),
        'hits': hits,
        'misses': misses,
        'falseAlarms': false_alarms,
        'correctNegatives': correct_negatives,
        'observedFraction': float(y.mean()),
        'forecastFraction': float(forecast_event.mean()),
        'nCells': int(use.sum()),
    }
    components = {
        'modelSquaredError': model_sum,
        'climatologySquaredError': climatology_sum,
        'hits': hits,
        'misses': misses,
        'falseAlarms': false_alarms,
        'correctNegatives': correct_negatives,
        'nCells': int(use.sum()),
    }
    return metrics, components


def pooled_metrics(components: dict) -> dict | None:
    if components['nCells'] == 0:
        return None
    hits = components['hits']
    misses = components['misses']
    false_alarms = components['falseAlarms']
    correct_negatives = components['correctNegatives']
    clim = components['climatologySquaredError']
    return {
        'bss': None if clim == 0 else float(1.0 - components['modelSquaredError'] / clim),
        'pod': _safe_ratio(hits, hits + misses),
        'far': _safe_ratio(false_alarms, hits + false_alarms),
        'hits': hits,
        'misses': misses,
        'falseAlarms': false_alarms,
        'correctNegatives': correct_negatives,
        'nCells': components['nCells'],
    }


def _empty_components() -> dict:
    return {
        'modelSquaredError': 0.0, 'climatologySquaredError': 0.0,
        'hits': 0, 'misses': 0, 'falseAlarms': 0, 'correctNegatives': 0, 'nCells': 0,
    }


def _add_components(total: dict, value: dict) -> None:
    for key in total:
        total[key] += value[key]


def _empty_map_components() -> dict:
    return {
        'model': np.zeros(TARGET_SHAPE, dtype=np.float64),
        'climatology': np.zeros(TARGET_SHAPE, dtype=np.float64),
        'count': np.zeros(TARGET_SHAPE, dtype=np.uint16),
    }


def _add_map_components(total: dict, probability: np.ndarray, observed: np.ndarray,
                        climatology: np.ndarray, valid_observation: np.ndarray,
                        land: np.ndarray) -> None:
    use = land & valid_observation & np.isfinite(probability) & np.isfinite(climatology)
    target = observed.astype(float)
    total['model'][use] += (probability[use] - target[use]) ** 2
    total['climatology'][use] += (climatology[use] - target[use]) ** 2
    total['count'][use] += 1


def _outcome_field(probability: np.ndarray, observed: np.ndarray,
                   valid_observation: np.ndarray, land: np.ndarray) -> np.ndarray:
    """0 correct negative, 1 hit, 2 miss, 3 false alarm; NaN outside verification cells."""
    result = np.full(TARGET_SHAPE, np.nan, dtype=np.float32)
    use = land & valid_observation & np.isfinite(probability)
    forecast = probability >= THRESHOLD
    truth = observed.astype(bool)
    result[use & ~forecast & ~truth] = 0
    result[use & forecast & truth] = 1
    result[use & ~forecast & truth] = 2
    result[use & forecast & ~truth] = 3
    return result


def _map_path(initialization: str, config: str, event: str, kind: str,
              product: str) -> str:
    return f'assets/verification/{initialization}-{config}-{event}-{kind}-{product}.png'


def _draw_map(field: np.ndarray, segments: list[np.ndarray], destination: Path,
              colors: list[str], bounds: list[float], overwrite: bool = False) -> None:
    """Render a compact verification map; optimized for the many daily panels."""
    if destination.exists() and not overwrite:
        return
    cmap = ListedColormap(colors)
    cmap.set_bad('#FFFFFF')
    fig, ax = plt.subplots(figsize=(4.1, 4.0))
    fig.subplots_adjust(left=.14, right=.985, bottom=.12, top=.985)
    ax.imshow(field, origin='lower', extent=(66.375, 100.125, 6.375, 38.625),
              interpolation='nearest', cmap=cmap, norm=BoundaryNorm(bounds, cmap.N),
              rasterized=True)
    ax.add_collection(LineCollection(segments, colors='#344951', linewidths=.34,
                                     alpha=.66, zorder=4))
    ax.set(xlim=(66.5, 100), ylim=(6.5, 38.5), xlabel='Longitude (°E)', ylabel='Latitude (°N)')
    ax.set_aspect('equal', adjustable='box')
    ax.set_xticks([70, 80, 90, 100])
    ax.set_yticks([10, 20, 30])
    ax.grid(color='#8EA1A8', alpha=.20, linewidth=.45, zorder=0)
    fig.savefig(destination, dpi=125, facecolor='white')
    plt.close(fig)


def _validate_probability(name: str, values: np.ndarray) -> np.ndarray:
    values = np.asarray(values)
    if values.shape != TARGET_SHAPE:
        raise ValueError(f'{name}: shape {values.shape}, expected {TARGET_SHAPE}')
    finite = values[np.isfinite(values)]
    if finite.size == 0 or np.any((finite < 0) | (finite > 1)):
        raise ValueError(f'{name}: probabilities must contain finite values within [0, 1]')
    return values


def build_verification(parent_id: str, start: str, site_dir: Path, work_dir: Path,
                       cache_dir: Path, climatology_path: Path | None,
                       land_mask_path: Path | None, as_of: date,
                       boundary_path: Path) -> dict:
    start_date = datetime.strptime(start, '%Y%m%d').date()
    imd_latest = latest_imd_date(as_of, cache_dir)
    latest_initialization = imd_latest - timedelta(days=5)
    if latest_initialization < start_date:
        raise RuntimeError('IMD does not yet contain five observed days for the first forecast')

    rainfall = imd.load(
        'rain', (start_date + timedelta(days=1)).isoformat(), imd_latest.isoformat(),
        source='realtime', cache_dir=str(cache_dir), progress=False,
    ).get_xarray()['rain']
    if rainfall.dims != ('time', 'lat', 'lon'):
        raise ValueError(f'Unexpected IMD rainfall dimensions: {rainfall.dims}')
    if (not np.allclose(rainfall['lat'].values, TARGET_LATS)
            or not np.allclose(rainfall['lon'].values, TARGET_LONS)):
        raise ValueError('IMD rainfall grid does not match the DryWetCast 0.25-degree India grid')
    rain_by_date = {
        np.datetime_as_string(value, unit='D'): np.asarray(rainfall.isel(time=index).values, dtype=np.float64)
        for index, value in enumerate(rainfall.time.values)
    }

    climatology_file = _model_file(climatology_path, 'climatology.npz', work_dir)
    land_file = _model_file(land_mask_path, 'land_mask.npy', work_dir)
    with np.load(climatology_file, allow_pickle=False) as values:
        climatology = {
            'dry': _sigmoid(values['logit_pi_dry']),
            'wet': _sigmoid(values['logit_pi_wet']),
        }
    land = np.load(land_file, allow_pickle=False).astype(bool)
    if land.shape != TARGET_SHAPE:
        raise ValueError(f'Land mask shape {land.shape}, expected {TARGET_SHAPE}')
    for event, values in climatology.items():
        if values.ndim != 3 or values.shape[1:] != TARGET_SHAPE:
            raise ValueError(f'{event} climatology has unexpected shape {values.shape}')

    folders = discover_date_folders(parent_id, start)
    pool = {
        config: {event: {kind: _empty_components() for kind in ('post', 'raw')}
                 for event in ('dry', 'wet')}
        for config in CONFIGS
    }
    map_pool = {
        config: {event: {kind: _empty_map_components() for kind in ('post', 'raw')}
                 for event in ('dry', 'wet')}
        for config in CONFIGS
    }
    final_map_dir = site_dir / 'assets' / 'verification'
    map_work = work_dir / 'verification-assets'
    if final_map_dir.exists():
        shutil.copytree(final_map_dir, map_work, dirs_exist_ok=True)
    else:
        map_work.mkdir(parents=True, exist_ok=True)
    segments = [np.column_stack((lon, lat)) for lon, lat in boundary_lines(boundary_path)]
    raw_counts = {config: 0 for config in CONFIGS}
    dates = []
    current = start_date
    while current <= latest_initialization:
        init = current.strftime('%Y%m%d')
        folder = folders.get((init, '00'))
        if folder is None:
            current += timedelta(days=1)
            continue
        files = forecast_files(folder)
        if files is None:
            current += timedelta(days=1)
            continue

        observed_dates = [(current + timedelta(days=offset)).isoformat() for offset in range(1, 6)]
        if any(value not in rain_by_date for value in observed_dates):
            current += timedelta(days=1)
            continue
        daily_rain = np.stack([rain_by_date[value] for value in observed_dates])
        valid_observation = np.all(np.isfinite(daily_rain) & (daily_rain >= 0), axis=0)
        dry_days = daily_rain < 1.0
        observed = {
            'dry': ((dry_days[0] & dry_days[1] & dry_days[2])
                    | (dry_days[1] & dry_days[2] & dry_days[3])
                    | (dry_days[2] & dry_days[3] & dry_days[4])).astype(np.float32),
            'wet': (daily_rain >= 1.0).any(axis=0).astype(np.float32),
        }
        doy = (current + timedelta(days=1)).timetuple().tm_yday
        clim = {event: climatology[event][min(doy - 1, 364)] for event in ('dry', 'wet')}

        record = {
            'initialization': init,
            'validStart': observed_dates[0],
            'validEnd': observed_dates[-1],
            'metrics': {},
            'maps': {'observed': {}},
        }
        for event in ('dry', 'wet'):
            relative = f'assets/verification/{init}-observed-{event}.png'
            _draw_map(np.where(land & valid_observation, observed[event], np.nan),
                      segments, map_work / Path(relative).name,
                      ['#F4F6F5', '#2B8C6B'], [-0.5, 0.5, 1.5])
            record['maps']['observed'][event] = relative
        for config, metadata in files.items():
            destination = work_dir / f'{init}-{config}.npz'
            download_file(metadata['id'], destination)
            with np.load(destination, allow_pickle=False) as archive:
                archive_date = str(archive['date'].item())
                archive_cycle = str(archive['cycle'].item()).zfill(2)
                archive_config = str(archive['config'].item())
                if archive_date != init or archive_cycle != '00':
                    raise ValueError(f'{destination.name}: archive metadata does not match its Drive folder')
                if archive_config != config:
                    raise ValueError(f'{destination.name}: configuration is {archive_config}, expected {config}')
                fields = {name: _validate_probability(f'{destination.name}:{name}', archive[name])
                          for name in archive.files
                          if name in ('p_dry', 'p_wet', 'raw_p_dry', 'raw_p_wet')}
                if 'p_dry' not in fields or 'p_wet' not in fields:
                    raise ValueError(f'{destination.name}: missing p_dry or p_wet')
                if ('raw_p_dry' in fields) != ('raw_p_wet' in fields):
                    raise ValueError(f'{destination.name}: raw_p_dry and raw_p_wet must both be present')

            record['metrics'][config] = {}
            record['maps'][config] = {}
            for event in ('dry', 'wet'):
                post, post_parts = score(fields[f'p_{event}'], observed[event], clim[event],
                                         valid_observation, land)
                _add_components(pool[config][event]['post'], post_parts)
                _add_map_components(map_pool[config][event]['post'], fields[f'p_{event}'],
                                    observed[event], clim[event], valid_observation, land)
                post_probability = _map_path(init, config, event, 'post', 'probability')
                post_outcome = _map_path(init, config, event, 'post', 'outcome')
                _draw_map(np.where(land, fields[f'p_{event}'], np.nan), segments,
                          map_work / Path(post_probability).name,
                          DRY_COLORS if event == 'dry' else WET_COLORS, PROBABILITY_BOUNDS)
                _draw_map(_outcome_field(fields[f'p_{event}'], observed[event], valid_observation, land),
                          segments, map_work / Path(post_outcome).name,
                          OUTCOME_COLORS, OUTCOME_BOUNDS)
                raw = None
                raw_maps = None
                raw_key = f'raw_p_{event}'
                if raw_key in fields:
                    raw, raw_parts = score(fields[raw_key], observed[event], clim[event],
                                           valid_observation, land)
                    _add_components(pool[config][event]['raw'], raw_parts)
                    _add_map_components(map_pool[config][event]['raw'], fields[raw_key],
                                        observed[event], clim[event], valid_observation, land)
                    raw_probability = _map_path(init, config, event, 'raw', 'probability')
                    raw_outcome = _map_path(init, config, event, 'raw', 'outcome')
                    _draw_map(np.where(land, fields[raw_key], np.nan), segments,
                              map_work / Path(raw_probability).name,
                              DRY_COLORS if event == 'dry' else WET_COLORS, PROBABILITY_BOUNDS)
                    _draw_map(_outcome_field(fields[raw_key], observed[event], valid_observation, land),
                              segments, map_work / Path(raw_outcome).name,
                              OUTCOME_COLORS, OUTCOME_BOUNDS)
                    raw_maps = {'probability': raw_probability, 'outcome': raw_outcome}
                record['metrics'][config][event] = {'post': post, 'raw': raw}
                record['maps'][config][event] = {
                    'post': {'probability': post_probability, 'outcome': post_outcome},
                    'raw': raw_maps,
                }
            if 'raw_p_dry' in fields and 'raw_p_wet' in fields:
                raw_counts[config] += 1
        dates.append(record)
        current += timedelta(days=1)

    pooled = {
        config: {
            event: {
                'post': pooled_metrics(pool[config][event]['post']),
                'raw': pooled_metrics(pool[config][event]['raw']),
            }
            for event in ('dry', 'wet')
        }
        for config in CONFIGS
    }
    pooled_maps = {config: {event: {} for event in ('dry', 'wet')} for config in CONFIGS}
    for config in CONFIGS:
        for event in ('dry', 'wet'):
            for kind in ('post', 'raw'):
                components = map_pool[config][event][kind]
                use = (components['count'] > 0) & (components['climatology'] > 0)
                if not np.any(use):
                    pooled_maps[config][event][kind] = None
                    continue
                field = np.full(TARGET_SHAPE, np.nan, dtype=np.float32)
                field[use] = 1.0 - components['model'][use] / components['climatology'][use]
                field = np.clip(field, -1, 1)
                relative = f'assets/verification/pooled-{config}-{event}-{kind}-bss.png'
                _draw_map(field, segments, map_work / Path(relative).name,
                          BSS_COLORS, BSS_BOUNDS, overwrite=True)
                pooled_maps[config][event][kind] = relative
    result = {
        'generatedAt': datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z'),
        'observationLatest': imd_latest.isoformat(),
        'latestVerifiedInitialization': dates[-1]['initialization'] if dates else None,
        'threshold': THRESHOLD,
        'provisional': True,
        'modelCommit': MODEL_COMMIT,
        'rawAvailability': raw_counts,
        'configLabels': {
            'gefs_reduced': 'GEFS Reduced', 'gefs_full': 'GEFS Full',
            'ncmrwf_reduced': 'NEPS Reduced', 'ncmrwf_full': 'NEPS Full',
        },
        'dates': dates,
        'pooled': pooled,
        'pooledMaps': pooled_maps,
    }
    site_dir.mkdir(parents=True, exist_ok=True)
    downloads = site_dir / 'downloads'
    downloads.mkdir(parents=True, exist_ok=True)
    if final_map_dir.exists():
        shutil.rmtree(final_map_dir)
    final_map_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(map_work, final_map_dir)
    (site_dir / 'verification-data.js').write_text(
        'window.VERIFICATION_DATA = ' + json.dumps(result, indent=2, allow_nan=False) + ';\n'
    )
    with (downloads / 'verification_metrics.csv').open('w', newline='') as handle:
        writer = csv.writer(handle, lineterminator='\n')
        writer.writerow([
            'initialization', 'valid_start', 'valid_end', 'configuration', 'event', 'forecast_type',
            'threshold', 'bss', 'pod', 'far', 'hits', 'misses', 'false_alarms',
            'correct_negatives', 'observed_fraction', 'forecast_fraction', 'land_cells',
        ])
        for record in dates:
            for config in CONFIGS:
                for event in ('dry', 'wet'):
                    for kind in ('post', 'raw'):
                        metrics = record['metrics'][config][event][kind]
                        if metrics is None:
                            continue
                        writer.writerow([
                            record['initialization'], record['validStart'], record['validEnd'], config,
                            event, kind, THRESHOLD, metrics['bss'], metrics['pod'], metrics['far'],
                            metrics['hits'], metrics['misses'], metrics['falseAlarms'],
                            metrics['correctNegatives'], metrics['observedFraction'],
                            metrics['forecastFraction'], metrics['nCells'],
                        ])
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--parent-folder-id', required=True)
    parser.add_argument('--start-date', default=DEFAULT_START)
    parser.add_argument('--site-dir', type=Path, default=Path('site'))
    parser.add_argument('--cache-dir', type=Path, default=Path.home() / '.cache' / 'imdlib')
    parser.add_argument('--climatology', type=Path)
    parser.add_argument('--land-mask', type=Path)
    parser.add_argument('--boundaries', type=Path, default=Path('data/india_states.geojson'))
    parser.add_argument('--as-of-date', type=lambda value: datetime.strptime(value, '%Y%m%d').date(),
                        default=date.today())
    parser.add_argument('--work-dir', type=Path)
    args = parser.parse_args()
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    base = args.work_dir or Path(tempfile.mkdtemp(prefix='drywetcast-verification-'))
    base.mkdir(parents=True, exist_ok=True)
    try:
        result = build_verification(
            args.parent_folder_id, args.start_date, args.site_dir, base, args.cache_dir,
            args.climatology, args.land_mask, args.as_of_date, args.boundaries,
        )
        print(f"Verified {len(result['dates'])} initialization(s) through "
              f"{result['latestVerifiedInitialization']}; IMD available through "
              f"{result['observationLatest']}")
    finally:
        if args.work_dir is None:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == '__main__':
    main()
