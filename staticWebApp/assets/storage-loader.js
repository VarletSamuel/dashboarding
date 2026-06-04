/**
 * Storage-based File Loader for Dashboards
 * 
 * Usage in dashboard HTML:
 *   - Call initStorageFileLoader() on page load
 *   - Pass dashboardId (e.g., 'app_service_plans', 'virtual_machines')
 *   - Automatically detects and loads files from manifest if connection is available
 *   - Falls back to upload UI if no connection
 */

// ══════════════════════════════════════════════════════════════
// AZURE STORAGE CLIENT
// ══════════════════════════════════════════════════════════════
class AzureStorageClient {
  constructor(connectionString) {
    this.connectionString = connectionString;
    this.parsedConnection = this.parseConnectionString(connectionString);
  }

  parseConnectionString(connStr) {
    // Parse connection string: DefaultEndpointProtocol=https://;AccountName=...;AccountKey=...;EndpointSuffix=...
    // Or SAS URL: https://account.blob.core.windows.net/?sv=...
    console.log(`[AzureStorageClient.parseConnectionString] Parsing connection (${connStr.length} chars)`);
    const result = {
      accountName: null,
      accountKey: null,
      sasToken: null,
      endpoint: null,
      container: null,    // extracted from path for scoped SAS tokens
      pathPrefix: null,   // directory prefix within container
    };

    if (connStr.includes('?sv=')) {
      // SAS URL format: https://account.blob.core.windows.net/[container/[prefix]]?sv=...
      console.log(`[AzureStorageClient.parseConnectionString] Detected SAS URL format`);
      try {
        const url = new URL(connStr);
        result.endpoint = url.origin;
        result.accountName = url.hostname.split('.')[0];
        result.sasToken = url.search.substring(1); // Remove leading ?

        // Extract container and optional path prefix from URL path
        // e.g. /reports/SYNH  →  container=reports, pathPrefix=SYNH/
        const pathParts = url.pathname.replace(/^\//, '').split('/').filter(Boolean);
        if (pathParts.length >= 1) {
          result.container = pathParts[0];
          result.pathPrefix = pathParts.length > 1 ? pathParts.slice(1).join('/') + '/' : '';
          console.log(`[AzureStorageClient.parseConnectionString] ✓ SAS URL parsed: account=${result.accountName}, container=${result.container}, prefix='${result.pathPrefix}'`);
        } else {
          console.log(`[AzureStorageClient.parseConnectionString] ✓ SAS URL parsed: account=${result.accountName}, endpoint=${result.endpoint} (account-level)`);
        }
      } catch (err) {
        console.error(`[AzureStorageClient.parseConnectionString] ✗ Failed to parse SAS URL:`, err.message);
      }
    } else {
      // Connection string format
      console.log(`[AzureStorageClient.parseConnectionString] Detected connection string format`);
      const parts = connStr.split(';');
      console.log(`[AzureStorageClient.parseConnectionString] Parts: ${parts.length}`);
      parts.forEach((part, idx) => {
        const [key, value] = part.split('=');
        if (key === 'AccountName') result.accountName = value;
        if (key === 'AccountKey') result.accountKey = value;
        if (key === 'SharedAccessSignature') result.sasToken = value;
        if (key === 'DefaultEndpointProtocol' && value === 'https') {
          // endpoint will be constructed from AccountName
        }
      });
      if (result.accountName) {
        result.endpoint = `https://${result.accountName}.blob.core.windows.net`;
        console.log(`[AzureStorageClient.parseConnectionString] ✓ Connection string parsed: account=${result.accountName}, endpoint=${result.endpoint}`);
      } else {
        console.warn(`[AzureStorageClient.parseConnectionString] ⚠️ Incomplete connection string: account=${result.accountName}, key=${result.accountKey ? 'present' : 'missing'}`);
      }
    }

    return result;
  }

  async listContainers() {
    if (!this.parsedConnection.endpoint) throw new Error('Invalid connection string');

    // Scoped SAS tokens cannot enumerate account containers; return the scoped container only.
    if (this.parsedConnection.container) {
      return [this.parsedConnection.container];
    }
    
    console.log(`[AzureStorageClient.listContainers] API call: ${this.parsedConnection.endpoint}/?comp=list`);
    const url = new URL(`${this.parsedConnection.endpoint}/?comp=list`);
    this.appendSasToUrl(url);

    try {
      const response = await fetch(url.toString(), {
        method: 'GET',
        cache: 'no-store',
        headers: this.getAuthHeaders(),
      });

      if (!response.ok) {
        const errMsg = `Failed to list containers: ${response.status} ${response.statusText}`;
        console.error(`[AzureStorageClient.listContainers] ${errMsg}`);
        throw new Error(errMsg);
      }
      
      const xml = await response.text();
      const containers = this.parseContainerList(xml);
      console.log(`[AzureStorageClient.listContainers] ✓ Success: ${containers.length} container(s)`);
      return containers;
    } catch (err) {
      console.error(`[AzureStorageClient.listContainers] ✗ Error:`, err.message);
      throw err;
    }
  }

  async listBlobs(containerName, prefix = '') {
    if (!this.parsedConnection.endpoint) throw new Error('Invalid connection string');
    
    console.log(`[AzureStorageClient.listBlobs] API call: ${containerName}${prefix ? ` (prefix: ${prefix})` : ''}`);
    const url = new URL(`${this.parsedConnection.endpoint}/${containerName}?comp=list&restype=container`);
    if (prefix) url.searchParams.append('prefix', prefix);
    this.appendSasToUrl(url);

    try {
      const response = await fetch(url.toString(), {
        method: 'GET',
        cache: 'no-store',
        headers: this.getAuthHeaders(),
      });

      if (!response.ok) {
        const errMsg = `Failed to list blobs: ${response.status} ${response.statusText}`;
        console.error(`[AzureStorageClient.listBlobs] ${errMsg}`);
        throw new Error(errMsg);
      }
      
      const xml = await response.text();
      const blobs = this.parseBlobList(xml);
      console.log(`[AzureStorageClient.listBlobs] ✓ Success: ${blobs.length} blob(s)`);
      return blobs;
    } catch (err) {
      console.error(`[AzureStorageClient.listBlobs] ✗ Error:`, err.message);
      throw err;
    }
  }

  async downloadBlob(containerName, blobName) {
    if (!this.parsedConnection.endpoint) throw new Error('Invalid connection string');
    
    // If SAS is scoped to a directory path, prepend that prefix to the blob name
    const prefix = this.parsedConnection.pathPrefix || '';
    const fullBlobName = prefix && !blobName.startsWith(prefix) ? prefix + blobName : blobName;
    
    console.log(`[AzureStorageClient.downloadBlob] Downloading: ${containerName}/${fullBlobName}`);
    const url = new URL(`${this.parsedConnection.endpoint}/${containerName}/${fullBlobName}`);
    this.appendSasToUrl(url);

    try {
      const response = await fetch(url.toString(), {
        method: 'GET',
        cache: 'no-store',
        headers: this.getAuthHeaders(),
      });

      if (!response.ok) {
        const errMsg = `Failed to download blob: ${response.status} ${response.statusText}`;
        console.error(`[AzureStorageClient.downloadBlob] ${errMsg}`);
        throw new Error(errMsg);
      }
      
      const content = await response.text();
      console.log(`[AzureStorageClient.downloadBlob] ✓ Downloaded: ${content.length} bytes`);
      return content;
    } catch (err) {
      console.error(`[AzureStorageClient.downloadBlob] ✗ Error:`, err.message);
      throw err;
    }
  }

  async findManifests(selectedFolder = '') {
    console.log(`[AzureStorageClient.findManifests] Starting manifest search...`);
    const manifests = [];
    const { container, pathPrefix } = this.parsedConnection;

    if (container) {
      // Scoped SAS: try selected folder first (container-level SAS), then fallback.
      const manifestCandidates = [];
      if (selectedFolder && !pathPrefix) {
        manifestCandidates.push(`${selectedFolder}/manifest.json`);
      } else if (selectedFolder && pathPrefix) {
        // If SAS is already folder-scoped, only allow matching selected folder.
        const scopedFolder = String(pathPrefix || '').replace(/\/+$/, '');
        if (scopedFolder !== selectedFolder) {
          console.warn(`[AzureStorageClient.findManifests] Scoped folder '${scopedFolder}' does not match selected folder '${selectedFolder}'`);
          return manifests;
        }
      }
      if (!selectedFolder || pathPrefix) {
        manifestCandidates.push('manifest.json');
      }
      console.log(`[AzureStorageClient.findManifests] Scoped SAS detected → candidates: ${manifestCandidates.join(', ')}`);
      try {
        for (const candidate of manifestCandidates) {
          try {
            const content = await this.downloadBlob(container, candidate);
            const json = JSON.parse(content);
            manifests.push({ container, blob: `${pathPrefix || ''}${candidate}`, manifest: json });
            console.log(`[AzureStorageClient.findManifests] ✓ Found manifest for: ${json.customer} (${candidate})`);
            break;
          } catch (candidateErr) {
            console.log(`[AzureStorageClient.findManifests] Candidate not found/readable: ${candidate}`);
            if (candidate === manifestCandidates[manifestCandidates.length - 1]) {
              throw candidateErr;
            }
          }
        }
      } catch (err) {
        console.error(`[AzureStorageClient.findManifests] ✗ Could not load manifest.json:`, err.message);
      }
      return manifests;
    }

    // Account-level SAS: enumerate all containers
    try {
      console.log(`[AzureStorageClient.findManifests] Account-level SAS → scanning all containers...`);
      const containers = await this.listContainers();
      console.log(`[AzureStorageClient.findManifests] Found ${containers.length} container(s): ${containers.join(', ')}`);
      
      for (const c of containers) {
        console.log(`[AzureStorageClient.findManifests] Scanning container: ${c}`);
        try {
          const blobs = await this.listBlobs(c);
          const manifestBlobs = blobs.filter(b => b.name.endsWith('manifest.json'));
          console.log(`[AzureStorageClient.findManifests]   ${blobs.length} blob(s), ${manifestBlobs.length} manifest(s)`);
          for (const blob of manifestBlobs) {
            try {
              const content = await this.downloadBlob(c, blob.name);
              const json = JSON.parse(content);
              manifests.push({ container: c, blob: blob.name, manifest: json });
              console.log(`[AzureStorageClient.findManifests]   ✓ Parsed manifest: ${json.customer}`);
            } catch (parseErr) {
              console.error(`[AzureStorageClient.findManifests]   ✗ Failed to parse ${blob.name}:`, parseErr.message);
            }
          }
        } catch (blobErr) {
          console.error(`[AzureStorageClient.findManifests]   ✗ Error scanning ${c}:`, blobErr.message);
        }
      }
      console.log(`[AzureStorageClient.findManifests] Found ${manifests.length} valid manifest(s)`);
    } catch (err) {
      console.error(`[AzureStorageClient.findManifests] Fatal error:`, err);
    }

    return manifests;
  }

  appendSasToUrl(url) {
    const rawSas = (this.parsedConnection.sasToken || '').replace(/^\?/, '');
    if (!rawSas) return;

    const sasParams = new URLSearchParams(rawSas);
    sasParams.forEach((value, key) => {
      if (!url.searchParams.has(key)) {
        url.searchParams.append(key, value);
      }
    });
  }

  getAuthHeaders() {
    const headers = {};
    // For SAS tokens, authentication is in the URL, not headers
    // For account key, we'd need to implement SharedKey auth (complex for browser)
    // For now, SAS tokens are the recommended approach
    return headers;
  }

  parseContainerList(xml) {
    const parser = new DOMParser();
    const doc = parser.parseFromString(xml, 'text/xml');
    const containers = [];
    
    const containerElements = doc.querySelectorAll('Container');
    containerElements.forEach(el => {
      const name = el.querySelector('Name')?.textContent;
      if (name) containers.push(name);
    });

    return containers;
  }

  parseBlobList(xml) {
    const parser = new DOMParser();
    const doc = parser.parseFromString(xml, 'text/xml');
    const blobs = [];
    
    const blobElements = doc.querySelectorAll('Blob');
    blobElements.forEach(el => {
      const name = el.querySelector('Name')?.textContent;
      const size = el.querySelector('Content-Length')?.textContent;
      const modified = el.querySelector('Last-Modified')?.textContent;
      if (name) {
        blobs.push({
          name,
          size: parseInt(size) || 0,
          modified,
        });
      }
    });

    return blobs;
  }
}

// ══════════════════════════════════════════════════════════════
// STORAGE FILE LOADER
// ══════════════════════════════════════════════════════════════

const LOCAL_DATA_DB = 'dco-local-data';
const LOCAL_DATA_STORE = 'datasets';
const LOCAL_DATASET_ID_KEY = 'dco-local-dataset-id';
const LOCAL_DATASET_ACTIVE_KEY = 'dco-local-dataset-active';

function openLocalDataDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(LOCAL_DATA_DB, 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(LOCAL_DATA_STORE)) {
        db.createObjectStore(LOCAL_DATA_STORE, { keyPath: 'id' });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error || new Error('Could not open local data database'));
  });
}

