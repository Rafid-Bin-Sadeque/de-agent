## Athena

### Workgroups (24h)

| workgroup | queries | succeeded | failed | cancelled | scanned |
|---|---|---|---|---|---|
| primary | 42 | 41 | 1 | 0 | 3.0 GB |

### Top queries by data scanned

| workgroup | query | state | scanned | text |
|---|---|---|---|---|
| primary | q-0001 | SUCCEEDED | 1.0 GB | select count(*) from bronze.orders where dt = ? |

### Failed queries

| workgroup | query | at | reason |
|---|---|---|---|
| primary | q-0002 | 2026-10-20 09:00 | SYNTAX_ERROR: line 1:8: Column 'x' cannot be resolved |

