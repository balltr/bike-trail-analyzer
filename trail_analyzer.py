import math
import os
import sys

import branca.colormap as cm
import folium
import gpxpy
from fitparse import FitFile

# FIT files store latitude/longitude in "semicircles". Multiply by this factor
# to convert to degrees so the values match the units gpxpy returns.
SEMICIRCLE_TO_DEGREES = 180.0 / (2 ** 31)


def parse_gpx(filename):
    """Parse a .gpx file and return a list of point dicts.

    Raises ValueError if the file is not a .gpx file.
    """
    if not filename.lower().endswith('.gpx'):
        raise ValueError(f"parse_gpx expected a .gpx file but got: {filename}")

    with open(filename, 'r') as f:
        gpx = gpxpy.parse(f)

    points = []
    for track in gpx.tracks:
        for segment in track.segments:
            for point in segment.points:
                points.append({
                    'lat': point.latitude,
                    'lon': point.longitude,
                    'elevation': point.elevation,
                    'time': point.time
                })
    return points


def parse_fit(filename):
    """Parse a .fit file and return a list of point dicts.

    Raises ValueError if the file is not a .fit file.
    """
    if not filename.lower().endswith('.fit'):
        raise ValueError(f"parse_fit expected a .fit file but got: {filename}")

    fitfile = FitFile(filename)

    points = []
    for record in fitfile.get_messages('record'):
        values = {field.name: field.value for field in record}

        lat = values.get('position_lat')
        lon = values.get('position_long')

        points.append({
            'lat': lat * SEMICIRCLE_TO_DEGREES if lat is not None else None,
            'lon': lon * SEMICIRCLE_TO_DEGREES if lon is not None else None,
            # Prefer the GPS/barometric enhanced altitude when present.
            'elevation': values.get('enhanced_altitude', values.get('altitude')),
            'time': values.get('timestamp')
        })
    return points


def parse_file(filename):
    """Dispatch to the correct parser based on the file extension."""
    extension = os.path.splitext(filename)[1].lower()
    if extension == '.gpx':
        return parse_gpx(filename)
    elif extension == '.fit':
        return parse_fit(filename)
    else:
        raise ValueError(f"Unsupported file type: {filename} (expected .gpx or .fit)")


