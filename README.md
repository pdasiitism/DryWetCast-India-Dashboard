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
Drive parent folder at 18:45, 20:45 and 22:45 IST. It selects the newest date folder
only when all four configuration folders each contain exactly one NPZ forecast record.
It validates the common initialization, cycle, grid, variables and probability range
before rebuilding and publishing the site. A missing or invalid upload leaves the
previous successful dashboard unchanged.

The workflow requires one repository secret:

```text
GOOGLE_DRIVE_API_KEY
```

The API key is used only to list and download files that the folder owner has already
made available to anyone with the link. Restrict the key to the Google Drive API.

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
  --parent-folder-id 1zGAcTf_kovWZvKlI1j2ud9Hrx207Sv04 \
  --api-key "$GOOGLE_DRIVE_API_KEY"
```

The publishable static site is in `site/`.
