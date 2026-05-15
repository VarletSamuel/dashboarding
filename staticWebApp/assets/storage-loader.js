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
        if (key === 'DefaultEndpointProtocol' && value === 'https') {
          // endpoint will be constructed from AccountName
        }
      });
      if (result.accountName && result.accountKey) {
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
    
    console.log(`[AzureStorageClient.listContainers] API call: ${this.parsedConnection.endpoint}/?comp=list`);
    const url = new URL(`${this.parsedConnection.endpoint}/?comp=list`);
    if (this.parsedConnection.sasToken) {
      url.search = this.parsedConnection.sasToken;
    }

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
    if (this.parsedConnection.sasToken) {
      url.search = this.parsedConnection.sasToken + (prefix ? `&prefix=${encodeURIComponent(prefix)}` : '');
    }

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
    if (this.parsedConnection.sasToken) {
      url.search = this.parsedConnection.sasToken;
    }

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

  async findManifests() {
    console.log(`[AzureStorageClient.findManifests] Starting manifest search...`);
    const manifests = [];
    const { container, pathPrefix } = this.parsedConnection;

    if (container) {
      // Scoped SAS (container- or directory-level): go directly to manifest.json
      // downloadBlob will automatically prepend pathPrefix
      const manifestBlobName = 'manifest.json';
      console.log(`[AzureStorageClient.findManifests] Scoped SAS detected → direct download: ${container}/${pathPrefix}${manifestBlobName}`);
      try {
        const content = await this.downloadBlob(container, manifestBlobName);
        const json = JSON.parse(content);
        manifests.push({ container, blob: `${pathPrefix}${manifestBlobName}`, manifest: json });
        console.log(`[AzureStorageClient.findManifests] ✓ Found manifest for: ${json.customer}`);
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

async function initStorageFileLoader(dashboardId, onStep) {
  const step = (msg) => { if (typeof onStep === 'function') onStep(msg); };
  console.log(`🔄 [storage-loader] Initializing for dashboard: ${dashboardId}`);
  step('Connecting to storage…');
  
  const STORAGE_CONFIG_KEY = 'dco-storage-connection';

  // Read connection from URL hash (#conn=...) first — injected by index.html when navigating.
  // This is needed because file:// localStorage is scoped per-directory in some browsers.
  let connection = null;
  const hash = window.location.hash;
  if (hash.startsWith('#conn=')) {
    connection = decodeURIComponent(hash.slice(6));
    // Persist locally when provided; clear local copy when hash is explicitly empty.
    if (connection) {
      localStorage.setItem(STORAGE_CONFIG_KEY, connection);
    } else {
      localStorage.removeItem(STORAGE_CONFIG_KEY);
    }
    // Remove hash from URL without triggering a reload
    history.replaceState(null, '', window.location.pathname + window.location.search);
    console.log(`✓ [storage-loader] Connection read from URL hash and saved to localStorage`);
  } else {
    connection = localStorage.getItem(STORAGE_CONFIG_KEY);
  }

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
    const manifests = await client.findManifests();
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

    // Find manifest that contains this dashboard
    console.log(`🔎 [storage-loader] Searching for dashboard: ${dashboardId}`);
    const relevantManifests = manifests.filter(m => {
      return m.manifest.dashboards?.some(d => d.id === dashboardId);
    });

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

    // Load files for this dashboard
    const dashboard = manifest.manifest.dashboards.find(d => d.id === dashboardId);
    if (!dashboard || !dashboard.files) {
      console.log(`❌ [storage-loader] No files defined in manifest for dashboard: ${dashboardId}`);
      return false;
    }

    console.log(`📂 [storage-loader] Dashboard has ${dashboard.files.length} file(s):`);
    dashboard.files.forEach(f => console.log(`   - ${f.type}: ${f.filename}`));

    const loadedFiles = await loadFilesFromManifest(client, manifest, dashboard, step);
    const normalizedFiles = mergeLoadedFilesByType(loadedFiles);
    console.log(`✅ [storage-loader] Loaded ${loadedFiles.length} files from storage (${normalizedFiles.length} type payload(s) after merge)`);

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

async function loadFilesFromManifest(client, manifest, dashboard, onStep) {
  const step = (msg) => { if (typeof onStep === 'function') onStep(msg); };
  const files = [];
  const containerName = manifest.container;
  console.log(`📥 [storage-loader] Downloading files from container: ${containerName}`);

  for (const [i, fileInfo] of dashboard.files.entries()) {
    try {
      step(`Downloading file ${i + 1} of ${dashboard.files.length}…`);
      console.log(`   ↓ Fetching ${fileInfo.type}: ${fileInfo.filename}`);
      const content = await client.downloadBlob(containerName, fileInfo.filename);
      if (!hasCsvDataRows(content)) {
        console.warn(`   ⚠️ Empty CSV in manifest: ${fileInfo.filename}`);
        throw new Error('No data rows found in CSV.');
      }
      console.log(`   ✓ Downloaded ${fileInfo.type} (${content.length} bytes)`);
      files.push({
        name: fileInfo.filename,
        type: fileInfo.type,
        content: content,
      });
    } catch (err) {
      console.error(`   ✗ Failed to load ${fileInfo.type}: ${fileInfo.filename}`, err.message);
    }
  }

  return files;
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