def _haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance between two lat/lon points, in meters."""
    """Converts lat/lon to horizontal distance. Necessary for computing gradient."""
    earth_radius_m = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return 2 * earth_radius_m * math.asin(math.sqrt(a))


def _smooth(values, window):
    """Centered moving average; returns a list the same length as `values`."""
    """Necessary to prevent wild gradient values from GPS/barometric noise."""
    if window <= 1:
        return list(values)
    half = window // 2
    smoothed = []
    for i in range(len(values)):
        chunk = values[max(0, i - half):min(len(values), i + half + 1)]
        smoothed.append(sum(chunk) / len(chunk))
    return smoothed


def compute_gradients(points, smooth_window=5, min_distance_m=1.0):
    """Turn a list of point dicts into a list of segment dicts with gradient.

    Each segment joins two consecutive points that have valid lat/lon/elevation
    and span at least `min_distance_m` meters. The distance filter drops GPS
    jitter recorded while stationary, which would otherwise produce wild
    (or divide-by-zero) gradient values.

    `smooth_window` applies a centered moving average to elevation before
    computing gradients, to reduce barometric/GPS noise. Set to 1 to disable.

    Returns a list of dicts, each with:
        lat1, lon1, lat2, lon2  - segment endpoints (degrees)
        distance_m              - horizontal distance (meters)
        elevation_change_m      - signed elevation delta (meters)
        gradient_pct            - 100 * rise / run
        cumulative_distance_m   - distance from the start of the track (meters)
    """
    usable = [p for p in points
              if p['lat'] is not None
              and p['lon'] is not None
              and p['elevation'] is not None]
    if len(usable) < 2:
        return []

    elevations = _smooth([p['elevation'] for p in usable], smooth_window)

    start_time = usable[0]['time']
    segments = []
    cumulative = 0.0
    elev_gain = 0.0
    elev_loss = 0.0

    # Walk the points accumulating distance until a segment spans at least
    # `min_distance_m`, then emit it. Points are never dropped: the segment's
    # `path` keeps every intermediate point so the drawn line follows the trail,
    # while the gradient is measured over the full (>= min_distance_m) baseline.
    anchor = 0
    seg_dist = 0.0
    path = [(usable[0]['lat'], usable[0]['lon'])]
    for i in range(1, len(usable)):
        seg_dist += _haversine_m(usable[i - 1]['lat'], usable[i - 1]['lon'],
                                 usable[i]['lat'], usable[i]['lon'])
        path.append((usable[i]['lat'], usable[i]['lon']))

        is_last = i == len(usable) - 1
        if seg_dist < min_distance_m and not is_last:
            continue  # keep accumulating until we've covered enough ground

        a, b = usable[anchor], usable[i]
        delta_elev = elevations[i] - elevations[anchor]
        cumulative += seg_dist
        if delta_elev > 0:
            elev_gain += delta_elev
        else:
            elev_loss += -delta_elev

        # Elapsed time from the start of the track (None if timestamps are missing).
        seg_time = b['time']
        elapsed_s = ((seg_time - start_time).total_seconds()
                     if seg_time is not None and start_time is not None else None)

        segments.append({
            'lat1': a['lat'], 'lon1': a['lon'],
            'lat2': b['lat'], 'lon2': b['lon'],
            'path': path,
            'distance_m': seg_dist,
            'elevation_change_m': delta_elev,
            'elevation_m': elevations[i],  # smoothed elevation at the segment end
            'gradient_pct': 100.0 * delta_elev / seg_dist,
            'cumulative_distance_m': cumulative,
            'elapsed_s': elapsed_s,
            'elev_gain_m': elev_gain,
            'elev_loss_m': elev_loss,
        })

        # Start the next segment from the current point.
        anchor = i
        seg_dist = 0.0
        path = [(usable[i]['lat'], usable[i]['lon'])]

    # The final point is force-emitted even if its tail is shorter than
    # min_distance_m; fold that short tail into the previous segment so every
    # segment spans a meaningful baseline (avoids a phantom gradient spike).
    if len(segments) >= 2 and segments[-1]['distance_m'] < min_distance_m:
        tail = segments.pop()
        prev = segments[-1]
        prev['path'] = prev['path'] + tail['path'][1:]
        prev['lat2'], prev['lon2'] = tail['lat2'], tail['lon2']
        prev['distance_m'] += tail['distance_m']
        prev['elevation_change_m'] += tail['elevation_change_m']
        prev['elevation_m'] = tail['elevation_m']
        prev['gradient_pct'] = 100.0 * prev['elevation_change_m'] / prev['distance_m']
        # Cumulative totals on the tail are end-of-ride values; keep them.
        prev['cumulative_distance_m'] = tail['cumulative_distance_m']
        prev['elapsed_s'] = tail['elapsed_s']
        prev['elev_gain_m'] = tail['elev_gain_m']
        prev['elev_loss_m'] = tail['elev_loss_m']
    return segments


def _format_elapsed(seconds):
    """Format elapsed seconds as H:MM:SS (or 'n/a' if unavailable)."""
    if seconds is None:
        return 'n/a'
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def render_map(segments, output_html='trail_map.html', clamp_pct=25.0,
               realistic_max_pct=50.0):
    """Render the trail on satellite imagery, colored by elevation gradient.

    Each segment is drawn as its own colored line so the gradient can vary
    along the trail. Color comes from a diverging scale: blue for descents,
    yellow/green near flat, red for steep climbs. Gradient values are clamped
    to +/- `clamp_pct` so a couple of GPS noise spikes don't dominate the scale.

    Writes an interactive HTML map to `output_html` and returns the path.
    """
    if not segments:
        raise ValueError("No segments to render; check that the file has GPS + elevation data.")

    # Center the map on the middle of the track.
    mid = segments[len(segments) // 2]
    fmap = folium.Map(location=[mid['lat1'], mid['lon1']], zoom_start=14)

    # Esri World Imagery satellite tiles (no API key required).
    folium.TileLayer(
        tiles='https://server.arcgisonline.com/ArcGIS/rest/services/'
              'World_Imagery/MapServer/tile/{z}/{y}/{x}',
        attr='Esri World Imagery',
        name='Satellite',
    ).add_to(fmap)

    # Diverging colormap (RdYlBu reversed): low = blue, mid = yellow, high = red.
    colormap = cm.LinearColormap(
        colors=['#2c7bb6', '#abd9e9', '#ffffbf', '#fdae61', '#d7191c'],
        vmin=-clamp_pct, vmax=clamp_pct,
        caption='Elevation gradient (%)',
    )

    for s in segments:
        clamped = max(-clamp_pct, min(clamp_pct, s['gradient_pct']))
        tooltip = (
            f"Distance: {s['cumulative_distance_m'] / 1000:.2f} km<br>"
            f"Elapsed: {_format_elapsed(s['elapsed_s'])}<br>"
            f"Elevation gained: +{s['elev_gain_m']:.0f} m<br>"
            f"Elevation lost: -{s['elev_loss_m']:.0f} m<br>"
            f"Gradient: {s['gradient_pct']:.1f}%"
        )
        folium.PolyLine(
            locations=s['path'],
            color=colormap(clamped),
            weight=4,
            opacity=0.85,
            tooltip=tooltip,
        ).add_to(fmap)

    start, finish = segments[0], segments[-1]

    # Ride summary, shown when the finish dot is hovered. Steepest climb/descent
    # ignore physically-implausible gradients (GPS-under-canopy artifacts where
    # horizontal distance stalls but elevation keeps changing).
    grades = [s['gradient_pct'] for s in segments]
    realistic = [g for g in grades if abs(g) <= realistic_max_pct]
    excluded = len(grades) - len(realistic)
    if not realistic:  # fall back if the filter removed everything
        realistic = grades
    artifact_note = (f"<br><span style='color:#888'>({excluded} artifact "
                     f"segments excluded)</span>" if excluded else "")
    summary_html = (
        "<div style='font-family:sans-serif;font-size:13px;white-space:nowrap'>"
        "<b>Ride summary</b><br>"
        f"Distance: {finish['cumulative_distance_m'] / 1000:.2f} km<br>"
        f"Total time: {_format_elapsed(finish['elapsed_s'])}<br>"
        f"Elevation gained: +{finish['elev_gain_m']:.0f} m<br>"
        f"Elevation lost: -{finish['elev_loss_m']:.0f} m<br>"
        f"Avg gradient: {sum(realistic) / len(realistic):.1f}%<br>"
        f"Steepest climb: {max(realistic):.1f}%<br>"
        f"Steepest descent: {min(realistic):.1f}%"
        f"{artifact_note}"
        "</div>"
    )

    # Green start dot and red finish dot. The finish dot shows the summary on hover.
    folium.CircleMarker(
        location=[start['lat1'], start['lon1']],
        radius=8, color='white', weight=2,
        fill=True, fill_color='green', fill_opacity=1.0,
    ).add_to(fmap)
    folium.CircleMarker(
        location=[finish['lat2'], finish['lon2']],
        radius=8, color='white', weight=2,
        fill=True, fill_color='red', fill_opacity=1.0,
        tooltip=folium.Tooltip(summary_html, sticky=True),
    ).add_to(fmap)

    # Always-visible "Start"/"Finish" text labels next to each dot.
    for (lat, lon), text, color in [
        ((start['lat1'], start['lon1']), 'Start', '#0a7d00'),
        ((finish['lat2'], finish['lon2']), 'Finish', '#c0140c'),
    ]:
        folium.map.Marker(
            [lat, lon],
            icon=folium.DivIcon(
                icon_size=(0, 0), icon_anchor=(0, 0),
                html=(f"<div style='font-weight:bold;color:{color};"
                      "background:rgba(255,255,255,0.85);padding:1px 5px;border-radius:3px;"
                      "font-family:sans-serif;font-size:12px;display:inline-block;"
                      f"transform:translate(12px,-9px)'>{text}</div>"),
            ),
        ).add_to(fmap)

    colormap.add_to(fmap)
    folium.LayerControl().add_to(fmap)

    # Auto-zoom/pan so the whole trail fits in view.
    lats = [s['lat1'] for s in segments] + [segments[-1]['lat2']]
    lons = [s['lon1'] for s in segments] + [segments[-1]['lon2']]
    fmap.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]])

    fmap.save(output_html)
    return output_html


def plot_elevation_profile(segments, output_png='elevation_profile.png', clamp_pct=25.0):
    """Plot elevation vs. distance, with the line colored by gradient.

    Uses the same diverging color scale as the map (blue = descent,
    yellow = flat, red = climb), clamped to +/- `clamp_pct`. Writes a PNG to
    `output_png` and returns the path.
    """
    import numpy as np
    import matplotlib
    matplotlib.use('Agg')  # headless backend: render to file, no GUI window
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection

    if not segments:
        raise ValueError("No segments to plot.")

    x = np.array([s['cumulative_distance_m'] / 1000 for s in segments])  # km
    y = np.array([s['elevation_m'] for s in segments])                    # meters
    grades = np.array([s['gradient_pct'] for s in segments])

    # Build line pieces between consecutive points, colored by gradient.
    pts = np.array([x, y]).T.reshape(-1, 1, 2)
    line_segs = np.concatenate([pts[:-1], pts[1:]], axis=1)
    lc = LineCollection(line_segs, cmap='RdYlBu_r',
                        norm=plt.Normalize(-clamp_pct, clamp_pct))
    lc.set_array(grades[1:])  # color each piece by the gradient at its end
    lc.set_linewidth(2)

    fig, ax = plt.subplots(figsize=(12, 4))
    ax.add_collection(lc)
    ax.fill_between(x, y, y.min(), color='0.92', zorder=0)  # subtle terrain fill
    ax.set_xlim(x.min(), x.max())
    ax.set_ylim(y.min() - 10, y.max() + 10)
    ax.set_xlabel('Distance (km)')
    ax.set_ylabel('Elevation (m)')
    ax.set_title('Elevation profile')
    ax.grid(True, alpha=0.3)

    cbar = fig.colorbar(lc, ax=ax, pad=0.01)
    cbar.set_label('Gradient (%)')

    fig.tight_layout()
    fig.savefig(output_png, dpi=120)
    plt.close(fig)
    return output_png


def main():
    # Use the filename passed on the command line, or fall back to a default.
    filename = sys.argv[1] if len(sys.argv) > 1 else 'Cougar_Ridge_mountain_bike_ride_4265.gpx'
    points = parse_file(filename)
    # print(f"Parsed {len(points)} points from {filename}")
    # for point in points[:10]:  # Print the first 10 points to verify
    #     print(f"Lat: {point['lat']}, Lon: {point['lon']}, Elevation: {point['elevation']} m, Time: {point['time']}")

    segments = compute_gradients(points, smooth_window=5, min_distance_m=8.0)
    if segments:
        grades = [s['gradient_pct'] for s in segments]
        total_km = segments[-1]['cumulative_distance_m'] / 1000
        print(f"\nComputed {len(segments)} segments over {total_km:.2f} km")
        print(f"Gradient %: min {min(grades):.1f}, max {max(grades):.1f}, "
              f"avg {sum(grades) / len(grades):.1f}")

        # Name the map after the input file, e.g. Cougar_Ridge.gpx -> Cougar_Ridge.html
        output_html = os.path.splitext(os.path.basename(filename))[0] + '.html'
        output = render_map(segments, output_html)
        print(f"Saved interactive map to {os.path.abspath(output)}")

        profile_png = os.path.splitext(os.path.basename(filename))[0] + '_profile.png'
        profile = plot_elevation_profile(segments, profile_png)
        print(f"Saved elevation profile to {os.path.abspath(profile)}")


if __name__ == "__main__":
    main()
