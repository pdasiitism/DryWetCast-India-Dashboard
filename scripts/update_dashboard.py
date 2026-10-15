#!/usr/bin/env python3
"""Download the latest complete DryWetCast cycle from public Drive and rebuild the site."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
import json
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import quote, urlencode
from urllib.request import urlopen

import matplotlib as mpl
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm, ListedColormap
import numpy as np


CONFIGS = ('gefs_reduced', 'gefs_full', 'ncmrwf_reduced', 'ncmrwf_full')
DATE_PATTERN = re.compile(r'^20\d{6}$')
FOLDER_MIME = 'application/vnd.google-apps.folder'
DRIVE_API = 'https://www.googleapis.com/drive/v3/files'

DRY_COLORS = ['#FFF9E8', '#FCE7A2', '#F9C65A', '#F08A3C', '#C94B32', '#7F1D1D']
WET_COLORS = ['#F1FAFE', '#CDECF7', '#91D0E8', '#4B9DCE', '#2166AC', '#08306B']
PROBABILITY_BOUNDS = [0, .2, .4, .6, .8, .9, 1.00001]
DIFF_COLORS = ['#08306B', '#2166AC', '#4B9DCE', '#CDECF7',
               '#FFF9E8', '#F9C65A', '#F08A3C', '#7F1D1D']
DIFF_BOUNDS = [-1.00001, -.20, -.10, -.05, 0, .05, .10, .20, 1.00001]

mpl.rcParams.update({
    'font.family': 'DejaVu Sans',
    'font.size': 10,
    'axes.facecolor': '#F3F6F5',
    'figure.facecolor': '#FFFFFF',
    'axes.edgecolor': '#B3C1C5',
    'axes.linewidth': .8,
    'xtick.labelsize': 9,
    'ytick.labelsize': 9,
})


def drive_children(folder_id: str, api_key: str) -> list[dict]:
    """List every direct child of a public Google Drive folder."""
    files = []
    page_token = None
    while True:
        params = {
            'q': f"'{folder_id}' in parents and trashed = false",
            'fields': 'nextPageToken,files(id,name,mimeType,modifiedTime,size)',
            'pageSize': 1000,
            'key': api_key,
            'orderBy': 'name_natural',
        }
        if page_token:
            params['pageToken'] = page_token
        with urlopen(f'{DRIVE_API}?{urlencode(params)}', timeout=60) as response:
            payload = json.load(response)
        files.extend(payload.get('files', []))
        page_token = payload.get('nextPageToken')
        if not page_token:
            return files


def download_file(file_id: str, api_key: str, destination: Path) -> None:
    url = f'{DRIVE_API}/{quote(file_id)}?{urlencode({"alt": "media", "key": api_key})}'
    with urlopen(url, timeout=180) as response, destination.open('wb') as handle:
        shutil.copyfileobj(response, handle)


def locate_latest_complete(parent_id: str, api_key: str) -> tuple[str, str, dict[str, dict]]:
    dates = [item for item in drive_children(parent_id, api_key)
             if item.get('mimeType') == FOLDER_MIME and DATE_PATTERN.fullmatch(item.get('name', ''))]
    for date_folder in sorted(dates, key=lambda item: item['name'], reverse=True):
        children = drive_children(date_folder['id'], api_key)
        folders = {item['name'].lower(): item for item in children if item.get('mimeType') == FOLDER_MIME}
        selected = {}
        for config in CONFIGS:
            folder = folders.get(config)
            if not folder:
                break
            npz_files = [item for item in drive_children(folder['id'], api_key)
                         if item.get('name', '').lower().endswith('.npz')]
            if len(npz_files) != 1:
                break
            selected[config] = npz_files[0]
        if len(selected) == len(CONFIGS):
            return date_folder['name'], date_folder['id'], selected
    raise RuntimeError('No complete YYYYMMDD folder containing all four configuration NPZ files was found')


def load_forecasts(input_dir: Path) -> dict[str, dict[str, np.ndarray]]:
    forecasts = {}
    reference_grid = None
    date = cycle = None
    for config in CONFIGS:
        path = input_dir / f'{config}.npz'
        with np.load(path, allow_pickle=False) as archive:
            missing = {'p_dry', 'p_wet', 'lats', 'lons', 'date', 'cycle'} - set(archive.files)
            if missing:
                raise ValueError(f'{path.name} is missing: {sorted(missing)}')
            current = {key: archive[key] for key in archive.files}
        if current['p_dry'].shape != (129, 135) or current['p_wet'].shape != (129, 135):
            raise ValueError(f'{path.name} does not contain 129 x 135 probability grids')
        for event in ('p_dry', 'p_wet'):
            valid = np.isfinite(current[event])
            if np.any((current[event][valid] < 0) | (current[event][valid] > 1)):
                raise ValueError(f'{path.name} contains {event} values outside 0–1')
        grid = (current['lats'].shape, current['lons'].shape,
                np.asarray(current['lats']).tobytes(), np.asarray(current['lons']).tobytes())
        if reference_grid is None:
            reference_grid = grid
            date = str(current['date'].item())
            cycle = str(current['cycle'].item()).zfill(2)
        elif grid != reference_grid or str(current['date'].item()) != date or str(current['cycle'].item()).zfill(2) != cycle:
            raise ValueError('The four files do not share one grid, initialization date and cycle')
        forecasts[config] = current
    return forecasts


def boundary_lines(path: Path) -> list[tuple[np.ndarray, np.ndarray]]:
    collection = json.loads(path.read_text())
    lines = []
    for feature in collection['features']:
        geometry = feature['geometry']
        polygons = geometry['coordinates'] if geometry['type'] == 'Polygon' else [p for p in geometry['coordinates']]
        if geometry['type'] == 'Polygon':
            polygons = [polygons]
        for polygon in polygons:
            for ring in polygon:
                array = np.asarray(ring, dtype=float)
                if array.ndim == 2 and array.shape[1] >= 2:
                    lines.append((array[:, 0], array[:, 1]))
    return lines


def metrics(first: np.ndarray, second: np.ndarray) -> dict[str, float]:
    valid = np.isfinite(first) & np.isfinite(second)
    a = first[valid].astype(float)
    b = second[valid].astype(float)
    difference = a - b
    return {
        'mean': float(difference.mean()),
        'mae': float(np.abs(difference).mean()),
        'rmse': float(np.sqrt(np.mean(difference ** 2))),
        'correlation': float(np.corrcoef(a, b)[0, 1]),
        'p95_absolute_difference': float(np.quantile(np.abs(difference), .95)),
        'maximum_absolute_difference': float(np.max(np.abs(difference))),
        'land_cells': int(valid.sum()),
    }


def draw_map(field: np.ndarray, lats: np.ndarray, lons: np.ndarray,
             lines: list[tuple[np.ndarray, np.ndarray]], destination: Path,
             colors: list[str], bounds: list[float]) -> None:
    cmap = ListedColormap(colors)
    cmap.set_bad('#FFFFFF')
    fig, ax = plt.subplots(figsize=(5.35, 5.15))
    fig.subplots_adjust(left=.13, right=.985, bottom=.11, top=.985)
    ax.pcolormesh(lons, lats, field, shading='nearest', cmap=cmap,
                  norm=BoundaryNorm(bounds, cmap.N), rasterized=True)
    for lon, lat in lines:
        ax.plot(lon, lat, color='#344951', alpha=.68, linewidth=.42, zorder=4)
    ax.set(xlim=(66.5, 100), ylim=(6.5, 38.5), xlabel='Longitude (°E)', ylabel='Latitude (°N)')
    ax.set_aspect('equal', adjustable='box')
    ax.set_xticks([70, 80, 90, 100])
    ax.set_yticks([10, 20, 30])
    ax.grid(color='#8EA1A8', alpha=.20, linewidth=.5, zorder=0)
    fig.savefig(destination, dpi=220, facecolor='white')
    plt.close(fig)


def build_site(forecasts: dict[str, dict[str, np.ndarray]], site_dir: Path,
               boundary_path: Path) -> None:
    assets = site_dir / 'assets'
    downloads = site_dir / 'downloads'
    assets.mkdir(parents=True, exist_ok=True)
    downloads.mkdir(parents=True, exist_ok=True)
    lines = boundary_lines(boundary_path)
    lats = forecasts['gefs_reduced']['lats']
    lons = forecasts['gefs_reduced']['lons']

    dashboard_stats = {}
    fields = {}
    csv_rows = []
    for family in ('reduced', 'full'):
        for event in ('dry', 'wet'):
            key = f'p_{event}'
            gefs = forecasts[f'gefs_{family}'][key]
            neps = forecasts[f'ncmrwf_{family}'][key]
            palette = DRY_COLORS if event == 'dry' else WET_COLORS
            draw_map(gefs, lats, lons, lines, assets / f'{family}-{event}-gefs.png', palette, PROBABILITY_BOUNDS)
            draw_map(neps, lats, lons, lines, assets / f'{family}-{event}-neps.png', palette, PROBABILITY_BOUNDS)
            draw_map(gefs - neps, lats, lons, lines, assets / f'{family}-{event}-difference.png', DIFF_COLORS, DIFF_BOUNDS)
            result = metrics(gefs, neps)
            dashboard_stats[f'{family}-{event}'] = {k: result[k] for k in ('mean', 'mae', 'correlation')}
            csv_rows.append((family, event, 'GEFS minus NEPS', result))
            fields[f'{family}_{event}'] = gefs - neps

    for source, archive_source in (('gefs', 'gefs'), ('neps', 'ncmrwf')):
        for event in ('dry', 'wet'):
            key = f'p_{event}'
            full = forecasts[f'{archive_source}_full'][key]
            reduced = forecasts[f'{archive_source}_reduced'][key]
            draw_map(full - reduced, lats, lons, lines,
                     assets / f'{source}-{event}-full-minus-reduced.png', DIFF_COLORS, DIFF_BOUNDS)
            result = metrics(full, reduced)
            dashboard_stats[f'{source}-full-reduced-{event}'] = {k: result[k] for k in ('mean', 'mae', 'correlation')}
            csv_rows.append((source, event, 'Full minus Reduced', result))
            fields[f'{source}_full_minus_reduced_{event}'] = full - reduced

    date = str(forecasts['gefs_reduced']['date'].item())
    cycle = str(forecasts['gefs_reduced']['cycle'].item()).zfill(2)
    display_date = datetime.strptime(date, '%Y%m%d').strftime('%-d %B %Y')
    data = {'initialization': f'Initialization {display_date}, {cycle} UTC',
            'date': date, 'cycle': cycle, 'stats': dashboard_stats}
    (site_dir / 'data.js').write_text('window.DASHBOARD_DATA = ' + json.dumps(data, indent=2) + ';\n')

    with (downloads / 'comparison_statistics.csv').open('w', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['selection', 'event', 'difference_definition', 'mean_difference', 'mae',
                         'rmse', 'correlation', 'p95_absolute_difference',
                         'maximum_absolute_difference', 'land_cells'])
        for selection, event, definition, result in csv_rows:
            writer.writerow([selection, event, definition, result['mean'], result['mae'],
                             result['rmse'], result['correlation'], result['p95_absolute_difference'],
                             result['maximum_absolute_difference'], result['land_cells']])
    np.savez_compressed(downloads / 'comparison_difference_fields.npz',
                        lats=lats, lons=lons, date=date, cycle=cycle, **fields)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--parent-folder-id', required=True)
    parser.add_argument('--api-key', required=True)
    parser.add_argument('--site-dir', type=Path, default=Path('site'))
    parser.add_argument('--boundaries', type=Path, default=Path('data/india_states.geojson'))
    parser.add_argument('--work-dir', type=Path)
    args = parser.parse_args()

    date_name, _, files = locate_latest_complete(args.parent_folder_id, args.api_key)
    base = args.work_dir or Path(tempfile.mkdtemp(prefix='drywetcast-dashboard-'))
    base.mkdir(parents=True, exist_ok=True)
    try:
        for config, item in files.items():
            download_file(item['id'], args.api_key, base / f'{config}.npz')
        forecasts = load_forecasts(base)
        forecast_date = str(forecasts['gefs_reduced']['date'].item())
        if forecast_date != date_name:
            raise ValueError(f'Drive folder is {date_name}, but its NPZ initialization is {forecast_date}')
        build_site(forecasts, args.site_dir, args.boundaries)
        print(f'Published dashboard inputs for {forecast_date}')
    finally:
        if args.work_dir is None:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == '__main__':
    main()
