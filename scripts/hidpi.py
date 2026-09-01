"""Windows DPI awareness, the scaling that has to follow it, and the switches that control both.

A Python process is DPI **unaware** by default — CPython's manifest deliberately does not claim
otherwise, because Tk cannot rescale itself, so IDLE and every other Tk front end ask for awareness
at run time or not at all. Windows renders an unaware process at 96 dpi and then stretches the
result as a bitmap up to whatever the display is really running at. That stretch is what "blurry
text" is. On a 100% desktop there is nothing to stretch and nobody notices; on a 125% display — a
scaled laptop, or an RDP session whose client is scaled — the whole window is a 1.25x enlargement
of a 96-dpi bitmap, every glyph included.

Claiming awareness stops the stretch, but it hands back the problem Windows was solving for us: the
toolkit now has to do the scaling itself, and it does not do it on its own. Two halves, both needed:

* **fonts** are given in *points*, and Tk turns points into pixels with ``tk scaling``. Setting that
  from the real DPI is what makes the text grow to the size it should always have had — sharply,
  because it is drawn at that size rather than enlarged after the fact.
* **pixels** — window minimums, the scrolling viewport — are 96-dpi numbers written for a 100%
  display, and have to be multiplied by :func:`scale`. Miss this half and the text is crisp but the
  form opens already scrolled, because the content grew and the box holding it did not.

FreeSimpleGUI's own ``set_options(dpi_awareness=True)`` cannot be used for the first half: it tests
``platform.release()`` against "7", "8" and "10", and Python reports "11" on Windows 11, so the call
is silently skipped and nothing happens. Its ``scaling=`` option does work, and :func:`tk_scaling`
is what ``apply_theme`` passes to it.

**Why any of this is adjustable.** "Sharper" is a judgement made by eye, and the eye has nothing to
compare against once a build has shipped — which is exactly the position an examiner is in when the
old binary is gone. ``--dpi-awareness unaware`` reproduces the pre-fix rendering in the *current*
build, so the two can be put side by side, and ``--dpi-scale`` decides the text size independently
of what the display claims. Both also serve the ordinary case of a machine whose scaling is set to
something the examiner does not actually want to read at.

Stdlib only, Windows only, and never fatal — an examiner's run must not fail because a cosmetic API
is missing or a policy has locked it down.
"""

import ctypes
import ctypes.wintypes as wintypes
import json
import logging
import os
import sys

logger = logging.getLogger(__name__)

#: The DPI Windows calls 100%. Every pixel constant in the GUI is a measurement at this DPI.
BASE_DPI = 96

#: What ``--dpi-awareness`` accepts. "auto" is "the best this Windows offers", which is what the
#: application wants; the rest exist to reproduce and compare the other behaviours.
AWARENESS_MODES = ("auto", "permonitor", "system", "unaware")

#: Environment equivalents of the two options, for launching from a shortcut or a service wrapper.
MODE_ENV = "SNAPCHAT_AUTO_DPI_AWARENESS"
SCALE_ENV = "SNAPCHAT_AUTO_DPI_SCALE"

#: The range a forced scale is accepted in, as a percentage. Outside it the GUI could not be
#: operated at all, and a typo ("--dpi-scale 1250") should not be able to produce that.
MIN_SCALE, MAX_SCALE = 50, 400

#: DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2, passed as a pseudo-handle.
_PER_MONITOR_AWARE_V2 = -4
#: PROCESS_DPI_AWARENESS values for the older SetProcessDpiAwareness.
_PROCESS_PER_MONITOR_DPI_AWARE = 2
_PROCESS_SYSTEM_DPI_AWARE = 1

_MONITOR_DEFAULTTOPRIMARY = 1
_MDT_EFFECTIVE_DPI = 0
_SM_CXSCREEN, _SM_CYSCREEN, _SM_REMOTESESSION = 0, 1, 0x1000
_ENUM_CURRENT_SETTINGS = -1

