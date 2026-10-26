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

The separate verification view compares completed five-day forecasts with
provisional IMD 0.25-degree real-time rainfall. It reports Brier Skill Score
(BSS) against the bundled 1991-2020 IMD event climatology and categorical
Probability of Detection (POD) and False Alarm Ratio (FAR) at a 0.50 threshold.
When forecast archives contain raw GEFS/NEPS member event fractions, it reports
the raw scores beside the post-processed scores and their BSS improvement.
For each completed initialization it also publishes forecast-probability,
observed-event and categorical-outcome maps. Spatial BSS is calculated at each
land grid cell by pooling squared errors across all available verified dates;
this avoids treating a single binary observation as a meaningful pointwise BSS.
The display is clipped to -1 through +1, while the tabular BSS remains unclipped.

Forecasting code and model documentation: [DryWetCast-India](https://github.com/pdasiitism/DryWetCast-India)

## Daily update

The `Update forecast from Google Drive` GitHub Actions workflow makes its primary
daily attempt at 16:00 IST, followed by half-hourly retries through 20:00 IST.
GitHub may start scheduled jobs later than their nominal time, so the repeated
checks make the daily update resilient to both scheduling delays and uploads that
finish late. It
selects the newest date folder only when all four configuration folders each contain
exactly one NPZ forecast record.
It validates the common initialization, cycle, grid, variables and probability range
before rebuilding and publishing the site. A missing or invalid upload leaves the
previous successful dashboard unchanged.

On the same run, `scripts/update_verification.py` finds the latest available IMD
real-time rainfall day and verifies every initialization whose full D1-D5 window is
observed. IMD and forecast fields already share the 129 x 135 grid. The verification
CSV is written to `site/downloads/verification_metrics.csv`. Older NPZ archives with
only `p_dry` and `p_wet` retain valid post-processed verification and are marked as
not having raw probabilities.

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
