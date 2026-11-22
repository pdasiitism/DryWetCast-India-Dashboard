#!/usr/bin/env python3
"""Restore deployed products and refresh verification without rolling them back."""

import argparse
import csv
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import tempfile


GENERATED = ('data.js', 'verification-data.js', 'assets', 'downloads')
PUBLISH_WORKFLOWS = {'.github/workflows/pages.yml',
                     '.github/workflows/update-from-drive.yml'}


def read_data(site):
    text = (site / 'verification-data.js').read_text()
    prefix = 'window.VERIFICATION_DATA = '
    if not text.startswith(prefix):
        raise ValueError('Unrecognized verification data file')
    return json.loads(text[len(prefix):].strip().removesuffix(';'))


def validate(site):
    data = read_data(site)
    dates = [item['initialization'] for item in data['dates']]
    if not dates or dates != sorted(set(dates)):
        raise ValueError('Verification dates are empty, duplicated, or unordered')
    if data['latestVerifiedInitialization'] != dates[-1]:
        raise ValueError('Latest verification date does not match the records')
    with (site / 'downloads/verification_metrics.csv').open() as handle:
        csv_dates = {row['initialization'] for row in csv.DictReader(handle)}
    if csv_dates != set(dates):
        raise ValueError('Verification CSV and dashboard cover different dates')

    def check_maps(value):
        if isinstance(value, dict):
            for child in value.values():
                check_maps(child)
        elif isinstance(value, list):
            for child in value:
                check_maps(child)
        elif isinstance(value, str) and value.startswith('assets/verification/'):
            path = PurePosixPath(value)
            if '..' in path.parts or not (site / value).is_file():
                raise ValueError(f'Missing or invalid verification map: {value}')

    check_maps(data)
    return data


def ensure_no_regression(previous, candidate):
    old_dates = {row['initialization']: row for row in previous['dates']}
    new_dates = {row['initialization']: row for row in candidate['dates']}
    if not old_dates.keys() <= new_dates.keys():
        raise ValueError('Refresh would remove previously verified initializations')
    if candidate['observationLatest'] < previous['observationLatest']:
        raise ValueError('Refresh would move the observation date backwards')
    for date, record in old_dates.items():
        for config, events in record['metrics'].items():
            for event, kinds in events.items():
                for kind, value in kinds.items():
                    replacement = (new_dates[date]['metrics'].get(config, {})
                                   .get(event, {}).get(kind))
                    if value is not None and replacement is None:
                        raise ValueError(f'Refresh would lose {date} {config} {event} {kind}')


def replace_products(source, destination, names=GENERATED):
    for name in names:
        src, dst = source / name, destination / name
        if dst.is_dir():
            shutil.rmtree(dst)
        elif dst.exists():
            dst.unlink()
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)


def gh_json(*args):
    return json.loads(subprocess.check_output(['gh', *args], text=True, timeout=60))


def restore(site, repository):
    # Restore one complete deployment, never independent caches for maps/scores.
    artifacts = gh_json('api', f'repos/{repository}/actions/artifacts?per_page=100')['artifacts']
    selected = None
    for artifact in sorted(artifacts, key=lambda item: item['id'], reverse=True):
        if artifact['name'] != 'github-pages' or artifact['expired']:
            continue
        run_id = artifact['workflow_run']['id']
        run = gh_json('api', f'repos/{repository}/actions/runs/{run_id}')
        if (run['conclusion'] == 'success' and run['head_branch'] == 'main'
                and run['path'] in PUBLISH_WORKFLOWS):
            selected = run_id
            break
    if selected is None:
        raise RuntimeError('No retained successful deployment found; refusing to publish repository snapshots')
    print(f'Restoring saved deployment {selected}', flush=True)
    with tempfile.TemporaryDirectory(prefix='drywet-restore-') as directory:
        root = Path(directory)
        subprocess.run(['gh', 'run', 'download', str(selected), '--repo', repository,
                        '--name', 'github-pages', '--dir', str(root / 'artifact')],
                       check=True, timeout=180)
        restored = root / 'restored'
        restored.mkdir()
        with tarfile.open(root / 'artifact/artifact.tar') as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                if path.is_absolute() or '..' in path.parts:
                    raise ValueError('Unsafe deployment archive path')
                if not path.parts or path.parts[0] not in GENERATED:
                    continue
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ValueError('Unexpected link in deployment archive')
                destination = restored / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, destination.open('wb') as target:
                    shutil.copyfileobj(source, target)
        data = validate(restored)
        replace_products(restored, site)
        print(f"Restored deployment {selected}; verification through {data['latestVerifiedInitialization']}")


def refresh(site, command):
    previous = validate(site)
    with tempfile.TemporaryDirectory(prefix='drywet-refresh-') as directory:
        staged = Path(directory) / 'site'
        shutil.copytree(site, staged)
        # Recompute maps as well as numbers: provisional observations may change.
        shutil.rmtree(staged / 'assets/verification', ignore_errors=True)
        try:
            subprocess.run([*command, '--site-dir', str(staged)], check=True)
            candidate = validate(staged)
            ensure_no_regression(previous, candidate)
        except (subprocess.CalledProcessError, ValueError, KeyError, OSError) as error:
            message = (f"Verification refresh failed; retained the complete previous results through "
                       f"{previous['latestVerifiedInitialization']}. {error}")
            print(f'::warning::{message}', flush=True)
            import os
            if os.environ.get('GITHUB_STEP_SUMMARY'):
                with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as handle:
                    handle.write(message + '\n')
            return False
        replace_products(staged, site, ('verification-data.js',
                         'downloads/verification_metrics.csv', 'assets/verification'))
        print(f"Accepted verification through {candidate['latestVerifiedInitialization']}")
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['restore', 'refresh'])
    parser.add_argument('--site-dir', type=Path, default=Path('site'))
    parser.add_argument('--repository')
    args, command = parser.parse_known_args()
    if args.action == 'restore':
        if not args.repository:
            parser.error('--repository is required for restore')
        restore(args.site_dir, args.repository)
    else:
        if command[:1] == ['--']:
            command = command[1:]
        if not command:
            parser.error('refresh requires a command after --')
        refresh(args.site_dir, command)


if __name__ == '__main__':
    main()
