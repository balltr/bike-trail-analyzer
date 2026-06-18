# Mountain Bike Trail Analyzer

Turns a GPS ride recording (`.gpx` or `.fit`) into two visual analyses of the trail:

1. **An interactive satellite map** — the route drawn over satellite imagery, with each
   segment color-coded by its elevation gradient (blue = descent, yellow = flat,
   red = climb). Start/finish markers, per-segment hover details, and a hover-able
   ride summary on the finish marker.
2. **An elevation profile chart** — elevation vs. distance, the line colored by the
   same gradient scale, saved as a PNG.

Both are generated from one command.

## What it measures

For every segment of the ride it computes:

- **Distance** — horizontal (great-circle) distance between GPS points.
- **Elevation gradient** — slope as a percentage (`100 × rise / run`).
- **Cumulative distance, elapsed time, and running elevation gain/loss.**

The finish-marker summary reports total distance, total time, total elevation gained
and lost, average gradient, and the steepest (realistic) climb and descent.

## Requirements

- **Python 3.14** (developed against the global CPython at
  `C:\Users\tball\AppData\Local\Python\pythoncore-3.14-64`; the editor interpreter is
  pinned in `.vscode/settings.json`).
- Python packages:

  ```
  python -m pip install gpxpy fitparse folium matplotlib
  ```

  (`branca` and `numpy` come in as dependencies of `folium`/`matplotlib`.)

## Usage

Run on the default file baked into `main()`:

```
python trail_analyzer.py
```

Or pass any `.gpx` or `.fit` file:

```
python trail_analyzer.py Cougar_Ridge_mountain_bike_ride_4265.gpx
```

> **Note:** the VS Code Run button (▶) runs the script with *no* arguments, so it always
> uses the default filename in `main()`. To analyze a specific file, run from the
> integrated terminal with the filename as shown above.

### Outputs

For an input named `Ride.gpx`, the script writes (next to the script):

| File                 | What it is                                  |
|----------------------|---------------------------------------------|
| `Ride.html`          | Interactive satellite map (open in browser) |
| `Ride_profile.png`   | Elevation profile chart                     |

It also prints a one-line summary to the terminal (segment count, total distance,
and the raw min/max/avg gradient).

## How it works

The pipeline is four stages:

1. **Parse** (`parse_file` → `parse_gpx` / `parse_fit`)
   Reads the file and returns a list of points, each `{lat, lon, elevation, time}`.
   The correct parser is chosen by file extension; each parser raises `ValueError` if
   handed the wrong type. FIT latitude/longitude are stored in *semicircles* and are
   converted to degrees so both formats produce identical units.

2. **Compute gradients** (`compute_gradients`)
   Walks the points and builds *segments*. Rather than measuring slope between every
   adjacent pair of points (which is dominated by GPS noise), it **accumulates distance
   until a segment spans at least `min_distance_m`**, then emits one segment — keeping
   every intermediate point so the drawn line still follows the trail. Elevation is
   lightly smoothed first (`smooth_window`) to suppress barometric noise. Each segment
   carries distance, gradient, cumulative distance, elapsed time, and running
   elevation gain/loss.

3. **Render the map** (`render_map`)
   Draws each segment as a colored polyline over Esri World Imagery satellite tiles,
   adds start (green) and finish (red) markers with labels, per-segment hover tooltips,
   a hover-able ride summary on the finish marker, a gradient legend, and auto-zooms to
   fit the whole trail.

4. **Plot the profile** (`plot_elevation_profile`)
   Renders elevation vs. distance as a gradient-colored line (matplotlib, headless),
   saved to PNG.

## Tuning the gradient calculation

GPS data is noisy, especially elevation and under tree cover. Two parameters on
`compute_gradients(points, smooth_window=5, min_distance_m=1.0)` control the cleanup:

- **`min_distance_m`** — minimum segment length in meters. Larger values measure
  gradient over a longer, more meaningful baseline and remove most spikes.
  `8.0` is a good default for mountain biking.
- **`smooth_window`** — number of points averaged together to smooth elevation before
  computing gradients. `5` is light; raise toward `9–11` for calmer numbers, set to `1`
  to disable.

**How to know it's tuned:** compare the reported total elevation gain/loss against the
recording app (Strava/Garmin). Within ~5–10% is dialed in. Increase `min_distance_m`
if numbers are still spiky; lower `smooth_window` if real climbs look washed out.

### Artifacts that smoothing can't fix

Under dense tree cover the GPS *horizontal* position can stall while the altimeter keeps
reading — producing physically impossible gradients (e.g. −124% over 8 m) that no amount
of smoothing removes, because the problem is the distance, not the elevation. To keep
these out of the summary, `render_map(..., realistic_max_pct=50.0)` excludes segments
steeper than ±`realistic_max_pct` from the steepest-climb/descent figures and notes how
many were excluded. Lower it (e.g. `35.0`) for stricter filtering. This affects only the
summary statistics; the map and profile still draw the full trail (colors are clamped to
`±clamp_pct`, default 25%, so outliers don't dominate the scale).

## Project layout

```
trail_analyzer.py    # all parsing, analysis, and visualization
.vscode/settings.json # pins the Python interpreter for the editor
.gitignore           # ignores generated outputs and ride data
```

Generated outputs (`*.html`, `*.png`) and ride files (`*.gpx`, `*.fit`) are gitignored —
they're rebuilt from the script and source data, so only the code is tracked.

## Status

Working end-to-end: parses GPX/FIT, computes noise-filtered gradients, and produces an
interactive satellite map plus an elevation profile.