#: Set by --dpi-scale: the DPI to use instead of the one the display reports. None means "ask".
_forced_dpi = None


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _on_windows():
    return sys.platform == "win32"


# --------------------------------------------------------------------------- claiming awareness

def enable(mode="auto"):
    """Claim DPI awareness for this process. Returns the awareness it ended up with.

    *mode* is one of :data:`AWARENESS_MODES`:

    ``auto`` / ``permonitor``
        Per-Monitor v2, falling back through per-monitor to system awareness. Per-monitor first
        because it is the only mode whose reported DPI follows a display that changes underneath a
        running session — which is what an RDP reconnect is; the system modes are told the DPI once,
        at process start, and are stretched from then on if it changes.
    ``system``
        System awareness only. Sharp at the DPI the process started at, stretched afterwards.
    ``unaware``
        Claim nothing, which is what the application did before any of this existed. Kept as a
        switch rather than deleted: it is the only way to see the stretched rendering next to the
        native one without going back for an older binary.

    Call once, before the first window is built: Tk reads the screen's DPI when its first
    interpreter is created, and awareness claimed after that point is too late. A process that was
    already given an awareness (by a manifest) fails every call with E_ACCESSDENIED, which is the
    desired state already, not an error.
    """
    if not _on_windows():
        return None
    if mode == "unaware":
        return awareness()
    if mode in ("auto", "permonitor"):
        try:                                                # Windows 10 1703 and later
            if ctypes.windll.user32.SetProcessDpiAwarenessContext(
                    ctypes.c_void_p(_PER_MONITOR_AWARE_V2)):
                return awareness()
        except (AttributeError, OSError) as error:
            logger.debug(f"SetProcessDpiAwarenessContext is unavailable: {error}")
        try:                                                # Windows 8.1 and later
            if ctypes.windll.shcore.SetProcessDpiAwareness(_PROCESS_PER_MONITOR_DPI_AWARE) == 0:
                return awareness()
        except (AttributeError, OSError) as error:
            logger.debug(f"SetProcessDpiAwareness(per-monitor) failed: {error}")
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(_PROCESS_SYSTEM_DPI_AWARE) == 0:
            return awareness()
    except (AttributeError, OSError) as error:
        logger.debug(f"SetProcessDpiAwareness(system) failed: {error}")
    try:                                                    # Vista and later: system-aware only
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError) as error:
        logger.debug(f"SetProcessDPIAware failed: {error}")
    return awareness()


def awareness():
    """0 unaware, 1 system-aware, 2 per-monitor aware; None where it cannot be read.

    Logged at startup rather than acted on: 0 is the one value that means the window will be a
    stretched bitmap on any display that is not at 100%.
    """
    if not _on_windows():
        return None
    value = ctypes.c_int()
    try:
        if ctypes.windll.shcore.GetProcessDpiAwareness(0, ctypes.byref(value)) == 0:
            return value.value
    except (AttributeError, OSError) as error:
        logger.debug(f"Could not read this process's DPI awareness: {error}")
    return None


# --------------------------------------------------------------------------- what the display is

def dpi():
    """The DPI to lay the GUI out for: what ``--dpi-scale`` forced, else what the display reports.

    ``GetDpiForMonitor`` is asked first: it is the per-monitor accurate figure, and the one that
    tracks an RDP session reconnected at a different scale. ``GetDpiForSystem`` is a session-wide
    value fixed when the process started, and is the fallback. An **unaware** process is told 96 by
    both, which is the right answer for it — it really is being rendered at 96 and stretched
    afterwards, so scaling on top of that would double-size a form that is already correct.
    """
    if _forced_dpi:
        return _forced_dpi
    if not _on_windows():
        return BASE_DPI
    try:
        user32 = ctypes.windll.user32
        user32.MonitorFromPoint.argtypes = [_POINT, ctypes.c_ulong]
        user32.MonitorFromPoint.restype = ctypes.c_void_p
        monitor = user32.MonitorFromPoint(_POINT(0, 0), _MONITOR_DEFAULTTOPRIMARY)
        shcore = ctypes.windll.shcore
        shcore.GetDpiForMonitor.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                            ctypes.POINTER(ctypes.c_uint),
                                            ctypes.POINTER(ctypes.c_uint)]
        x, y = ctypes.c_uint(), ctypes.c_uint()
        if shcore.GetDpiForMonitor(monitor, _MDT_EFFECTIVE_DPI,
                                   ctypes.byref(x), ctypes.byref(y)) == 0 and x.value:
            return x.value
    except (AttributeError, OSError, ValueError) as error:
        logger.debug(f"GetDpiForMonitor is unavailable: {error}")
    try:
        return ctypes.windll.user32.GetDpiForSystem() or BASE_DPI
    except (AttributeError, OSError) as error:
        logger.debug(f"GetDpiForSystem is unavailable: {error}")
    return BASE_DPI


