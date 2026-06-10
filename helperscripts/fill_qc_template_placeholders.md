# fill_qc_template.py Placeholder Reference

This document lists all placeholders currently present in:

- `templates/*_QualityControls_v*.docx`

and how `helperscripts/fill_qc_template.py` handles them.

## Scalar Placeholders

These are replaced via `replace_scalar()` using values from `payload["scalars"]`.

| Placeholder | Source |
|---|---|
| `{{CUSTOMER}}` | CLI argument `--customer` (uppercased) |
| `{{DATE}}` | Current local date (`%d %B %Y`) |
| `{{SCAN_DATE}}` | `manifest.generated_at` (fallback: current ISO date) |
| `{{SCAN_OPERATOR}}` | `USERNAME`/`USER` env var (fallback: `Unknown`) |
| `{{TENANT}}` | Tenant(s) derived from subscriptions export |

## Table Placeholders

These are detected dynamically (`{{TABLE_*}}`) and replaced with a Word table.

### Direct / alias-backed placeholders

| Placeholder | Resolution |
|---|---|
| `{{TABLE_SUBSCRIPTIONS_IN_SCOPE}}` | `payload.tables.SUBSCRIPTIONS_IN_SCOPE` |
| `{{TABLE_SUBSCRIPTIONS_OUT_OF_SCOPE}}` | `payload.tables.SUBSCRIPTIONS_OUT_OF_SCOPE` |
| `{{TABLE_SUBSCRIPTIONS_QUOTAS}}` | `payload.tables.SUBSCRIPTIONS_QUOTAS` from `quotaConsumption` summary (fallback: latest `*quota_consumption_summary*.csv`) |
| `{{TABLE_APPSERVICE}}` | `payload.tables.APPSERVICE` |
| `{{TABLE_APP_SERVICE_PLAN}}` | Alias to App Service data (`APPSERVICE`) with columns: ASP Name, SKU & Instances, Number of applications, Cost signal, CPU p95%, Memory p95% |
| `{{TABLE_KEYVAULT}}` | `payload.tables.KEYVAULT` |
| `{{TABLE_STORAGE}}` | Alias to `payload.tables.STORAGE_ACCOUNTS` |
| `{{TABLE_SQL}}` | `payload.tables.SQL` |
| `{{TABLE_VM}}` | Alias to dashboard/fallback key `VIRTUALMACHINES` |
| `{{TABLE_CONTAINER_APPS_AKS}}` | Alias to dashboard/fallback key `CONTAINERAPPS` |
| `{{TABLE_EVENTHUB_SB}}` | Alias to dashboard/fallback key `EVENTHUBS` |
| `{{TABLE_APIM}}` | Dashboard/fallback key `APIM` |
| `{{TABLE_ACR}}` | Dashboard/fallback key `ACR` |
| `{{TABLE_APP_CONFIG}}` | Dashboard/fallback key `APPCONFIG` |
| `{{TABLE_APP_INSIGHTS}}` | Dashboard/fallback key `APPINSIGHTS` |
| `{{TABLE_COGNITIVE}}` | Dashboard/fallback key `COGNITIVE` |
| `{{TABLE_COSMOSDB}}` | Dashboard/fallback key `COSMOSDB` |
| `{{TABLE_LOGIC_DF}}` | Dashboard/fallback key `LOGICDF` |
| `{{TABLE_OVERALL_POSTURE}}` | Alias key `OVERALL_POSTURE` (typically fallback unless mapped) |
| `{{TABLE_AGREED_ACTIONS}}` | Alias key `AGREED_ACTIONS` (typically fallback unless mapped) |
| `{{TABLE_ENTRA_SECRETS_EXPIRED}}` | `payload.tables.ENTRA_SECRETS_EXPIRED` from `appSecretExpirations` summary |
| `{{TABLE_ENTRA_SECRETS_EXPIRING_90D}}` | `payload.tables.ENTRA_SECRETS_EXPIRING_90D` from `appSecretExpirations` summary (`status=expiring`, `days_remaining<=90`) |
| `{{TABLE_ENTRA_SECRETS_EXPIRING_180D}}` | `payload.tables.ENTRA_SECRETS_EXPIRING_180D` from `appSecretExpirations` summary (`91<=days_remaining<=180`) |
| `{{TABLE_ENTRA_SECRETS_NOEXPIRY}}` | `payload.tables.ENTRA_SECRETS_NOEXPIRY` from `appSecretExpirations` summary (`end_utc` empty) |

