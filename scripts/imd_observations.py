"""Read IMD rainfall, with a validated fallback to the same official endpoint."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
import subprocess

import imdlib as imd
from imdlib.util import REALTIME_URLS, get_filename_realtime


RAIN_BYTES = 129 * 135 * 4


def download_day(day, directory):
    destination = Path(get_filename_realtime(day, 'rain', str(directory)))
    if destination.exists() and destination.stat().st_size == RAIN_BYTES:
        return destination
    temporary = destination.with_suffix('.part')
    library_url, field = REALTIME_URLS['rain']
    form_url = library_url.replace('://imdpune.gov.in/', '://www.imdpune.gov.in/')
    try:
        errors = []
        for url in (form_url, library_url):
            try:
                subprocess.run([
                    'curl', '--fail', '-sS', '--max-time', '25',
                    '-X', 'POST', '-d', f'{field}={day:%d%m%Y}', url,
                    '-e', url.rsplit('/', 1)[0] + '/Rain_Download.html',
                    '-H', 'Cache-Control: no-cache', '-H', 'User-Agent: Mozilla/5.0',
                    '-o', str(temporary),
                ], check=True, timeout=30)
                size = temporary.stat().st_size
                if size == RAIN_BYTES:
                    temporary.replace(destination)
                    return destination
                if size:
                    errors.append(f'{size} bytes, expected {RAIN_BYTES}')
            except (subprocess.SubprocessError, OSError) as error:
                errors.append(str(error))
        if not errors:
            raise imd.DataNotAvailableError(f'IMD returned no rainfall for {day}')
        raise imd.DownloadError(f'Could not download IMD rainfall for {day}: {"; ".join(errors)}')
    finally:
        temporary.unlink(missing_ok=True)


def load_rainfall(start, end, cache_dir):
    directory = Path(cache_dir) / 'direct-rainfall'
    directory.mkdir(parents=True, exist_ok=True)
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    paths = [Path(get_filename_realtime(day, 'rain', str(directory))) for day in days]
    if all(path.is_file() and path.stat().st_size == RAIN_BYTES for path in paths):
        return imd.open_real_data('rain', start, end, file_dir=str(directory))
    try:
        return imd.load('rain', start, end, source='realtime',
                        cache_dir=str(cache_dir), progress=False)
    except (imd.DataNotAvailableError, imd.DownloadError) as error:
        print(f'IMD library retrieval failed for {start} to {end}: {error}. '
              'Checking the same IMD endpoint directly.', flush=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda day: download_day(day, directory), days))
    return imd.open_real_data('rain', start, end, file_dir=str(directory))
