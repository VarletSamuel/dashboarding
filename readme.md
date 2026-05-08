# Azure Managed Services Dashboarding

A comprehensive data extraction and visualization suite for Azure resource monitoring, cost analysis, and performance tracking across multiple customer environments.

## Table of Contents

1. [Quick Start](#quick-start)
2. [Prerequisites](#prerequisites)
3. [Extractor Scripts](#extractor-scripts)
4. [Dashboards](#dashboards)
5. [File Upload & Storage](#file-upload--storage)
6. [Future Enhancements](#future-enhancements)

---

## Quick Start

### Basic Usage
```bash
cd extractor
python ManagedServiceWrapper.py -c CUST
```

This runs all extraction scripts for the CUST customer and generates reports in `../reports/CUST_<timestamp>/`.

---

## Prerequisites

### System Requirements
- **Python**: 3.9+
- **Azure CLI**: `az` command available on PATH
- **Internet**: Access to Azure APIs and storage services

### Python Dependencies

Install required packages:
```bash
pip install azure-identity azure-storage-blob requests ExcelJS
```

Key packages by functionality:
- **azure-identity**: Authentication to Azure (CLI, Service Principal, Managed Identity)
- **azure-storage-blob**: Blob storage connection and file operations
- **requests**: HTTP API calls to Azure Management API
- **ExcelJS**: Excel file generation for dashboards

### Azure Permissions

Minimum roles required for each script:

| Script | Required Role | Scope |
|--------|---------------|-------|
| `get_subscriptions.py` | Reader | Subscription |
| `get_daily_costs.py` | Cost Management Reader | Subscription |
| `get_reserved_instances.py` | Reader | Subscription |
| `get_virtualmachines.py` | Reader | Subscription |
| `get_containerApps.py` | Reader | Subscription |
| `get_appserviceplans.py` | Reader | Subscription |
| `get_eventhubnamespaces.py` | Reader | Subscription |
| `get_orphaned_resources.py` | Reader | Subscription |

### Customer Configuration

Each customer requires a JSON configuration file in `customers/<CUSTOMER>.json`:

```json
{
  "name": "CUSTOMER",
  "authentication": {
    "client_id": "optional-app-id-for-service-principal"
  },
  "azure": [
    {
      "tenant_id": "00000000-0000-0000-0000-000000000000",
      "subscriptions": [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222"
      ]
    }
  ]
}
```

---

## Extractor Scripts

### Using the Wrapper (Recommended)

The `ManagedServiceWrapper.py` orchestrates all extractor scripts with unified parameter handling and logging.

#### Wrapper Parameters

```
REQUIRED:
  -c, --customer CUST
        Customer code (4 digit code as prefixed in the customer\CUST.json file)

COMMON OPTIONS:
  -i, --input <path>
        Override customer JSON path. Default: ../customers/<CUSTOMER>.json

  --output-dir <path>
        Root output directory. Default: ../reports
        Each run creates: <output-dir>/<CUSTOMER>_<YYYYMMDD_HHMM>/

  --output-format {csv|json|both}
        File format for extractors. Default: both

  --skip-login
        Skip 'az login --tenant'. Use if already authenticated to all tenants.

SERVICE PRINCIPAL (Non-Interactive Auth):
  --sp-client-id <APP_ID>
        App Registration client ID for service principal login.

  --sp-client-secret <SECRET>
        Client secret (falls back to AZURE_SP_CLIENT_SECRET env var if not provided).

  --sp-certificate <CERT_PATH>
        Path to PEM certificate (alternative to --sp-client-secret).

DATE FILTERING (Forwarded to cost and metrics scripts):
  --from <YYYY-MM-DD>
        Start date. Default: first day of previous month.

  --to <YYYY-MM-DD>
        End date. Default: today.

  --lookback <MINUTES|ISO-8601>
        Metrics window: integer minutes or ISO-8601 (e.g., PT6H, PT30M).
        Overrides default date range for: get_eventhubnamespaces,
        get_containerApps, get_appserviceplans.

RESERVED INSTANCES:
  --no-utilisation
        Skip RI utilisation fetch (speeds up run if not needed).

SELECTIVE EXECUTION:
  --skip <SCRIPT1> [SCRIPT2] ...
        Skip specific scripts by name (without .py extension).
        Example: --skip get_daily_costs get_reserved_instances

  --only <SCRIPT1> [SCRIPT2] ...
        Run ONLY the listed scripts. Overrides --skip.
        Example: --only get_daily_costs get_eventhubnamespaces
```

#### Wrapper Examples

```bash
# Basic run (all scripts, interactive login)
python ManagedServiceWrapper.py -c CUST

# Specific customer JSON and output directory
python ManagedServiceWrapper.py -c CUST -i /path/to/custom.json --output-dir ./my_reports

# Custom date range (cost analysis)
python ManagedServiceWrapper.py -c CUST --from 2026-01-01 --to 2026-05-07

# Metrics lookback (last 6 hours)
python ManagedServiceWrapper.py -c CUST --lookback PT6H

# Service Principal (non-interactive CI/CD)
python ManagedServiceWrapper.py -c CUST \
  --sp-client-id xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx \
  --sp-client-secret "****************************"

# Service Principal with certificate
python ManagedServiceWrapper.py -c CUST \
  --sp-client-id   --sp-client-id xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx \
  --sp-certificate /path/to/cert.pem

# Skip RI utilisation (faster for cost-only runs)
python ManagedServiceWrapper.py -c CUST --no-utilisation

# Run only cost and VM data
python ManagedServiceWrapper.py -c CUST --only get_daily_costs get_virtualmachines

# Run all except costs and reserved instances
python ManagedServiceWrapper.py -c CUST --skip get_daily_costs get_reserved_instances

# Already logged in to all tenants, so skip login
python ManagedServiceWrapper.py -c CUST --skip-login

# CSV format only (no JSON)
python ManagedServiceWrapper.py -c CUST --output-format csv

# Combined: costs for last month + skip RI utilisation
python ManagedServiceWrapper.py -c CUST \
  --from 2026-04-01 --to 2026-04-30 \
  --no-utilisation
```

#### Output Structure

Each wrapper run creates a timestamped directory:
```
reports/
└── CUST_20260508_1430/
    ├── CUST_log.txt                                    # Full execution log
    ├── subscriptions_CUST.csv                          # All subscriptions
    ├── daily_costs_by_resource_CUST_2026-04-01_2026-05-07.csv
    ├── reserved_instances_CUST.csv
    ├── virtualmachines_CUST.csv
    ├── container_apps_metrics_CUST_2026-05-01_2026-05-07.csv
    ├── appservice_plans_metrics_CUST_2026-05-01_2026-05-07.csv
    └── eventhub_namespaces_metrics_CUST_2026-05-01_2026-05-07.csv
```

---

### Running Individual Scripts

Each extractor script can be run independently for debugging or targeted data collection.

#### get_subscriptions.py
Lists all subscriptions for a customer.

```bash
# Interactive (uses az login)
python get_subscriptions.py -i ../customers/CUST.json

# Service Principal
python get_subscriptions.py -i ../customers/CUST.json \
  --sp-client-id <APP_ID> --sp-client-secret <SECRET>

# Already logged in
python get_subscriptions.py -i ../customers/CUST.json --skip-login

# JSON output only
python get_subscriptions.py -i ../customers/CUST.json --output-format json
```

#### get_daily_costs.py
Fetches daily cost data from Azure Cost Management API.

```bash
# Default: previous month to today
python get_daily_costs.py -i ../customers/CUST.json

# Custom date range
python get_daily_costs.py -i ../customers/CUST.json \
  --from 2026-01-01 --to 2026-05-07

# Specific subscription only
python get_daily_costs.py -s <subscription-id>

# Service Principal (non-interactive)
python get_daily_costs.py -i ../customers/CUST.json \
  --sp-client-id <APP_ID> --sp-client-secret <SECRET> --skip-login
```

#### get_reserved_instances.py
Analyzes reserved instance recommendations and utilization.

```bash
# Standard run
python get_reserved_instances.py -i ../customers/CUST.json

# Skip utilisation calculations (faster)
python get_reserved_instances.py -i ../customers/CUST.json --no-utilisation

# Service Principal
python get_reserved_instances.py -i ../customers/CUST.json \
  --sp-client-id <APP_ID> --sp-client-secret <SECRET> --skip-login
```

#### get_virtualmachines.py
Collects VM configurations and performance metrics.

```bash
# All VMs for customer
python get_virtualmachines.py -i ../customers/CUST.json

# Specific subscription
python get_virtualmachines.py -s <subscription-id>

# Service Principal
python get_virtualmachines.py -i ../customers/CUST.json \
  --sp-client-id <APP_ID> --sp-client-secret <SECRET> --skip-login
```

#### get_containerApps.py
Metrics and configuration for Azure Container Apps.

```bash
# Last 24 hours
python get_containerApps.py -i ../customers/CUST.json

# Custom date range
python get_containerApps.py -i ../customers/CUST.json \
  --from 2026-05-01 --to 2026-05-07

# Lookback window (e.g., 6 hours)
python get_containerApps.py -i ../customers/CUST.json --lookback PT6H
```

#### get_appserviceplans.py
Azure App Service Plans and Web Apps metrics.

```bash
# Last 24 hours
python get_appserviceplans.py -i ../customers/CUST.json

# Custom date range
python get_appserviceplans.py -i ../customers/CUST.json \
  --from 2026-05-01 --to 2026-05-07

# Last 12 hours lookback
python get_appserviceplans.py -i ../customers/CUST.json --lookback PT12H
```

#### get_eventhubnamespaces.py
Event Hub namespace metrics and throughput analysis.

```bash
# Last 24 hours
python get_eventhubnamespaces.py -i ../customers/CUST.json

# Custom date range
python get_eventhubnamespaces.py -i ../customers/CUST.json \
  --from 2026-05-01 --to 2026-05-07

# Last 6 hours
python get_eventhubnamespaces.py -i ../customers/CUST.json --lookback PT6H
```

#### get_orphaned_resources.py
Identifies unused resources (unattached disks, empty storage accounts, etc.).

```bash
# Check all subscriptions
python get_orphaned_resources.py -i ../customers/CUST.json

# Specific subscription
python get_orphaned_resources.py -s <subscription-id>
```

---

## Dashboards

Interactive HTML dashboards for visualizing extracted data. Located in `dashboards/`.

### Available Dashboards

| Dashboard | Purpose | Data Source |
|-----------|---------|-------------|
| **azureCosts.html** | Daily cost breakdown by resource type, subscription, and resource group | `daily_costs_by_resource_*.csv` |
| **containerApps.html** | Container Apps performance, replicca metrics, environment utilization | `container_apps_metrics_*.csv` |
| **appServicePlan.html** | App Service Plans CPU, memory, request rates by tier | `appservice_plans_metrics_*.csv` |
| **eventhubs.html** | Event Hub throughput, consumer lag, partition metrics | `eventhub_namespaces_metrics_*.csv` |
| **virtualMachines.html** | VM inventory, sizing recommendations, performance by size | `virtualmachines_*.csv` |
| **reservedInstances.html** | RI recommendations, savings analysis, commitment terms | `reserved_instances_*.csv` |
| **cosmos.html** | Cosmos DB RU reservation and scaling analysis | Integrated cost analysis |
| **devops.html** | Azure DevOps pipeline health and execution metrics | Azure DevOps REST API |
| **healthChecks.html** | Service health status and availability monitoring | Azure Service Health API |
| **qualityChecks.html** | Code quality and security compliance metrics | Customer-specific data |

### Opening a Dashboard

1. Copy the desired CSV file from `reports/<CUSTOMER>_<timestamp>/` to your local machine
2. Open the corresponding HTML dashboard in a web browser
3. Click **"Choose Files"** and select the CSV file(s)
4. The dashboard populates instantly with interactive visualizations

---

## File Upload & Storage

Dashboards support two methods for loading data:

### Method 1: Local File Upload (Quick Testing)

1. Open the HTML dashboard in your browser
2. Click the **"Choose Files"** button
3. Select one or more CSV files from your computer
4. Click "Open"
5. Dashboard renders immediately

**Supports**:
- Single file upload
- Multiple file selection (combines data automatically)
- Drag & drop (in supported browsers)

### Method 2: Azure Storage Connection (Production)

Connect dashboards to a public Azure Storage account with shared access for automatic data loading.

#### Prerequisites

1. **Azure Storage Account** with public or SAS-authenticated access
2. **Connection String** or **SAS Token** to the container
3. **Manifest File** (`manifest.json`) describing available CSV files

#### Storage Structure

```
container/
├── manifest.json
├── costs_2026-05.csv
├── vms_2026-05.csv
└── ... (other CSV files)
```

#### Manifest Format (`manifest.json`)

```json
{
  "files": [
    {
      "name": "costs_2026-05.csv",
      "type": "costs",
      "customer": "CUST",
      "date": "2026-05-07"
    },
    {
      "name": "vms_2026-05.csv",
      "type": "vms",
      "customer": "CUST",
      "date": "2026-05-07"
    }
  ]
}
```

#### Connection String Method

1. In dashboard HTML, set storage connection details in `bin/storage-loader.js`:

```javascript
const STORAGE_CONFIG = {
  connectionString: "DefaultEndpointProtocol=https;AccountName=...;AccountKey=...;EndpointSuffix=core.windows.net",
  containerName: "dashboards",
  manifestPath: "manifest.json"
};
```

2. Dashboard auto-loads files on page load via `window.onFilesLoaded()` callback

#### SAS Token Method

Use shared access signatures for time-limited, secure access:

```javascript
const STORAGE_CONFIG = {
  accountName: "mystorageaccount",
  containerName: "dashboards",
  sasToken: "sv=2021-06-08&ss=bfqt&srt=sco&sp=rwdlacupitfx&se=2026-12-31T23:59:59Z&st=2026-01-01T00:00:00Z&spr=https&sig=...",
  manifestPath: "manifest.json"
};
```

#### Environment Variable Method

Store connection details in environment variables:

```bash
export DASHBOARD_STORAGE_CONNECTION="DefaultEndpointProtocol=https;..."
export DASHBOARD_CONTAINER="dashboards"
export DASHBOARD_SAS_TOKEN="sv=2021-06-08..."
```

Then reference in dashboard:
```javascript
const STORAGE_CONFIG = {
  connectionString: process.env.DASHBOARD_STORAGE_CONNECTION,
  containerName: process.env.DASHBOARD_CONTAINER,
  sasToken: process.env.DASHBOARD_SAS_TOKEN,
  manifestPath: "manifest.json"
};
```

---

## Future Enhancements

### High Priority

#### 1. Automatic Script Execution on Azure
- **Goal**: Schedule regular data extraction without manual intervention
- **Implementation**:
  - Deploy wrapper as **Azure Automation Runbook** or **Azure Function** (scheduled trigger)
  - Service Principal with minimal RBAC roles for each customer
  - Automatic output upload to storage account
  - Status notifications via Logic Apps or Service Bus
- **Benefit**: Always-fresh data in dashboards

#### 2. Data Refresh & Manifest Auto-Update
- **Goal**: Keep `manifest.json` synchronized with latest CSV files
- **Implementation**:
  - Blob Storage event trigger on file upload
  - Azure Function processes trigger → updates manifest.json
  - Dashboard detects manifest changes → auto-reloads data
  - Retention policy: Keep 30 days of historical runs
- **Benefit**: Zero-touch dashboard updates

#### 3. Cost & Metrics Database
- **Goal**: Historical trend analysis across months
- **Implementation**:
  - Store extracted data in **Azure SQL Database** or **Cosmos DB**
  - ETL pipeline normalizes CSVs → database tables
  - Dashboards query database instead of CSV files
  - Enable time-series analysis and anomaly detection
- **Benefit**: Long-term trend analysis, forecasting

### Medium Priority

#### 4. Additional Resource Types
- **Scope Extension**:
  - **Databases**: SQL Database, PostgreSQL, MySQL managed instances
  - **Analytics**: Data Factory, Synapse, Data Lake Storage usage
  - **Networking**: ExpressRoute, VPN Gateway, Load Balancer metrics
  - **Security**: Key Vault access logs, firewall rules, compliance state
  - **AI/ML**: Cognitive Services, Machine Learning usage and costs
- **Implementation**: New extractor scripts following existing pattern
- **Benefit**: Unified view of entire Azure estate

#### 5. Custom Report Generation
- **Goal**: Export dashboard filters to PDF/Excel reports
- **Implementation**:
  - Add "Export Report" button to each dashboard
  - Generate formatted Excel workbooks (like azure-cost-dashboard)
  - PDF generation via Playwright or similar
  - Email distribution via SendGrid or Logic Apps
- **Benefit**: Executive summaries, compliance reports

### Long-term Priority

#### 6. Azure AD / Entra ID Authentication
- **Goal**: Dashboard access restricted to authenticated users
- **Implementation**:
  - Wrap dashboards in **Azure Static Web App** with Entra ID auth
  - Per-customer isolation (user can only see their data)
  - Audit logging of who accessed what data and when
  - Role-based access (read-only vs. admin roles)
- **Benefit**: Enterprise security, audit trail, multi-tenancy

#### 7. Real-Time Alerts & Anomaly Detection
- **Goal**: Proactive notification of cost spikes, resource failures
- **Implementation**:
  - Statistical anomaly detection on cost trends
  - Threshold-based alerts (e.g., > 10% daily cost increase)
  - Integration with **Azure Monitor** for resource health
  - Alert distribution: Email, Teams, PagerDuty
- **Benefit**: Rapid incident response

#### 8. FinOps Dashboard & Chargeback
- **Goal**: Cost allocation and budget management
- **Implementation**:
  - Tag-based cost allocation to business units
  - Monthly billing workbooks by department
  - Showback reports for cost accountability
  - Budget vs. actual tracking
- **Benefit**: Cost accountability and forecasting

---

## Support & Troubleshooting

### Common Issues

**Q: "az login" fails**
- Ensure Azure CLI is installed: `az --version`
- Log out and try again: `az logout && az login`
- For Service Principal: verify client ID, secret, and tenant are correct

**Q: "No valid records found" after file upload**
- Verify CSV file format matches extractor output
- Check for encoding issues (should be UTF-8)
- Ensure file contains data rows (not just headers)

**Q: Storage account connection fails**
- Verify SAS token expiry: `not after 2026-12-31`
- Check network access rules (public or firewall whitelist)
- Confirm `manifest.json` exists in container root

**Q: Dashboard filters not working**
- Clear browser cache: Ctrl+Shift+Delete
- Verify CSV has required columns (subscription, date, etc.)
- Check browser console for errors: F12 → Console tab

### Debug Mode

Enable verbose logging:
```bash
# Wrapper debug
python ManagedServiceWrapper.py -c CUST --debug

# Individual script debug
python get_daily_costs.py -i ../customers/CUST.json --verbose
```

Check log files in `reports/<CUSTOMER>_<timestamp>/<CUSTOMER>_log.txt`.

---

**Last Updated**: May 2026  
**Version**: 1.0