def force_dpi(value):
    """Lay the GUI out for *value* dpi whatever the display says. None restores "ask the display"."""
    global _forced_dpi
    _forced_dpi = value


def forced():
    """The DPI ``--dpi-scale`` forced, or None. Reported at startup so a surprising size is
    traceable to the switch that caused it rather than blamed on the display."""
    return _forced_dpi


def scale():
    """What a pixel measurement written for a 100% display has to be multiplied by."""
    return dpi() / BASE_DPI


def px(value):
    """*value*, a pixel count written at 100%, at the display's real scale."""
    return int(round(value * scale()))


def px2(pair):
    """A ``(width, height)`` pair of 100% pixel counts, at the display's real scale."""
    return (px(pair[0]), px(pair[1]))


def tk_scaling():
    """The value for Tk's ``tk scaling``: pixels per *point*, which is what sizes every font.

    Set explicitly rather than left to Tk. Tk derives its own figure from the screen dimensions when
    the interpreter starts, and what those report depends on the awareness mode the process happened
    to end up in; this does not, and it is also how ``--dpi-scale`` reaches the fonts.
    """
    return dpi() / 72.0


# --------------------------------------------------------------------------- the switches

def parse_scale(text):
    """A scale written as ``125``, ``"125%"`` or ``1.25`` -> the DPI it means, else None.

    Both spellings are accepted because both are what people mean by "125": Windows shows a
    percentage, and everything inside this module is a factor.
    """
    try:
        value = float(str(text).strip().rstrip("%").replace(",", "."))
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    percent = value * 100 if value <= 8 else value           # 1.25 and 125 mean the same thing
    if not MIN_SCALE <= percent <= MAX_SCALE:
        return None
    return int(round(BASE_DPI * percent / 100))


def strip_options(argv):
    """Pull the ``--dpi-*`` options out of *argv*. Returns ``(options, remaining)``.

    Pure — it claims nothing and sets nothing, so the same parse can be run twice. It has to be
    separable because the application dispatches on its *first* argument only: without removing
    these first, ``Snapchat_Auto.exe --dpi-scale 150`` would be read as an unknown command and
    print the usage instead of opening the window it was asked for.
    """
    options, remaining, index = {}, [], 0
    while index < len(argv):
        token = argv[index]
        name, _, inline = str(token).partition("=")
        key = name.lstrip("-/").lower().replace("_", "-")
        if key == "dpi-report":
            options["report"] = True
        elif key in ("dpi-awareness", "dpi-scale"):
            value = inline
            if not value and index + 1 < len(argv):          # "--dpi-scale 150", not "=150"
                index += 1
                value = argv[index]
            options[key[len("dpi-"):]] = value
        else:
            remaining.append(token)
        index += 1
    return options, remaining


def _saved(config_path, key):
    """*key* from the GUI's saved settings, or None. Read directly rather than through the
    application's own loader: this runs before the first window, which is earlier than that."""
    if not config_path:
        return None
    try:
        with open(config_path, encoding="utf-8") as handle:
            return json.load(handle).get(key)
    except (OSError, ValueError) as error:
        logger.debug(f"No saved DPI settings in {config_path}: {error}")
        return None


