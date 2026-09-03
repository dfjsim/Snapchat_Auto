# Blurred text on scaled displays and over RDP

## The symptom

The GUI's text is soft and slightly smeared — not the wrong size, and not the wrong font: the right
window, out of focus. It shows up on a laptop set to 125% or 150%, and most obviously over RDP when
the client machine's scaling differs from the scaling the remote session was started at. Connect to
the same session from a client at 100% and the window is sharp again.

## Why

Nothing is wrong with the fonts. The window is a **bitmap that Windows enlarged**.

A Python process is DPI *unaware* by default. That is deliberate on CPython's part: its manifest
does not claim awareness, because Tk cannot rescale itself, so IDLE and every other Tk front end
either ask for awareness at run time or do without. Windows handles an unaware process by drawing
it at 96 dpi into an off-screen surface and then stretching that surface to the display's real
scale. At 100% there is nothing to stretch. At 125% every glyph is a 1.25x enlargement of a bitmap
that was rendered for a smaller screen, which is exactly what "blurry" means.

The RDP case is the same mechanism with one extra step. A modern client tells the session what
scale it is running at, and the session's display DPI follows — that is why the blur appears and
disappears with the client's scaling and not with the session's original resolution.

Confirmed on this project before the fix: `GetProcessDpiAwareness` returned `0`
(`PROCESS_DPI_UNAWARE`), and Tk reported 96 dpi regardless of the display.

## The fix, in two halves

Claiming awareness stops the stretch, but it hands back the job Windows was doing: the toolkit now
has to scale itself, and Tk 8.6 does not do it on its own. Both halves are needed, and either one
alone is worse than neither.

`scripts/hidpi.py` owns both.

**Fonts.** Font sizes are given in *points*, and Tk turns points into pixels with `tk scaling` —
pixels per point. `hidpi.tk_scaling()` returns `dpi / 72`, and `apply_theme` passes it to
FreeSimpleGUI's `set_options(scaling=…)`, so every window built afterwards gets it. An 11pt font
that was 17 pixels tall at 96 dpi is then 21 pixels at 120 dpi — drawn at that size, not enlarged
to it. Note the units: handing Tk the *ratio* (1.25) instead of `dpi / 72` would leave the text a
quarter too small.

**Pixels.** Window minimums and the scrolling viewport are pixel counts written for a 100% display,
while `Window.get_screen_size()` reports real ones. With only the font half done, the text is crisp
and the form opens already scrolled on a screen with room to spare — the content grew and the box
holding it did not. `hidpi.px()` / `px2()` scale those constants at the point of use, so a window
rebuilt after a DPI change picks up the new one.

`hidpi.enable()` asks for **Per-Monitor v2** first, falling back through per-monitor and system
awareness to the Vista-era call. Per-monitor first because it is the only mode whose reported DPI
follows a display that changes underneath a running session, which is what an RDP reconnect is; the
system-aware modes are told the DPI once, at process start, and are stretched from then on if it
changes. It is called at import in `Snapchat_Auto.py`, before any window exists — Tk reads the
screen's DPI when its first interpreter is created, and a claim made after that is too late.

Everything in the module is Windows-only, stdlib-only and non-fatal. An extraction must not fail
because a cosmetic API was missing or a policy blocked it; off Windows, and wherever the calls do
not work, the scale is 1.0 and nothing moves.

## FreeSimpleGUI's own DPI option does not work on Windows 11

`sg.set_options(dpi_awareness=True)` looks like it should be the whole fix. It is a no-op here:

```python
if dpi_awareness is True:
    if running_windows():
        if platform.release() == '7':
            ctypes.windll.user32.SetProcessDPIAware()
        elif platform.release() == '8' or platform.release() == '10':
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
```

On Windows 11 `platform.release()` returns `'11'`, so no branch is taken and the call is silently
skipped. (It would also only ever ask for *system* awareness, which is the mode that stops tracking
the display after process start.) Its `scaling=` parameter, on the other hand, does work — it
reaches `tk scaling` on each window's root — and is what this project uses.

## What it does not fix

Reconnecting to a session at a **different** scale while a window is already open. Under
Per-Monitor v2 Windows will not stretch the window, and Tk 8.6 ignores `WM_DPICHANGED`, so the
window keeps its old pixel size: sharp, but physically smaller or larger than it should be. It
costs nothing to live with here — the settings window is the only one open for any length of time,
and closing and reopening it (or cycling the appearance button, which rebuilds it) re-reads the DPI.
A run itself has no window.

