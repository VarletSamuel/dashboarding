/**
 * azure-loader.js
 * ===============
 * Drop-in snippet that adds a "☁️ Load from Azure Storage" button
 * to the upload screen of any dashboard in this project.
 *
 * HOW TO USE
 * ----------
 * 1.  Host this file alongside your dashboards (or inline it).
 * 2.  In each dashboard HTML, add BEFORE the closing </body>:
 *
 *       <script src="azure-loader.js"></script>
 *       <script>
 *         AzureLoader.init({
 *           // Required: URL of the manifest JSON written by the Azure Function.
 *           // Example: https://<account>.blob.core.windows.net/dashboard/manifest_MYCO.json
 *           manifestUrl: 'https://<account>.blob.core.windows.net/dashboard/manifest_MYCO.json',
 *
 *           // Which file key from the manifest to load into THIS dashboard.
 *           // Possible keys: 'daily_costs' | 'workload_profiles' | 'eventhub_namespaces'
 *           fileKey: 'daily_costs',
 *
 *           // The id of the upload screen element to attach the button to.
 *           uploadScreenId: 'uploadScreen',   // default
 *
 *           // Optional: callback invoked with the raw CSV text when loaded.
 *           // If omitted, AzureLoader will try to call window.handleFile()
 *           // or window.loadDemo() pattern used by all existing dashboards.
 *           onCsvLoaded: null,
 *         });
 *       </script>
 *
 * 3.  Make sure the storage account has CORS configured to allow your
 *     static-website origin (see README for the az CLI one-liner).
 *
 * NOTES
 * -----
 * - The manifest URL and file keys are baked into the HTML at deploy time.
 *   No credentials are needed — the blobs are publicly readable.
 * - The manifest JSON is written by orchestrator.py after every collection run.
 */