### Fallback behavior for unmapped tables

If no mapped data exists, the script inserts a default 2-column table:

- Header: `Status`, `Details`
- Row: `No data mapping`, `No extractor output is currently mapped for TABLE_<KEY>.`

This ensures all `TABLE_*` anchors in the template are replaced.

## Recommendation Placeholders

These are detected dynamically (`{{RECOMMENDATIONS_*}}`) and replaced with paragraphs.

| Placeholder | Resolution |
|---|---|
| `{{RECOMMENDATIONS_KEYVAULT}}` | `payload.recommendations.KEYVAULT` |
| `{{RECOMMENDATIONS_APPSERVICE}}` | `payload.recommendations.APPSERVICE` |
| `{{RECOMMENDATIONS_APP_SERVICE_PLAN}}` | Alias to `APPSERVICE` (includes detailed cost-signal legend) |
| `{{RECOMMENDATIONS_STORAGE}}` | Alias to `STORAGE_ACCOUNTS` |
| `{{RECOMMENDATIONS_SQL}}` | `payload.recommendations.SQL` |
| `{{RECOMMENDATIONS_VM}}` | Alias key `VM` (fallback unless explicitly populated) |
| `{{RECOMMENDATIONS_EVENTHUB_SB}}` | Alias key `EVENTHUB_SB` (fallback unless explicitly populated) |
| `{{RECOMMENDATIONS_CONTAINER_APPS_AKS}}` | Alias key `CONTAINER_APPS_AKS` (fallback unless explicitly populated) |
| `{{RECOMMENDATIONS_APIM}}` | Alias key `APIM` (fallback unless explicitly populated) |
| `{{RECOMMENDATIONS_ACR}}` | Alias key `ACR` (fallback unless explicitly populated) |
| `{{RECOMMENDATIONS_APP_CONFIG}}` | Alias key `APP_CONFIG` (fallback unless explicitly populated) |
| `{{RECOMMENDATIONS_APP_INSIGHTS}}` | Alias key `APP_INSIGHTS` (fallback unless explicitly populated) |
| `{{RECOMMENDATIONS_COGNITIVE}}` | Alias key `COGNITIVE` (fallback unless explicitly populated) |
| `{{RECOMMENDATIONS_COSMOSDB}}` | Alias key `COSMOSDB` (fallback unless explicitly populated) |
| `{{RECOMMENDATIONS_LOGIC_DF}}` | Alias key `LOGIC_DF` (fallback unless explicitly populated) |
| `{{RECOMMENDATIONS_ENTRA_SECRETS}}` | `payload.recommendations.ENTRA_SECRETS` summary and action guidance from Entra expiry export |

### Fallback behavior for unmapped recommendations

If no mapped recommendation exists, the script inserts:

1. `No automated recommendation is currently mapped for RECOMMENDATIONS_<KEY>.`
2. `Please review this section manually based on customer-specific context.`

This ensures all `RECOMMENDATIONS_*` anchors in the template are replaced.

## Notes

- Placeholder detection in the script is dynamic for table/recommendation anchors.
- Scalar replacement supports placeholders split across runs/SDT sections.
- Template selection behavior:
  - `--template <path>`: use an explicit DOCX file.
  - `--template-version <suffix>`: choose `templates/*_QualityControls_v<suffix>.docx` (for example `1.1` or `SYNH`).
  - When no template argument is provided, the script scans `templates/` and picks the highest numeric version automatically.