def configure(argv=None, config_path=None):
    """Apply this process's DPI settings and return what was applied, for the startup log.

    Precedence, most specific first: the command line, the environment (:data:`MODE_ENV` and
    :data:`SCALE_ENV`), the saved GUI settings (``dpi_awareness`` / ``dpi_scale``), the default.
    The saved settings are what makes this usable by somebody who will never type a flag.

    Call once, before the first window exists. ``--dpi-report`` deliberately claims *nothing* here:
    the report's whole value is showing the unaware and the aware numbers side by side, and
    awareness can only be claimed once per process.
    """
    options, _ = strip_options(sys.argv[1:] if argv is None else argv)

    mode = (options.get("awareness") or os.environ.get(MODE_ENV)
            or _saved(config_path, "dpi_awareness") or "auto")
    mode = str(mode).strip().lower()
    if mode not in AWARENESS_MODES:
        logger.warning(f"Unknown DPI awareness {mode!r}; using 'auto'. "
                       f"One of: {', '.join(AWARENESS_MODES)}.")
        mode = "auto"

    asked = options.get("scale") or os.environ.get(SCALE_ENV) or _saved(config_path, "dpi_scale")
    if asked not in (None, ""):
        if (value := parse_scale(asked)) is None:
            logger.warning(f"Ignoring the DPI scale {asked!r}: give a percentage between "
                           f"{MIN_SCALE} and {MAX_SCALE}, or a factor: 125, '125%' or 1.25.")
        else:
            force_dpi(value)

    report = bool(options.get("report"))
    return {"mode": mode, "report": report,
            "awareness": awareness() if report else enable(mode),
            "forced": forced()}


def describe(settings):
    """The one-line startup log: what the display is, and every switch that changed it."""
    parts = [f"{dpi()} dpi ({scale():.0%})",
             f"awareness {settings.get('awareness')} ({settings.get('mode', 'auto')})"]
    if settings.get("forced"):
        parts.append(f"scale forced by --dpi-scale (display reports "
                     f"{_display_dpi_unforced()} dpi)")
    return "Display: " + ", ".join(parts)


def _display_dpi_unforced():
    """What the display says, ignoring any forced scale — for the log line above."""
    global _forced_dpi
    keep, _forced_dpi = _forced_dpi, None
    try:
        return dpi()
    finally:
        _forced_dpi = keep


# --------------------------------------------------------------------------- --dpi-report

class _DEVMODEW(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", wintypes.WCHAR * 32),
        ("dmSpecVersion", wintypes.WORD), ("dmDriverVersion", wintypes.WORD),
        ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD),
        ("dmFields", wintypes.DWORD),
        # the printer/display union: eight shorts, the same 16 bytes as POINTL + two DWORDs
        ("dmUnion1", ctypes.c_short * 8),
        ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short),
        ("dmFormName", wintypes.WCHAR * 32),
        ("dmLogPixels", wintypes.WORD),
        ("dmBitsPerPel", wintypes.DWORD),
        ("dmPelsWidth", wintypes.DWORD), ("dmPelsHeight", wintypes.DWORD),
        ("dmDisplayFlags", wintypes.DWORD), ("dmDisplayFrequency", wintypes.DWORD),
        ("dmICMMethod", wintypes.DWORD), ("dmICMIntent", wintypes.DWORD),
        ("dmMediaType", wintypes.DWORD), ("dmDitherType", wintypes.DWORD),
        ("dmReserved1", wintypes.DWORD), ("dmReserved2", wintypes.DWORD),
        ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD),
    ]


def _framebuffer():
    """The desktop's real mode, from the display driver. No DPI awareness can change it, which is
    what makes it the reference the two GetSystemMetrics readings are compared against."""
    mode = _DEVMODEW()
    mode.dmSize = ctypes.sizeof(_DEVMODEW)
    if ctypes.windll.user32.EnumDisplaySettingsW(None, _ENUM_CURRENT_SETTINGS, ctypes.byref(mode)):
        return f"{mode.dmPelsWidth}x{mode.dmPelsHeight}"
    return "unreadable"