async function readLocalDataset(datasetId) {
  if (!datasetId) return null;
  const db = await openLocalDataDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(LOCAL_DATA_STORE, 'readonly');
    const store = tx.objectStore(LOCAL_DATA_STORE);
    const req = store.get(datasetId);
    req.onsuccess = () => resolve(req.result || null);
    req.onerror = () => reject(req.error || new Error('Could not read local dataset'));
  });
}

function getLocalActiveDatasetId() {
  return localStorage.getItem(LOCAL_DATASET_ID_KEY);
}

function isLocalSourceActive() {
  return localStorage.getItem(LOCAL_DATASET_ACTIVE_KEY) === 'true' && !!getLocalActiveDatasetId();
}

function getBasename(name) {
  const norm = String(name || '').replace(/\\/g, '/');
  const idx = norm.lastIndexOf('/');
  return idx >= 0 ? norm.slice(idx + 1) : norm;
}

function findManifestInLocalDataset(dataset) {
  const files = (dataset && dataset.files) || [];
  const manifestFile = files.find(f => getBasename(f.name || '') === 'manifest.json');
  if (!manifestFile || !manifestFile.content) return null;
  try {
    return JSON.parse(manifestFile.content);
  } catch (err) {
    console.warn('[storage-loader][local] Invalid manifest.json:', err.message);
    return null;
  }
}