## The switches

Everything above is automatic. It is also adjustable, for a reason worth stating: **"sharper" is a
judgement made by eye, and the eye has nothing to compare against once a build has shipped.** The
first person to ask whether this actually helped could not answer it, because the binary that
rendered the old way was gone. So the old way is a switch rather than a deletion.

| | |
|---|---|
| `--dpi-awareness auto` | the default: Per-Monitor v2, falling back through per-monitor to system |
| `--dpi-awareness system` | sharp at the DPI the process started at, stretched if it later changes |
| `--dpi-awareness unaware` | claim nothing — reproduces the pre-1.6 rendering, in the current build |
| `--dpi-scale 125` | lay the GUI out for this scale whatever the display says; `125`, `125%` and `1.25` all mean the same, 50–400 |
| `--dpi-report` | print what the display, the session and Tk each report, and exit |

They are modifiers, not commands: add them to a headless run or use them alone with the GUI. Because
the application dispatches on its *first* argument only, `hidpi.strip_options` removes them before
that dispatch runs — otherwise `--dpi-scale 150` on its own would be read as an unknown command and
print the usage instead of opening the window.

Each is also read from the environment (`SNAPCHAT_AUTO_DPI_AWARENESS`, `SNAPCHAT_AUTO_DPI_SCALE`)
and from `dpi_awareness` / `dpi_scale` in `~/.snapchat_auto_gui.json`, in that order of precedence
after the command line.

## The GUI control

`--dpi-scale` exists for its own sake as well as for testing — it decides the text size
independently of what Windows was set to, which is what somebody wants when the machine's scaling
is not the size they want to read a forensic report at. So it is also a control on the form, beside
the theme button: **Text size**, offering `Auto` (follow the display) and 100% through 200%.

Three things make it one setting rather than two:

* it writes the same `dpi_scale` key the command line writes, which `hidpi.configure` reads back
  before the *next* run's first window — so choosing a size is remembered without any other
  machinery;
* it is saved *before* the rebuild, like the appearance, so the choice survives an examiner who then
  cancels out of the form;
* the control reads its own value from `hidpi.forced()`, not from the config, because `--dpi-scale`
  and the environment can force a size the config has never heard of. A control that disagreed with
  the window it sits in would be worse than no control. A forced size the preset list does not offer
  is added to the list rather than rounded to the nearest one it knows.

Choosing a size **rebuilds the window**, exactly as the theme button does and for a related reason:
the size reaches each widget through `tk scaling`, which is read when that widget is created, so an
existing window cannot be re-scaled — it has to be built again at the new size.

Awareness is deliberately *not* on the form. It can only be claimed once per process, so changing it
would mean a restart, and it is a diagnostic switch rather than a preference.

## Checking it

The startup log states what was resolved and why, which is the fastest way to read a screenshot from
an examiner's machine:

```
[INFO] Display: 120 dpi (125%), awareness 2 (auto)
[INFO] Display: 144 dpi (150%), awareness 2 (auto), scale forced by --dpi-scale (display reports 96 dpi)
```

`awareness 0` is the stretched case and the whole diagnosis. `1` is system-aware. `2` is
per-monitor, which is what this should normally report. The second line is why a surprising text
size is traceable to the switch that caused it rather than blamed on the display.

`--dpi-report` is the fuller version, and the thing to ask for when text looks soft. It prints the
framebuffer, then the unaware and the aware readings side by side, then what Tk does with them —
including the pixel height the GUI's 11 pt font will actually have. **Comparing the two
`GetSystemMetrics` lines against the framebuffer is the diagnosis:**

* unaware is told less than the framebuffer, aware is told the framebuffer → the display is scaled,
  the session is handling it correctly, and awareness is the fix. This is the ordinary case.
* *both* are less than the framebuffer, or the framebuffer is smaller than the window the RDP client
  shows → the enlargement is happening on the client, after the pixels have left the session, and no
  application can reach it. Sign *out* (not just disconnect) and reconnect so a new session is
  created at the client's scale.
* both equal the framebuffer at 96 dpi → nothing is being scaled. Softness there is the RDP codec or
  font smoothing, neither of which is an application setting.

`tests/test_hidpi.py` covers the arithmetic, the option parsing and the promise that none of it can
take a run down; `tests/test_gui_window.py` covers the geometry that has to scale with it.
