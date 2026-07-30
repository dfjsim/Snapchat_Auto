"""
Offline map tiles for geolocated artifacts.

Examiner machines are usually offline, and a forensic report must not reach out to the internet on
its own — so map imagery is only produced when the examiner explicitly points the tool at a **tile
server they run themselves** (an offline OpenStreetMap/OSM-style XYZ server). Nothing here runs
unless a server URL is configured, and no request is ever made to a public host by default.

What it does:

* :func:`normalize_template` accepts either a bare server root (``http://localhost:8080``) or a full
  XYZ template (``http://host/tiles/{z}/{x}/{y}.png``) and returns a usable template;
* :func:`test_server` fetches one tile so the GUI can tell the examiner straight away whether the
  server answers (and with what);
* :class:`TileFetcher` stitches a small static map around a coordinate — the tiles are cached in
  memory, so a whole gallery of memories in the same neighbourhood costs a handful of requests.

The rendered map is a **derived artifact**: it is imagery from the examiner's own tile server with a
marker drawn at the recovered coordinates, not data recovered from the device. Reports label it so.
"""

import io
import re
import math
import logging
import urllib.error
import urllib.request

from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)

DEFAULT_ZOOM = 15
DEFAULT_TILES = 3                                          # 3x3 tiles => 768x768 px
TILE_PX = 256
USER_AGENT = "Snapchat_Auto (offline forensic report generator)"


def normalize_template(url):
    """Return an XYZ tile template from what the examiner typed, or None if it is unusable.

    Accepts a full template (must contain ``{z}``, ``{x}`` and ``{y}``) or a server root, in which
    case the conventional ``/{z}/{x}/{y}.png`` layout is appended.
    """
    url = (url or "").strip()
    if not url or not re.match(r"^https?://", url, re.I):
        return None
    if "{z}" in url and "{x}" in url and "{y}" in url:
        return url
    if "{" in url:                                         # a template, but not one we understand
        return None
    return url.rstrip("/") + "/{z}/{x}/{y}.png"


def viewer_url(template, lat, lon, zoom=DEFAULT_ZOOM):
    """A link to the tile server itself, centred on the coordinates (OSM ``#map=z/lat/lon``)."""
    root = template.split("{")[0].rstrip("/")
    if root.endswith("/tiles"):
        root = root[:-len("/tiles")]
    return f"{root}/#map={zoom}/{lat:.5f}/{lon:.5f}"


def tile_url(template, z, x, y):
    return template.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))


def _get(url, timeout):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(), response.headers.get("Content-Type", "")


def test_server(url, timeout=8, zoom=1):
    """Fetch one tile. Returns ``(ok, message)`` — the message is shown to the examiner as-is."""
    template = normalize_template(url)
    if not template:
        return False, ("That does not look like a tile server URL. Give either the server root "
                       "(http://host:port) or a full http(s) template containing {z}, {x} and {y} "
                       "— e.g. http://localhost:8080/tiles/{z}/{x}/{y}.png")
    probe = tile_url(template, zoom, 0, 0)
    try:
        data, content_type = _get(probe, timeout)
    except urllib.error.HTTPError as error:
        return False, f"{probe} answered HTTP {error.code} ({error.reason})."
    except Exception as error:                             # URLError, socket timeout, bad host…
        return False, f"Could not reach {probe}: {error}"
    if not data:
        return False, f"{probe} returned an empty response."
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception:
        head = data[:40].decode("utf-8", "replace")
        return False, (f"{probe} answered {len(data)} bytes of {content_type or 'unknown type'}, "
                       f"which is not an image (starts with: {head!r}).")
    return True, (f"OK — {probe} returned a {image.width}x{image.height} {image.format} tile. "
                  f"Maps will be rendered from this server.")


def _lat_lon_to_tile(lat, lon, zoom):
    """Web-Mercator tile coordinates (fractional, so the exact pixel of a point is known)."""
    n = 2.0 ** zoom
    lat = max(min(lat, 85.05112878), -85.05112878)
    rad = math.radians(lat)
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.log(math.tan(rad) + 1.0 / math.cos(rad)) / math.pi) / 2.0 * n
    return x, y


class TileFetcher:
    """Renders small static maps from one tile server, caching tiles across calls."""

    def __init__(self, url, timeout=8):
        self.template = normalize_template(url)
        self.timeout = timeout
        self.cache = {}
        self.failed = False                                # stop hammering a server that is down
        self.errors = 0

    def ok(self):
        return bool(self.template) and not self.failed

    def _tile(self, z, x, y):
        key = (z, x, y)
        if key in self.cache:
            return self.cache[key]
        image = None
        try:
            data, _ = _get(tile_url(self.template, z, x, y), self.timeout)
            image = Image.open(io.BytesIO(data)).convert("RGB")
        except Exception as error:
            self.errors += 1
            logger.debug(f"Tile {z}/{x}/{y} failed: {error}")
            if self.errors >= 8:                           # server gone: give up for this run
                self.failed = True
                logger.warning("Offline tile server stopped answering — maps will be incomplete")
        self.cache[key] = image
        return image

    def static_map(self, lat, lon, out_path, zoom=DEFAULT_ZOOM, tiles=DEFAULT_TILES):
        """Write a ``tiles x tiles`` map centred on (lat, lon) with a marker. Returns a dict of
        what was rendered (for the report caption), or None when nothing could be fetched."""
        if not self.ok():
            return None
        fx, fy = _lat_lon_to_tile(lat, lon, zoom)
        half = tiles // 2
        cx, cy = int(math.floor(fx)), int(math.floor(fy))
        span = 2 ** zoom
        canvas = Image.new("RGB", (tiles * TILE_PX, tiles * TILE_PX), (238, 238, 238))
        got = 0
        for dx in range(-half, half + 1):
            for dy in range(-half, half + 1):
                x, y = (cx + dx) % span, cy + dy
                if not 0 <= y < span:
                    continue
                tile = self._tile(zoom, x, y)
                if tile is None:
                    continue
                canvas.paste(tile, ((dx + half) * TILE_PX, (dy + half) * TILE_PX))
                got += 1
        if not got:
            return None
        # the coordinate's exact pixel inside the stitched canvas
        px = int((fx - cx + half) * TILE_PX)
        py = int((fy - cy + half) * TILE_PX)
        draw = ImageDraw.Draw(canvas)
        draw.line([(px - 14, py), (px + 14, py)], fill=(255, 255, 255), width=5)
        draw.line([(px, py - 14), (px, py + 14)], fill=(255, 255, 255), width=5)
        draw.line([(px - 14, py), (px + 14, py)], fill=(200, 20, 40), width=2)
        draw.line([(px, py - 14), (px, py + 14)], fill=(200, 20, 40), width=2)
        draw.ellipse([px - 7, py - 7, px + 7, py + 7], outline=(200, 20, 40), width=3)
        try:
            canvas.save(out_path, "PNG", optimize=True)
        except OSError as error:
            logger.debug(f"Could not write {out_path}: {error}")
            return None
        return {"zoom": zoom, "tiles": tiles, "fetched": got, "expected": tiles * tiles,
                "center": (lat, lon), "template": self.template,
                "viewer": viewer_url(self.template, lat, lon, zoom)}