(function (global) {
  'use strict';

  // ── Styles injected once ─────────────────────────────────────────────────
  const CSS = `
  .az-loader-wrap {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
    margin-top: 4px;
  }
  .az-loader-sep {
    color: var(--text-muted, #5a6480);
    font-size: 13px;
  }
  .az-loader-btn {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 9px 22px;
    border-radius: 8px;
    font-family: var(--font-body, 'DM Sans', sans-serif);
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s;
    border: 1px solid var(--border, #262d40);
    background: var(--surface, #151820);
    color: var(--cyan, #22d3ab);
  }
  .az-loader-btn:hover:not(:disabled) {
    border-color: var(--cyan, #22d3ab);
    background: var(--cyan-dim, rgba(34,211,171,0.12));
  }
  .az-loader-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .az-loader-status {
    font-size: 12px;
    font-family: var(--font-mono, 'JetBrains Mono', monospace);
    color: var(--text-dim, #8892a8);
    min-height: 18px;
    text-align: center;
  }
  .az-loader-status.err { color: var(--red, #ef5350); }
  .az-loader-status.ok  { color: var(--cyan, #22d3ab); }
  .az-loader-meta {
    font-size: 11px;
    color: var(--text-muted, #5a6480);
    font-family: var(--font-mono, monospace);
    text-align: center;
    display: none;
  }
  `;

  function injectStyles() {
    if (document.getElementById('az-loader-styles')) return;
    const el = document.createElement('style');
    el.id = 'az-loader-styles';
    el.textContent = CSS;
    document.head.appendChild(el);
  }

  // ── Manifest fetcher ─────────────────────────────────────────────────────
  async function fetchManifest(url) {
    const resp = await fetch(url, { cache: 'no-store' });
    if (!resp.ok) throw new Error(`Manifest fetch failed: HTTP ${resp.status}`);
    return resp.json();
  }

  async function fetchCsv(url) {
    const resp = await fetch(url, { cache: 'no-store' });
    if (!resp.ok) throw new Error(`CSV fetch failed: HTTP ${resp.status}`);
    return resp.text();
  }

  // ── Build a synthetic File-like object so existing handleFile() works ────
  function textToFakeFile(text, filename) {
    const blob = new Blob([text], { type: 'text/csv' });
    // Blob doesn't have a .name property — attach one
    return Object.assign(blob, { name: filename });
  }

  // ── Main init ────────────────────────────────────────────────────────────
  function init(options) {
    const {
      manifestUrl,
      fileKey,
      uploadScreenId = 'uploadScreen',
      onCsvLoaded = null,
    } = options || {};

    if (!manifestUrl) {
      console.warn('[AzureLoader] manifestUrl is required.');
      return;
    }
    if (!fileKey) {
      console.warn('[AzureLoader] fileKey is required.');
      return;
    }

    // Wait for DOM
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => _mount({ manifestUrl, fileKey, uploadScreenId, onCsvLoaded }));
    } else {
      _mount({ manifestUrl, fileKey, uploadScreenId, onCsvLoaded });
    }
  }

  function _mount({ manifestUrl, fileKey, uploadScreenId, onCsvLoaded }) {
    injectStyles();

    const screen = document.getElementById(uploadScreenId);
    if (!screen) {
      console.warn('[AzureLoader] Upload screen #' + uploadScreenId + ' not found.');
      return;
    }

    // Build the UI block
    const wrap = document.createElement('div');
    wrap.className = 'az-loader-wrap';
    wrap.innerHTML = `
      <span class="az-loader-sep">— or —</span>
      <button class="az-loader-btn" id="azLoadBtn">
        ☁️ Load from Azure Storage
      </button>
      <div class="az-loader-status" id="azStatus"></div>
      <div class="az-loader-meta" id="azMeta"></div>
    `;
    screen.appendChild(wrap);

    const btn    = document.getElementById('azLoadBtn');
    const status = document.getElementById('azStatus');
    const meta   = document.getElementById('azMeta');

    btn.addEventListener('click', async () => {
      btn.disabled = true;
      status.className = 'az-loader-status';
      status.textContent = '⏳ Fetching manifest …';
      meta.style.display = 'none';

      try {
        // 1. Load manifest
        const manifest = await fetchManifest(manifestUrl);
        const csvUrl = manifest?.files?.[fileKey];
        if (!csvUrl) {
          throw new Error(
            `Key '${fileKey}' not found in manifest. ` +
            `Available keys: ${Object.keys(manifest?.files || {}).join(', ') || '(none)'}`
          );
        }

        // Show metadata from manifest
        meta.textContent = `Generated ${manifest.generated_at?.slice(0, 16)?.replace('T', ' ')} UTC  ·  ${manifest.date_from} → ${manifest.date_to}`;
        meta.style.display = 'block';

        status.textContent = '⏳ Downloading CSV …';

        // 2. Fetch CSV
        const csvText = await fetchCsv(csvUrl);

        status.className = 'az-loader-status ok';
        status.textContent = '✓ Loaded from Azure Storage';

        // 3. Hand off to dashboard
        const filename = csvUrl.split('/').pop();

        if (typeof onCsvLoaded === 'function') {
          onCsvLoaded(csvText, filename, manifest);
          return;
        }

        // Auto-detect the dashboard's file handler
        if (typeof window.handleFile === 'function') {
          const fakeFile = textToFakeFile(csvText, filename);
          window.handleFile(fakeFile);
        } else if (typeof window.handleFileText === 'function') {
          window.handleFileText(csvText, filename);
        } else {
          // Fallback: trigger a synthetic file-input change
          const dt = new DataTransfer();
          dt.items.add(new File([csvText], filename, { type: 'text/csv' }));
          const fi = document.querySelector('input[type=file][accept=".csv"]');
          if (fi) {
            Object.defineProperty(fi, 'files', { value: dt.files, writable: false });
            fi.dispatchEvent(new Event('change', { bubbles: true }));
          } else {
            console.error('[AzureLoader] Could not find a file handler. Implement window.handleFile(file) or pass onCsvLoaded.');
          }
        }

      } catch (err) {
        status.className = 'az-loader-status err';
        status.textContent = '❌ ' + err.message;
        console.error('[AzureLoader]', err);
        btn.disabled = false;
      }
    });
  }

  // ── Public API ───────────────────────────────────────────────────────────
  global.AzureLoader = { init };

})(window);