function fileSortScore(name) {
  const base = getBasename(name);
  const compact = base.match(/_(\d{8}_\d{4})\./);
  if (compact) return Number(compact[1].replace('_', ''));
  const range = base.match(/_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\./);
  if (range) return Number(range[2].replace(/-/g, ''));
  const day = base.match(/_(\d{4}-\d{2}-\d{2})\./);
  if (day) return Number(day[1].replace(/-/g, ''));
  return 0;
}

function getManifestDashboardIds(dashboardId) {
  const aliases = {
    quality_checks: [
      'quality_checks',
      'qualityChecks',
      'virtualMachines',
      'keyVaults',
      'storageAccounts',
      'appServicePlans',
      'sqlDatabases',
    ],
  };
  return aliases[dashboardId] || [dashboardId];
}

function manifestSupportsQualityChecks(manifest) {
  if (!manifest) return false;

  const hasNonCostDashboard = Array.isArray(manifest.dashboards) && manifest.dashboards.some(d => {
    const id = String(d?.id || '');
    if (!id || id === 'azureCosts') return false;
    const files = Array.isArray(d?.files) ? d.files : [];
    return files.length > 0;
  });

  const hasSubscriptionsOtherFile = Array.isArray(manifest.other_files) && manifest.other_files.some(f => {
    return String(f?.type || '').toLowerCase() === 'subscriptions' && !!String(f?.filename || '').trim();
  });

  return hasNonCostDashboard || hasSubscriptionsOtherFile;
}

