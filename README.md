# 🐱 Desktop Cat

A lightweight pixel companion that sits atop your taskbar or Dock to encourage healthy desk habits. It **tracks desk time to prompt movement breaks**, **reminds you to hydrate, eat lunch, and log off**, **fetches local files and apps in milliseconds**, and **runs a visual cleanup animation** while clearing temporary files and the system recycle bin.

Single Python file · PyQt6 · no build step · 100% local processing.

<p align="center">
  <img src="docs/screenshots/choice.png" width="380" alt="Double-click the cat: Fetch a file or Cobra mode">
  <img src="docs/screenshots/fetch.png" width="420" alt="Instant file and app search">
</p>

## Features

### 🩺 Health Nudges
| Reminder | Trigger |
|---|---|
| 🚶 **Get up and walk** | After **40 min** of continuous active screen time (a 5-min idle break resets it) |
| 💧 **Hydration prompt** | Every **30 min** of active screen time |
| 🍱 **Lunch break** | **1:00 PM**, once per day |
| 🏠 **End of workday** | **5:45 PM**, once per day |

Idle detection tracks hardware input activity so genuine time away from your desk counts as rest. Notifications never pile up, and routine prompts include a "10 more min" snooze option.

<p align="center">
  <img src="docs/screenshots/walk.png" width="360" alt="Sitting reminder">
  <img src="docs/screenshots/lunch.png" width="360" alt="Lunch reminder">
</p>

### 🔎 Fast Local File & App Launcher
Double-click the cat → **Fetch a file**, and begin typing. The utility maintains an in-memory index of file names across your local fixed drives (indexing millions of entries in seconds and returning results in under 80 ms), persisted locally and updated as files change.

- **Apps:** Indexed system applications and installed package entries display prioritized at the top. Click to launch.
- **Files:** Press `Enter` or click to animate the cat retrieving the item and opening its parent folder with the target pre-selected.
- **Search syntax:** `words` (AND logic) · `!exclude` · `"exact phrase"` · `ext:pdf;docx` · `file:` · `folder:` · `app:` · `*wild?cards` · `docs\report` (path matching)
- **Keyboard shortcuts:** `Enter` launch/locate · `Shift+Enter` copy path · `Ctrl+Enter` open directly · `↑ / ↓` navigate · `Esc` dismiss · drag results directly into external apps

### 🐍 Storage Cleanup Mode
Double-click → **Cobra mode**. Inspect storage usage before performing any action (viewing Recycle Bin usage and old cache files); **no items are removed without explicit confirmation**. During cleanup, an interactive pixel battle animation plays until completion.

<p align="center">
  <img src="docs/screenshots/cobra_fight.png" width="330" alt="The cat animation">
  <img src="docs/screenshots/cobra_ko.png" width="360" alt="Completed animation">
</p>

Safety boundaries:
- Only temp/cache items unmodified for **24 hours or longer** are eligible for deletion.
- Modification dates are re-verified immediately prior to deletion.
- In-use and locked files are skipped safely.
- Directory junctions and symlinks are never traversed.
- All disk operations run at low background I/O priority.
- Right-click → *Preview reminders → Cobra fight (practice)* tests the animation with zero filesystem changes.

### ✨ Visual Design
Subtle frosted glass dialogue styling, clean pixel scaling across mixed-DPI displays, and clean typographic fallbacks using system sans-serif fonts.

## One-Click Install
Download or clone this repository (unzip it first if needed), open the [`install`](install) folder, and double-click the file for your system:

| System | File |
|---|---|
| Windows | `Install Desktop Cat (Windows).bat` |
| Mac | `Install Desktop Cat (Mac).command` |

The installer sets up Python if needed, installs the required packages in a private environment, creates a **Desktop Cat** icon on your Desktop, and starts the companion. It is safe to run again. See [`install/README.txt`](install/README.txt) for security prompts and uninstall steps.

## Platform Support
| Feature | Windows 10 / 11 | macOS |
|---|---|---|
| Companion, health nudges, animations | ✅ | ✅ |
| File search | ✅ Fixed drives, updated live | ✅ Home folder, refreshed hourly |
| App search | ✅ | ✅ |
| Storage cleanup | ✅ | ❌ Finds nothing to clean; not available yet |
| Dialogue styling | Frosted glass | Plain dark tint |

macOS support has not yet been tested on a real Mac. Please open an issue if something misbehaves.

## Requirements (Manual Install)
- **Windows 10 / 11 or macOS**
- **Python 3.10+**
- **PyQt6** (or PySide6)

## Manual Installation
```bash
git clone https://github.com/typotive/poocha.ser_assistant.git
cd poocha.ser_assistant
pip install -r requirements.txt
python desktop_cat.py
```

**Generate Shortcuts:**
Run once to create the launcher with its icon (Windows: Desktop and Start menu; macOS: an app on the Desktop). No console window appears:
```bash
python desktop_cat.py --install-shortcut
```
You can then pin it to your taskbar or Dock. Running a second instance focuses the active companion rather than launching duplicates.

**Quick Demo:**
```bash
python desktop_cat.py --demo
```
*Demo mode accelerates intervals (30 s movement alert, 45 s hydration alert) for quick evaluation.*

## Controls
| Action | Response |
|---|---|
| Single Click | Display status (current desk duration, next break) or dismiss prompt |
| Double Click | Open action menu (**Fetch a file** or **Cleanup mode**) |
| Right Click | Context menu (Status, Fetch, Cleanup, Timer Reset, Animation Previews, Reindex, Quit) |
| Click & Drag | Reposition companion across displays |

## Configuration
All options can be defined within the `Config` dataclass in `desktop_cat.py` or passed via CLI flags:

| Parameter | Default | Command Line Argument |
|---|---|---|
| `alert_after_s` (continuous sitting threshold) | 40 min | `--alert-minutes 40` |
| `idle_threshold_s` (break detection threshold) | 5 min | `--idle-minutes 5` |
| `water_every_s` (hydration interval) | 30 min | `--water-minutes 30` |
| `lunch_time` / `home_time` | 13:00 / 17:45 | `--lunch 13:00 --home 17:45` |
| `routine_days` | All days | e.g. `(0, 1, 2, 3, 4)` for Mon–Fri |
| `index_roots` | Fixed local drives | `--index D:\Work` (repeatable) |
| `fetch_trip` | Enabled | `False` disables animation for instant opens |
| `cobra_min_age_h` | 24 hours | Age cutoff for temporary file cleanup |

## Data & Privacy
All computation occurs locally on your machine. No telemetry, usage statistics, or personal data are collected or transmitted over the network. The local search index is stored exclusively within `%LOCALAPPDATA%\DesktopCat\` (macOS: `~/Library/Application Support/DesktopCat/`) and can be purged at any time.

## Project Structure
```
desktop_cat.py        Application source and logic
desktop_cat.ico       Application icon asset
install/              One-click installers for Windows and macOS
requirements.txt      Runtime dependencies
docs/screenshots/     Visual documentation assets
CLAUDE.md             Architecture reference, implementation notes, and testing guidelines
```

## License
Distributed under the [MIT License](LICENSE).
