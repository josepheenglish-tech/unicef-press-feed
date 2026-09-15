# UNICEF Press Feed

An internal, locally reviewable feed built from the supplied UNICEF monitor and office registry. Open `public/index.html` to browse the collected snapshot. No server or build step is needed for the page.

## Included

- Chronological releases, keyword search, region and office filters, date range and content-type filters.
- Saved releases and read markers stored in the current browser only.
- Source coverage view with failed, unverified and partial collections distinguished from successful collections.
- `public/feed.xml`: RSS 2.0, press releases only, latest 1,000 retained records.
- `public/data.json`: all retained releases, statements and other recognized press output, with source health and original links.
- Original-source thumbnails when available; no sample or generated news content.

## Refresh the collection

Use Python 3.12 or newer, from this folder:

```sh
python -m pip install -r requirements.txt
python monitor.py --workers 6 --pages 2
```

Outputs are written beside this script regardless of the terminal's current folder. Keep `state.json`: it retains items across runs, including source failures, for 180 days from publication (or first observation when the date is unavailable). Tests can be run with `python -m unittest -v`.

For a targeted check, use `python monitor.py --offices india,mali,mexico`. This preserves history and source status for offices outside the selection. `python monitor.py --render-only` rebuilds the page from the collected snapshot without network requests.

## Coverage and interpretation

This is a monitor of the 132 configured office sources, not a verified feed of every UNICEF release or a complete historical archive. The initial registry was supplied by the user; China and Togo were added from the supplied inventory. National Committees, all language editions, and offices represented only by other sites are not comprehensively inventoried. Office names and regional groupings follow that registry and are not independently certified.

The collector tries configured listings followed by common localized press-centre paths. It prefers declared RSS/Atom links and otherwise parses recognized press output links. It fetches at most `--pages` pages per source per run (default 2, maximum 50). Some UNICEF pagination endpoints reject automated requests; these sources are marked partial. Other sources need custom paths in `offices.json`. Successful collection means items were parsed from that source, not that its full publication history was checked. An empty parse is marked `no_items`, never assumed to mean the office is silent.

Publication dates come from each individual source card. Missing dates are explicitly labeled. Statements are distinguished when a recognized content label exists; unlabeled links under recognized press paths default to press release and may need source-specific tuning. Language is inherited from the configured edition; text is never automatically translated.

The page groups exact matching normalized titles on the same publication date after filtering, preserving links to each office copy. The archive and RSS retain every source record. Older releases falling out of the retention window are removed. Page filtering does not change the RSS subscription.

## Public hosting with daily updates

Public access is approved. The deployment workflow is prepared in `.github/workflows/monitor.yml` for GitHub Pages, with daily collection at 11:17 UTC. It runs in GitHub's cloud and does not depend on this computer being on. Publication and the schedule are not active until a repository is connected and configured.

The workflow obtains the site's address from GitHub Pages and uses it in RSS metadata. Colleagues will share the site address; RSS readers can subscribe to its `feed.xml`. The Refresh button loads the most recently collected data; it does not start a server-side collection.

Activation requires putting this project at the root of a GitHub repository, selecting GitHub Actions as the Pages publishing source, and manually running Daily UNICEF press feed once. The workflow must be on the default branch. Repository policy must permit the history commit and Pages deployment. The older `deployment/monitor.yml` is a reference only; use the workflow in `.github/workflows`.

Daily runs test the collector, fetch current releases, preserve history, and deploy the output. A failed collection or deployment leaves the last published site available. Scheduled runs can be delayed by GitHub; the collection timestamp and Sources view show freshness and collection gaps. No GitHub connection was available when these files were prepared, so no public address has been created yet.

Source snapshots are publicly accessible UNICEF material. This interface is an internal working tool, not an official UNICEF publishing service. Remote thumbnails load from their original UNICEF URL and may be unavailable offline. Lucide icons are bundled locally under their accompanying license.