function shouldMergeByTypeForDashboard(dashboardId) {
  // Quality checks intentionally ingests multiple summary CSV shapes.
  // Keep files separate so per-file schema detection continues to work.
  return dashboardId !== 'quality_checks';
}

function inferDashboardFilesFromLocal(dashboardId, datasetFiles) {
  const files = (datasetFiles || []).map(f => ({
    name: getBasename(f.name || ''),
    fullName: f.name || '',
    content: f.content || '',
  }));

  const patterns = {
    appServicePlans: [
      { type: 'summary', re: /_app_service_plans_summary_/i },
      { type: 'timeseries', re: /_app_service_plans_timeseries_/i },
    ],
    containerApps: [
      { type: 'summary', re: /_container_apps_summary_/i },
      { type: 'timeseries', re: /_container_apps_timeseries_/i },
    ],
    virtualMachines: [
      { type: 'summary', re: /_virtual_machines_summary_/i },
      { type: 'timeseries', re: /_virtual_machines_timeseries_/i },
    ],
    eventhubs: [
      { type: 'summary', re: /_eventhub_summary_/i },
      { type: 'timeseries', re: /_eventhub_timeseries_/i },
    ],
    postgresql: [
      { type: 'summary', re: /_postgresql_summary_/i },
      { type: 'timeseries', re: /_postgresql_timeseries_/i },
    ],
    azureCosts: [
      { type: 'costs_by_resource', re: /_daily_costs_/i },
    ],
    storageAccounts: [
      { type: 'summary', re: /_storage_accounts_summary_/i },
    ],
    keyVaults: [
      { type: 'summary', re: /_keyvaults_summary_/i },
    ],
    appSecretExpirations: [
      { type: 'summary', re: /_app_secret_expirations_summary_/i },
    ],
    cosmos: [
      { type: 'summary', re: /_cosmos_ru_/i },
    ],
    reservedInstances: [
      { type: 'summary', re: /_reserved_instances_/i },
    ],
    quality_checks: [
      { type: 'summary', re: /_virtual_machines_summary_/i },
      { type: 'summary', re: /_keyvaults_summary_/i },
      { type: 'summary', re: /_storage_accounts_summary_/i },
      { type: 'summary', re: /_app_service_plans_summary_/i },
      { type: 'summary', re: /_sql_summary_/i },
      { type: 'subscriptions', re: /^subscriptions_.*\.csv$/i },
    ],
  };

  const defs = patterns[dashboardId] || [];
  const out = [];

  defs.forEach(def => {
    const matched = files.filter(f => def.re.test(f.name));
    if (!matched.length) return;

    if (def.type === 'summary') {
      const latest = matched.sort((a, b) => fileSortScore(b.name) - fileSortScore(a.name))[0];
      out.push({ name: latest.name, type: 'summary', content: latest.content });
    } else {
      matched
        .sort((a, b) => fileSortScore(a.name) - fileSortScore(b.name))
        .forEach(item => out.push({ name: item.name, type: def.type, content: item.content }));
    }
  });

  return out;
}

