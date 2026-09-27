# Only on Vision Pro

A searchable directory of every app that runs **only** on Apple Vision Pro: apps whose US App Store listing supports Vision Pro and nothing else (no iPhone, iPad, Mac or Apple TV version).

The site is plain static files in `site/`, so it can be hosted by Vercel (`vercel.json` points it at `site/`) or GitHub Pages (the workflow publishes there when Pages is set to "GitHub Actions").

- Search by name, developer or keyword, filter by category (and game type), sort by popularity, release date or last update.
- Tap an app to open it in the App Store. On iPhone, tapping **Get** installs it on your Vision Pro.
- The list updates itself every day.

## How it stays current

A GitHub Actions workflow (`.github/workflows/update.yml`) runs every 4 hours. Once a day it runs the full check in `scripts/update.py`:

1. Reads Apple's Vision Pro top charts for the US in every category, plus three other countries per day in rotation (all 12 every four days).
2. Reads the Vision Pro App Store's "Apps & Games" and category pages.
3. Looks every candidate up with the public iTunes Lookup API and keeps apps whose supported devices are only Apple Vision Pro.
4. Lists every app from each Vision Pro developer, which catches new releases that aren't charting yet.
5. Checks each app's App Store page once to confirm it isn't also sold for another platform (such as Apple TV), and to get its subtitle.
6. Downloads icons for new apps and writes `site/data/apps.json`.

Apps that disappear from the US App Store are removed after two missed checks. The other runs finish any leftover work and otherwise do nothing. Everything the script remembers lives in `state/`, and `state/status.json` shows what the last run did.

To run the full check right away: **Actions → Update and publish → Run workflow** (tick "Run the full App Store check now").

## Files

| Path | What it is |
| --- | --- |
| `site/` | The published website |
| `site/data/apps.json` | The app list the page loads |
| `site/icons/` | One 128px icon per app |
| `scripts/update.py` | The daily updater |
| `scripts/make_images.py` | Draws the link-preview image and home-screen icon |
| `state/` | The updater's memory between runs |

Independent project, not affiliated with Apple. App names and icons belong to their developers.
