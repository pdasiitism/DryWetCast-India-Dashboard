# DryWetCast India Dashboard

Public operational comparison dashboard for the four DryWetCast India configurations:

- GEFS Reduced
- GEFS Full
- NEPS Reduced
- NEPS Full

The site compares GEFS with NEPS within each feature set and Full with Reduced
within each rainfall source. DRY and WET probabilities use the same fixed-category
palettes as the operational outlook maps. Difference fields are shown as literal
probability differences on the -1 to +1 scale.

Forecasting code and model documentation: [DryWetCast-India](https://github.com/pdasiitism/DryWetCast-India)

## Daily update

The `Update forecast from Google Drive` GitHub Actions workflow checks the public
Drive parent folder roughly every 30 minutes from 16:00 through 23:30 IST. GitHub may
start scheduled jobs later than their nominal time, so the repeated checks make the
daily update resilient to both scheduling delays and uploads that finish late. It
selects the newest date folder only when all four configuration folders each contain
exactly one NPZ forecast record.
It validates the common initialization, cycle, grid, variables and probability range
before rebuilding and publishing the site. A missing or invalid upload leaves the
previous successful dashboard unchanged.

The workflow reads the folder through its public `Anyone with the link` pages. It does
not use a Google login, Google Cloud access, Drive API key, GitHub secret or access to
the original forecast bucket. The public Google Drive view exposes at most 50 direct
children, so older date folders should be moved into an archive folder before the
parent reaches 50 cycles.

The configured public parent folder is:

```text
1zGAcTf_kovWZvKlI1j2ud9Hrx207Sv04
```

## Local rebuild

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/update_dashboard.py \
  --parent-folder-id 1zGAcTf_kovWZvKlI1j2ud9Hrx207Sv04
```

The publishable static site is in `site/`.