function matchLocalFile(files, targetName) {
  const exact = files.find(f => getBasename(f.name || '') === targetName);
  if (exact) return exact;

  const normalizedTarget = String(targetName || '').replace(/\\/g, '/');
  return files.find(f => String(f.name || '').replace(/\\/g, '/').endsWith(normalizedTarget));
}

async function loadLocalFilesForDashboard(dashboardId, onStep) {
  const step = (msg) => { if (typeof onStep === 'function') onStep(msg); };
  const datasetId = getLocalActiveDatasetId();
  if (!datasetId) return [];

  step('Reading local folder dataset…');
  const dataset = await readLocalDataset(datasetId);
  if (!dataset || !Array.isArray(dataset.files)) return [];

  const manifest = findManifestInLocalDataset(dataset);
  if (manifest && Array.isArray(manifest.dashboards)) {
    const ids = new Set(getManifestDashboardIds(dashboardId));
    const dashboards = manifest.dashboards.filter(d => ids.has(d.id));
    const totalFiles = dashboards.reduce((sum, d) => sum + ((d.files || []).length), 0);
    if (dashboards.length && totalFiles) {
      step(`Loading ${totalFiles} file(s) from local manifest…`);
      const loaded = [];
      const seen = new Set();
      dashboards.forEach(dashboard => {
        dashboard.files.forEach(fileInfo => {
          const matched = matchLocalFile(dataset.files, fileInfo.filename);
          if (!matched) return;
          if (!hasCsvDataRows(matched.content || '')) return;
          const key = `${fileInfo.type}|${getBasename(matched.name || fileInfo.filename)}`;
          if (seen.has(key)) return;
          seen.add(key);
            loaded.push({
              export_generated_at_utc: fileInfo.export_generated_at_utc || '',
            name: getBasename(matched.name || fileInfo.filename),
            type: fileInfo.type,
            content: matched.content || '',
          });
        });
      });

      if (dashboardId === 'quality_checks' && Array.isArray(manifest.other_files)) {
        manifest.other_files
          .filter(f => String(f.type || '').toLowerCase() === 'subscriptions')
          .forEach(fileInfo => {
            const matched = matchLocalFile(dataset.files, fileInfo.filename);
            if (!matched) return;
            if (!hasCsvDataRows(matched.content || '')) return;
            const key = `subscriptions|${getBasename(matched.name || fileInfo.filename)}`;
            if (seen.has(key)) return;
            seen.add(key);
            loaded.push({
              name: getBasename(matched.name || fileInfo.filename),
              type: 'subscriptions',
              content: matched.content || '',
            });
          });
      }
      return loaded;
    }
  }

  step('Inferring dashboard files from local folder contents…');
  return inferDashboardFilesFromLocal(dashboardId, dataset.files);
}