def _metrics(label):
    user32 = ctypes.windll.user32
    lines = [f"  process DPI awareness   {awareness()}",
             f"  GetDpiForSystem         {_safe(user32.GetDpiForSystem)}",
             f"  GetDpiForMonitor        {_display_dpi_unforced()}",
             f"  screen per GetSystemMetrics  {user32.GetSystemMetrics(_SM_CXSCREEN)}x"
             f"{user32.GetSystemMetrics(_SM_CYSCREEN)}   <- what a {label} process is told"]
    return "\n".join(lines)


def _safe(function, *args):
    try:
        return function(*args)
    except Exception as error:                              # noqa: BLE001 - an absent API
        return f"unavailable ({error})"


def _tk_lines():
    """What Tk will actually do with all of the above — the numbers that decide how text looks."""
    try:
        import tkinter
        import tkinter.font as tkfont
        root = tkinter.Tk()
        root.withdraw()
        root.tk.call("tk", "scaling", tk_scaling())
        font = tkfont.Font(root=root, family="Helvetica", size=11)
        lines = [f"  winfo screen            {root.winfo_screenwidth()}x{root.winfo_screenheight()}",
                 f"  winfo fpixels 1i        {root.winfo_fpixels('1i'):.1f} dpi",
                 f"  tk scaling (applied)    {tk_scaling():.4f}",
                 f"  the GUI's 11pt font     {font.metrics('linespace')} px tall, "
                 f"as {font.actual('family')}"]
        root.destroy()
        return "\n".join(lines)
    except Exception as error:                              # noqa: BLE001 - no display, no Tk
        return f"  unavailable: {error}"


def print_report():
    """``--dpi-report``: everything needed to tell where a soft-looking window is being softened.

    Printed rather than logged, and it claims awareness itself half way through so that the before
    and the after can be read off one page. Returns an exit code.
    """
    if not _on_windows():
        print("DPI scaling is a Windows concern; nothing to report here.")
        return 0
    user32 = ctypes.windll.user32
    print("=" * 78)
    print(f"Python                    {sys.version.split()[0]}")
    print(f"Remote (RDP) session      {bool(user32.GetSystemMetrics(_SM_REMOTESESSION))}")
    print(f"framebuffer               {_framebuffer()}   <- the real pixels the desktop draws")
    if forced():
        print(f"--dpi-scale              forcing {forced()} dpi ({scale():.0%})")
    print("=" * 78)
    print("\nAs launched, claiming nothing (--dpi-awareness unaware):")
    print(_metrics("DPI-unaware"))
    print("\nAfter claiming the best this Windows offers (--dpi-awareness auto, the default):")
    enable("auto")
    print(_metrics("DPI-aware"))
    print("\nWhat Tk then does, which is what decides how the text looks:")
    print(_tk_lines())
    print("""
------------------------------------------------------------------------------
Reading it - compare the two "screen per GetSystemMetrics" lines to the framebuffer

* unaware is told LESS than the framebuffer, aware is told the framebuffer
      The display is scaled and the session is handling it correctly. An unaware
      process draws into the smaller size and Windows enlarges the result to fill
      the framebuffer -- that enlargement IS the blur, and claiming awareness is
      the fix. Run with --dpi-awareness unaware to see the old rendering again.

* BOTH lines are less than the framebuffer, or the framebuffer is smaller than
  the window the RDP client shows
      The enlargement is happening on the client, after the pixels have left this
      session, and no application can reach it. Fix it at the connection: sign OUT
      (not just disconnect) and reconnect, so a new session is created at the
      client's scale.

* both lines equal the framebuffer and the DPI is 96
      Nothing is being scaled. Text will be small but sharp; softness here is the
      RDP codec or font smoothing, not scaling -- neither is an application
      setting. Raise the connection quality instead.
------------------------------------------------------------------------------""")
    return 0