async function initStorageFileLoader(dashboardId, onStep) {
  const step = (msg) => { if (typeof onStep === 'function') onStep(msg); };
  console.log(`🔄 [storage-loader] Initializing for dashboard: ${dashboardId}`);
  step('Checking configured data source…');
  
  const STORAGE_CONFIG_KEY = 'dco-storage-connection';
  const STORAGE_FOLDER_KEY = 'dco-storage-folder';
  const hash = window.location.hash;
  const hashParams = new URLSearchParams((hash || '').replace(/^#/, ''));

  if (hash.startsWith('#local=')) {
    const localValue = decodeURIComponent(hash.slice(7));
    if (localValue && localValue !== '1') {
      localStorage.setItem(LOCAL_DATASET_ID_KEY, localValue);
    }
    localStorage.setItem(LOCAL_DATASET_ACTIVE_KEY, 'true');
    history.replaceState(null, '', window.location.pathname + window.location.search);
  }

  if (isLocalSourceActive()) {
    try {
      step('Loading from local folder…');
      const localFiles = await loadLocalFilesForDashboard(dashboardId, step);
      const normalized = shouldMergeByTypeForDashboard(dashboardId)
        ? mergeLoadedFilesByType(localFiles || [])
        : (localFiles || []);
      if (normalized.length > 0) {
        if (typeof onFilesLoaded === 'function') {
          onFilesLoaded(normalized);
        }
        console.log(`✅ [storage-loader][local] Loaded ${normalized.length} file type payload(s)`);
        return true;
      }
      console.log(`ℹ️ [storage-loader][local] No local files resolved for dashboard: ${dashboardId}`);
    } catch (err) {
      console.warn(`⚠️ [storage-loader][local] Local folder load failed: ${err.message}`);
    }
  }

  // Read connection/folder from URL hash first — injected by index.html when navigating.
  // This is needed because file:// localStorage is scoped per-directory in some browsers.
  let connection = null;
  const connFromHash = hashParams.get('conn');
  const folderFromHash = hashParams.get('folder');
  if (connFromHash !== null) {
    connection = connFromHash;
    // Persist locally when provided; clear local copy when hash is explicitly empty.
    if (connection) {
      localStorage.setItem(STORAGE_CONFIG_KEY, connection);
    } else {
      localStorage.removeItem(STORAGE_CONFIG_KEY);
    }
    if (folderFromHash) {
      localStorage.setItem(STORAGE_FOLDER_KEY, folderFromHash);
    }
    // Remove hash from URL without triggering a reload
    history.replaceState(null, '', window.location.pathname + window.location.search);
    console.log(`✓ [storage-loader] Connection/folder read from URL hash and saved to localStorage`);
  } else {
    connection = localStorage.getItem(STORAGE_CONFIG_KEY);
  }

  const selectedFolder = (folderFromHash || localStorage.getItem(STORAGE_FOLDER_KEY) || '').trim();

  if (!connection) {
    console.log(`ℹ️ [storage-loader] No storage connection configured. Using file upload UI.`);
    return false;
  }
  
  console.log(`✓ [storage-loader] Connection string found (length: ${connection.length} chars)`);

  try {
    const client = new AzureStorageClient(connection);
    console.log(`✓ [storage-loader] AzureStorageClient initialized`);
    console.log(`  - Parsed: ${client.parsedConnection.accountName ? '✓ Account: ' + client.parsedConnection.accountName : '✗ No account parsed'}`);
    console.log(`  - SAS Token: ${client.parsedConnection.sasToken ? '✓ Present' : '✗ Not found'}`);
    
    console.log(`🔍 [storage-loader] Searching for manifests...`);
    step('Looking for manifest…');
    const manifests = await client.findManifests(selectedFolder);
    console.log(`  Found ${manifests.length} manifest(s)`);
    
    if (manifests.length === 0) {
      console.log(`❌ [storage-loader] No manifest files found in storage account.`);
      return false;
    }

    // List all found manifests with their dashboards
    manifests.forEach((m, idx) => {
      const dashboards = m.manifest.dashboards?.map(d => d.id).join(', ') || 'none';
      console.log(`  [${idx}] ${m.blob} (Customer: ${m.manifest.customer}, Dashboards: ${dashboards})`);
    });

    const candidateDashboardIds = getManifestDashboardIds(dashboardId);

    // Find manifest that contains this dashboard (or one of its aliases)
    console.log(`🔎 [storage-loader] Searching for dashboard: ${dashboardId}`);
    let relevantManifests = manifests.filter(m => {
      return m.manifest.dashboards?.some(d => candidateDashboardIds.includes(d.id));
    });

    if (dashboardId === 'quality_checks' && relevantManifests.length === 0) {
      relevantManifests = manifests.filter(m => manifestSupportsQualityChecks(m.manifest));
    }

    if (selectedFolder) {
      relevantManifests = relevantManifests.filter(m => {
        const blobPath = m.blob || '';
        const firstPathSegment = blobPath.split('/')[0];
        return firstPathSegment === selectedFolder || m.manifest.customer === selectedFolder;
      });
      console.log(`🔎 [storage-loader] Folder filter '${selectedFolder}' → ${relevantManifests.length} matching manifest(s)`);
    }

    if (relevantManifests.length === 0) {
      console.log(`❌ [storage-loader] No manifest found for dashboard: ${dashboardId}`);
      console.log(`   Available dashboards in manifests:`, manifests.flatMap(m => m.manifest.dashboards?.map(d => d.id) || []));
      return false;
    }

    console.log(`✓ [storage-loader] Found ${relevantManifests.length} matching manifest(s)`);

    // Use the most recent manifest (sort by generated_at if multiple exist)
    const manifest = relevantManifests.sort((a, b) => {
      const dateA = new Date(a.manifest.generated_at || 0);
      const dateB = new Date(b.manifest.generated_at || 0);
      return dateB - dateA;
    })[0];

    console.log(`✓ [storage-loader] Using manifest: ${manifest.blob} (Generated: ${manifest.manifest.generated_at})`);

    // Load files for this dashboard (supports composite IDs like quality_checks)
    const dashboards = (manifest.manifest.dashboards || []).filter(d => candidateDashboardIds.includes(d.id));
    const totalDashboardFiles = dashboards.reduce((sum, d) => sum + ((d.files || []).length), 0);
    const hasSubscriptionsOtherFile = dashboardId === 'quality_checks' && Array.isArray(manifest.manifest.other_files)
      && manifest.manifest.other_files.some(f => String(f?.type || '').toLowerCase() === 'subscriptions');
    if ((!dashboards.length || !totalDashboardFiles) && !hasSubscriptionsOtherFile) {
      console.log(`❌ [storage-loader] No files defined in manifest for dashboard: ${dashboardId}`);
      return false;
    }

    console.log(`📂 [storage-loader] Dashboard has ${totalDashboardFiles} file(s) across ${dashboards.length} manifest section(s)`);
    dashboards.forEach(d => {
      (d.files || []).forEach(f => console.log(`   - [${d.id}] ${f.type}: ${f.filename}`));
    });

    const loadedFiles = [];
    for (const dashboard of dashboards) {
      const filesForSection = await loadFilesFromManifest(client, manifest, dashboard, step, selectedFolder);
      loadedFiles.push(...filesForSection);
    }

    if (dashboardId === 'quality_checks' && Array.isArray(manifest.manifest.other_files)) {
      const subsEntries = manifest.manifest.other_files
        .filter(f => String(f.type || '').toLowerCase() === 'subscriptions');
      for (const entry of subsEntries) {
        try {
          const resolved = await loadSingleManifestFile(client, manifest, entry, selectedFolder);
          if (resolved) loadedFiles.push(resolved);
        } catch (err) {
          console.warn(`[storage-loader] Could not load subscriptions file '${entry.filename}': ${err.message}`);
        }
      }
    }

    const normalizedFiles = shouldMergeByTypeForDashboard(dashboardId)
      ? mergeLoadedFilesByType(loadedFiles)
      : loadedFiles;
    console.log(`✅ [storage-loader] Loaded ${loadedFiles.length} files from storage (${normalizedFiles.length} payload(s) after normalization)`);

    if (normalizedFiles.length === 0) {
      console.warn(`⚠️ [storage-loader] No valid files loaded from manifest; using upload fallback UI`);
      return false;
    }
    
    // Trigger dashboard render
    if (typeof onFilesLoaded === 'function') {
      console.log(`🚀 [storage-loader] Triggering onFilesLoaded callback`);
      onFilesLoaded(normalizedFiles);
    } else {
      console.warn(`⚠️ [storage-loader] onFilesLoaded callback not found`);
    }

    return true;
  } catch (err) {
    console.error(`❌ [storage-loader] Error during auto-load:`, err);
    console.error(`   Message: ${err.message}`);
    console.error(`   Stack:`, err.stack);
    if (err && err.message === 'No data rows found in CSV.') {
      alert(err.message);
    }
    return false;
  }
}

function hasCsvDataRows(content) {
  if (typeof content !== 'string') return false;
  const nonEmptyLines = content
    .replace(/^\uFEFF/, '')
    .split(/\r?\n/)
    .map(line => line.trim())
    .filter(Boolean);
  return nonEmptyLines.length > 1;
}

async function loadFilesFromManifest(client, manifest, dashboard, onStep, selectedFolder = '') {
  const step = (msg) => { if (typeof onStep === 'function') onStep(msg); };
  const files = [];
  const containerName = manifest.container;
  const normalizedFolder = String(selectedFolder || '').trim().replace(/^\/+|\/+$/g, '');
  console.log(`📥 [storage-loader] Downloading files from container: ${containerName}`);

  for (const [i, fileInfo] of dashboard.files.entries()) {
    try {
      step(`Downloading file ${i + 1} of ${dashboard.files.length}…`);
      const rawName = String(fileInfo.filename || '').replace(/^\/+/, '');
      const candidates = [];
      if (normalizedFolder && !rawName.includes('/')) {
        candidates.push(`${normalizedFolder}/${rawName}`);
      }
      // In folder mode, do not fall back to root paths to prevent cross-customer data leaks.
      if (!normalizedFolder || rawName.includes('/')) {
        candidates.push(rawName);
      }

      console.log(`   ↓ Fetching ${fileInfo.type}: ${rawName} (candidates: ${candidates.join(', ')})`);

      let content = null;
      let resolvedName = rawName;
      let lastError = null;
      for (const candidate of candidates) {
        try {
          content = await client.downloadBlob(containerName, candidate);
          resolvedName = candidate;
          break;
        } catch (candidateErr) {
          lastError = candidateErr;
          console.log(`   ↪ Candidate failed: ${candidate}`);
        }
      }

      if (content === null) {
        throw lastError || new Error(`Failed to download any candidate for ${rawName}`);
      }

      if (!hasCsvDataRows(content)) {
        console.warn(`   ⚠️ Empty CSV in manifest: ${resolvedName}`);
        throw new Error('No data rows found in CSV.');
      }
      console.log(`   ✓ Downloaded ${fileInfo.type}: ${resolvedName} (${content.length} bytes)`);
      files.push({
        name: resolvedName,
        type: fileInfo.type,
        content: content,
          export_generated_at_utc: fileInfo.export_generated_at_utc || '',
      });
    } catch (err) {
      console.error(`   ✗ Failed to load ${fileInfo.type}: ${fileInfo.filename}`, err.message);
    }
  }

  return files;
}

async function loadSingleManifestFile(client, manifest, fileInfo, selectedFolder = '') {
  const containerName = manifest.container;
  const normalizedFolder = String(selectedFolder || '').trim().replace(/^\/+|\/+$/g, '');
  const rawName = String(fileInfo.filename || '').replace(/^\/+/, '');
  const candidates = [];

  if (normalizedFolder && !rawName.includes('/')) {
    candidates.push(`${normalizedFolder}/${rawName}`);
  }
  if (!normalizedFolder || rawName.includes('/')) {
    candidates.push(rawName);
  }

  let content = null;
  let resolvedName = rawName;
  let lastError = null;
  for (const candidate of candidates) {
    try {
      content = await client.downloadBlob(containerName, candidate);
      resolvedName = candidate;
      break;
    } catch (candidateErr) {
      lastError = candidateErr;
    }
  }

  if (content === null) {
    throw lastError || new Error(`Failed to download any candidate for ${rawName}`);
  }
  if (!hasCsvDataRows(content)) {
    return null;
  }

  return {
    name: resolvedName,
    type: fileInfo.type || 'summary',
    content,
      export_generated_at_utc: fileInfo.export_generated_at_utc || '',
  };
}

function mergeCsvContents(csvContents) {
  const rows = [];
  const seen = new Set();
  let header = null;

  (csvContents || []).forEach(content => {
    if (typeof content !== 'string') return;
    const lines = content
      .replace(/^\uFEFF/, '')
      .split(/\r?\n/)
      .map(line => line.trim())
      .filter(Boolean);

    if (!lines.length) return;
    if (!header) header = lines[0];

    lines.slice(1).forEach(line => {
      if (seen.has(line)) return;
      seen.add(line);
      rows.push(line);
    });
  });

  if (!header) return '';
  return [header, ...rows].join('\n');
}

function mergeLoadedFilesByType(files) {
  const groups = new Map();

  (files || []).forEach(file => {
    const type = file.type || 'unknown';
    if (!groups.has(type)) groups.set(type, []);
    groups.get(type).push(file);
  });

  const merged = [];
  groups.forEach((typedFiles, type) => {
    if (typedFiles.length === 1) {
      merged.push(typedFiles[0]);
      return;
    }

    const mergedContent = mergeCsvContents(typedFiles.map(file => file.content || ''));
    const mergedName = typedFiles.map(file => file.name).join(' | ');

    console.log(`   ↻ Merged ${typedFiles.length} file(s) for type '${type}'`);
    merged.push({
      name: mergedName,
      type,
      content: mergedContent,
      sourceFiles: typedFiles.map(file => file.name),
    });
  });

  return merged;
}

/**
 * Helper: Convert loaded storage files to dashboard format
 * Returns object with summaryContent, timeseriesContent, and costsContent
 */
function organizeLoadedFiles(loadedFiles) {
  const organized = {
    summaryContent: null,
    timeseriesContent: null,
    costsContent: null,
  };

  for (const file of loadedFiles) {
    if (file.type === 'summary') {
      organized.summaryContent = file.content;
    } else if (file.type === 'timeseries') {
      organized.timeseriesContent = file.content;
    } else if (file.type === 'costs_by_resource' || file.type === 'costs') {
      organized.costsContent = file.content;
    }
  }

  return organized;
}

/**
 * Hide file upload UI when files are automatically loaded from storage
 */
function hideFileUploadUI() {
  const uploadArea = document.getElementById('uploadArea');
  const dropZone = document.querySelector('.drop-zone');
  
  if (uploadArea) uploadArea.style.display = 'none';
  if (dropZone) dropZone.style.display = 'none';
}

/**
 * Show file upload UI (fallback when no storage connection)
 */
function showFileUploadUI() {
  const uploadArea = document.getElementById('uploadArea');
  const dropZone = document.querySelector('.drop-zone');
  
  if (uploadArea) uploadArea.style.display = 'block';
  if (dropZone) dropZone.style.display = 'block';
}
