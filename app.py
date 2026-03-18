#!/usr/bin/env python3
"""
SEC SC TO-T Attorney Lookup — Web UI
======================================
Single-command web app: python3 app.py
Opens at http://localhost:8050

Backend proxies EDGAR filing HTML (browser can't due to CORS)
and extracts attorney info from the "Copies to:" section.
"""
import re
import json
import logging
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path

import requests
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('sc_tot_app')

app = FastAPI(title="SC TO-T Attorney Lookup")

EDGAR_SEARCH = 'https://efts.sec.gov/LATEST/search-index'
EDGAR_BASE = 'https://www.sec.gov/Archives/edgar/data'
USER_AGENT = 'CamacPartners/1.0 (Research; charlie@camacpartners.com)'


# ── HTML parsing helpers ─────────────────────────────────────────────

class HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
    def handle_data(self, data):
        self.parts.append(data)
    def get_text(self):
        return ' '.join(self.parts)

def html_to_text(html):
    e = HTMLTextExtractor()
    e.feed(html)
    return e.get_text()

def build_filing_url(adsh, cik, filename):
    return f'{EDGAR_BASE}/{cik}/{adsh.replace("-","")}/{filename}'

FIRM_RE = re.compile(
    r'\b(LLP|LLC|P\.?C\.?|L\.?P\.?|Inc\.?|P\.?A\.?|PLLC)\b|'
    r'\b(Skadden|Sullivan|Wachtell|Kirkland|Latham|Davis Polk|Simpson|'
    r'Cravath|Cleary|Debevoise|Paul.*Weiss|Fried.*Frank|Gibson.*Dunn|'
    r'Kilpatrick|Morgan.*Lewis|Jones Day|Willkie|Ropes|Sidley|'
    r'Weil.*Gotshal|White.*Case|Milbank|Proskauer|Covington|Goodwin|'
    r'Dechert|Mayer.*Brown|Baker.*McKenzie|Hogan.*Lovells|DLA|'
    r'Greenberg|Pillsbury|Norton.*Rose|King.*Spalding|Vinson|'
    r'Shearman|Cadwalader|Schulte|Orrick|Katten|Foley|Reed.*Smith|'
    r'Morrison.*Foerster|Stearns.*Weaver|Jackson.*Lewis)',
    re.IGNORECASE
)
PHONE_RE = re.compile(r'\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}')
ADDR_RE = re.compile(
    r'\d+\s+\w+.*(?:Street|St\.|Avenue|Ave\.|Boulevard|Blvd|Road|Rd|Plaza|Drive|Dr|Way|Place|Pl|Suite|Floor)',
    re.IGNORECASE
)
CITY_RE = re.compile(
    r'[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*,?\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*,?\s+\d{5}',
    re.IGNORECASE
)

def extract_copies_to(html):
    if not html:
        return []
    m = re.search(
        r'Copies\s+to:.*?(?=<(?:center|TABLE|DIV\s+STYLE="line-height)|$)',
        html, re.IGNORECASE | re.DOTALL
    )
    if not m:
        return []
    block = m.group(0)
    lines = []
    for p in re.finditer(r'<P[^>]*>(.*?)</P>', block, re.IGNORECASE | re.DOTALL):
        t = re.sub(r'\s+', ' ', html_to_text(p.group(1))).strip()
        if t and t != '\xa0':
            lines.append(t)
    if not lines:
        return []

    attorneys = []
    cur = {'names': [], 'firm': '', 'address': '', 'phone': ''}
    for line in lines:
        if re.match(r'^\s*Copies\s+to:\s*$', line, re.IGNORECASE):
            continue
        if PHONE_RE.search(line):
            cur['phone'] = PHONE_RE.search(line).group(0)
            if cur['names'] or cur['firm']:
                attorneys.append(dict(cur))
                cur = {'names': [], 'firm': '', 'address': '', 'phone': ''}
        elif FIRM_RE.search(line):
            cur['firm'] = line.strip()
        elif ADDR_RE.search(line):
            cur['address'] = (cur['address'] + ', ' + line.strip()) if cur['address'] else line.strip()
        elif CITY_RE.search(line):
            cur['address'] = (cur['address'] + ', ' + line.strip()) if cur['address'] else line.strip()
        else:
            cur['names'].append(line.strip())
    if cur['names'] or cur['firm']:
        attorneys.append(cur)

    return [
        {
            'attorney_names': '; '.join(n for n in a['names'] if n and len(n) > 2),
            'firm': a['firm'],
            'address': a['address'],
            'phone': a['phone'],
        }
        for a in attorneys
    ]


# ── API endpoints ────────────────────────────────────────────────────

@app.get("/api/search")
async def search_filings(
    start: str = Query(default=""),
    end: str = Query(default=""),
    limit: int = Query(default=100),
):
    """Search EDGAR for SC TO-T filings and extract attorney info."""
    if not start:
        start = (date.today() - timedelta(days=180)).isoformat()
    if not end:
        end = date.today().isoformat()

    # Step 1: search EDGAR
    params = {
        'forms': 'SC TO-T',
        'dateRange': 'custom',
        'startdt': start,
        'enddt': end,
        '_source': 'adsh,form,file_date,display_names,ciks,biz_locations',
        'size': limit,
    }
    try:
        r = requests.get(EDGAR_SEARCH, params=params,
                         headers={'User-Agent': USER_AGENT}, timeout=30)
        r.raise_for_status()
        hits = r.json().get('hits', {}).get('hits', [])
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=502)

    # Step 2: fetch each filing and extract attorneys
    results = []
    seen_adsh = set()
    for h in hits:
        s = h['_source']
        adsh = s['adsh']
        doc_id = h['_id']
        filename = doc_id.split(':')[1] if ':' in doc_id else ''
        ciks = s.get('ciks', [])
        companies = s.get('display_names', [])
        file_date = s.get('file_date', '')
        form = s.get('form', '')

        # Deduplicate by adsh (same filing, different amendments)
        if adsh in seen_adsh:
            continue
        seen_adsh.add(adsh)

        if not ciks or not filename:
            continue

        # Try fetching HTML
        url = build_filing_url(adsh, ciks[0], filename)
        html = None
        try:
            resp = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=20)
            if resp.status_code == 200:
                html = resp.text
            elif len(ciks) > 1:
                url = build_filing_url(adsh, ciks[1], filename)
                resp = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=20)
                if resp.status_code == 200:
                    html = resp.text
        except Exception:
            pass

        attorneys = extract_copies_to(html) if html else []
        # Clean company names (strip CIK info)
        clean_companies = []
        for c in companies:
            name = re.sub(r'\s*\(CIK\s+\d+\)', '', c).strip()
            name = re.sub(r'\s*\([A-Z]{1,5}\)', '', name).strip()
            clean_companies.append(name)

        for a in attorneys:
            # Skip garbage entries (underscores, blanks)
            if not a['firm'] and (not a['attorney_names'] or '___' in a['attorney_names']):
                continue
            results.append({
                'filing_date': file_date,
                'form': form,
                'companies': ' / '.join(clean_companies),
                'attorney_names': a['attorney_names'],
                'firm': a['firm'],
                'address': a['address'],
                'phone': a['phone'],
                'filing_url': url,
            })

    return JSONResponse(results)


@app.get("/", response_class=HTMLResponse)
async def index():
    return FRONTEND_HTML


# ── Frontend ─────────────────────────────────────────────────────────

FRONTEND_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SC TO-T Attorney Lookup</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg: #f3f4f6;
    --surface: #ffffff;
    --border: #e5e7eb;
    --text: #111827;
    --text-secondary: #6b7280;
    --accent: #1e3a5f;
    --accent-light: #e8eef4;
    --green: #059669;
    --mono: 'JetBrains Mono', monospace;
    --sans: 'DM Sans', -apple-system, sans-serif;
  }

  body {
    font-family: var(--sans);
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }

  .container {
    max-width: 1400px;
    margin: 0 auto;
    padding: 32px 24px;
  }

  /* ── Header ── */
  .header {
    margin-bottom: 32px;
  }
  .header h1 {
    font-size: 22px;
    font-weight: 700;
    letter-spacing: -0.3px;
    color: var(--accent);
  }
  .header p {
    font-size: 13px;
    color: var(--text-secondary);
    margin-top: 4px;
  }

  /* ── Controls ── */
  .controls {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 20px 24px;
    display: flex;
    gap: 16px;
    align-items: end;
    flex-wrap: wrap;
    margin-bottom: 24px;
  }
  .control-group {
    display: flex;
    flex-direction: column;
    gap: 5px;
  }
  .control-group label {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--text-secondary);
  }
  .control-group input[type="date"],
  .control-group input[type="text"] {
    font-family: var(--sans);
    font-size: 14px;
    padding: 8px 12px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--bg);
    color: var(--text);
    outline: none;
    transition: border-color 0.15s;
  }
  .control-group input:focus {
    border-color: var(--accent);
  }
  .control-group input[type="text"] {
    width: 260px;
  }

  button {
    font-family: var(--sans);
    font-size: 13px;
    font-weight: 600;
    padding: 9px 20px;
    border: none;
    border-radius: 6px;
    cursor: pointer;
    transition: all 0.15s;
  }
  .btn-primary {
    background: var(--accent);
    color: #fff;
  }
  .btn-primary:hover { opacity: 0.9; }
  .btn-primary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .btn-secondary {
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border);
  }
  .btn-secondary:hover { background: var(--border); }

  /* ── Status ── */
  .status {
    font-size: 13px;
    color: var(--text-secondary);
    margin-bottom: 16px;
    min-height: 20px;
    font-family: var(--mono);
  }

  /* ── Table ── */
  .table-wrap {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }
  thead {
    background: var(--accent);
    color: #fff;
  }
  thead th {
    padding: 12px 16px;
    text-align: left;
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    white-space: nowrap;
    position: sticky;
    top: 0;
  }
  tbody tr {
    border-bottom: 1px solid var(--border);
    transition: background 0.1s;
  }
  tbody tr:hover {
    background: var(--accent-light);
  }
  tbody tr:last-child {
    border-bottom: none;
  }
  tbody td {
    padding: 10px 16px;
    vertical-align: top;
  }
  td.date {
    font-family: var(--mono);
    font-size: 12px;
    white-space: nowrap;
    color: var(--text-secondary);
  }
  td.firm {
    font-weight: 600;
  }
  td.phone {
    font-family: var(--mono);
    font-size: 12px;
    white-space: nowrap;
  }
  td a {
    color: var(--accent);
    text-decoration: none;
    font-size: 12px;
  }
  td a:hover { text-decoration: underline; }

  .empty {
    padding: 60px 24px;
    text-align: center;
    color: var(--text-secondary);
    font-size: 14px;
  }

  /* ── Count badge ── */
  .count {
    display: inline-block;
    background: var(--accent-light);
    color: var(--accent);
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 600;
    padding: 3px 10px;
    border-radius: 20px;
    margin-left: 8px;
  }

  @media (max-width: 768px) {
    .controls { flex-direction: column; }
    .control-group input[type="text"] { width: 100%; }
    .table-wrap { overflow-x: auto; }
  }
</style>
</head>
<body>

<div class="container">
  <div class="header">
    <h1>SC TO-T Attorney Lookup <span id="count" class="count" style="display:none"></span></h1>
    <p>Extract counsel contact info from SEC tender offer filings</p>
  </div>

  <div class="controls">
    <div class="control-group">
      <label>Start Date</label>
      <input type="date" id="startDate">
    </div>
    <div class="control-group">
      <label>End Date</label>
      <input type="date" id="endDate">
    </div>
    <div class="control-group">
      <label>Filter</label>
      <input type="text" id="filter" placeholder="Company, attorney, or firm...">
    </div>
    <button class="btn-primary" id="searchBtn" onclick="runSearch()">Search Filings</button>
    <button class="btn-secondary" id="exportBtn" onclick="exportCSV()" style="display:none">Export CSV</button>
  </div>

  <div class="status" id="status"></div>

  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Date</th>
          <th>Companies</th>
          <th>Firm</th>
          <th>Attorneys</th>
          <th>Address</th>
          <th>Phone</th>
          <th>Filing</th>
        </tr>
      </thead>
      <tbody id="tbody">
        <tr><td colspan="7" class="empty">Enter a date range and click Search Filings</td></tr>
      </tbody>
    </table>
  </div>
</div>

<script>
let allData = [];

// Default dates: last 6 months
const today = new Date();
const sixMonthsAgo = new Date(today);
sixMonthsAgo.setMonth(sixMonthsAgo.getMonth() - 6);
document.getElementById('startDate').value = sixMonthsAgo.toISOString().split('T')[0];
document.getElementById('endDate').value = today.toISOString().split('T')[0];

// Filter listener
document.getElementById('filter').addEventListener('input', renderTable);

async function runSearch() {
  const btn = document.getElementById('searchBtn');
  const status = document.getElementById('status');
  const start = document.getElementById('startDate').value;
  const end = document.getElementById('endDate').value;

  btn.disabled = true;
  btn.textContent = 'Searching...';
  status.textContent = 'Querying EDGAR and extracting attorney info — this may take a minute...';

  try {
    const resp = await fetch(`/api/search?start=${start}&end=${end}&limit=200`);
    allData = await resp.json();
    if (allData.error) {
      status.textContent = 'Error: ' + allData.error;
      return;
    }
    status.textContent = `Found ${allData.length} attorney records`;
    document.getElementById('count').textContent = allData.length;
    document.getElementById('count').style.display = allData.length ? 'inline-block' : 'none';
    document.getElementById('exportBtn').style.display = allData.length ? '' : 'none';
    renderTable();
  } catch (e) {
    status.textContent = 'Request failed: ' + e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Search Filings';
  }
}

function renderTable() {
  const tbody = document.getElementById('tbody');
  const filter = document.getElementById('filter').value.toLowerCase();

  let rows = allData;
  if (filter) {
    rows = rows.filter(r =>
      (r.companies || '').toLowerCase().includes(filter) ||
      (r.attorney_names || '').toLowerCase().includes(filter) ||
      (r.firm || '').toLowerCase().includes(filter)
    );
  }

  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty">No results</td></tr>';
    return;
  }

  tbody.innerHTML = rows.map(r => `
    <tr>
      <td class="date">${r.filing_date || ''}</td>
      <td>${esc(r.companies || '')}</td>
      <td class="firm">${esc(r.firm || '')}</td>
      <td>${esc(r.attorney_names || '')}</td>
      <td>${esc(r.address || '')}</td>
      <td class="phone">${esc(r.phone || '')}</td>
      <td>${r.filing_url ? '<a href="' + esc(r.filing_url) + '" target="_blank">View</a>' : ''}</td>
    </tr>
  `).join('');
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function exportCSV() {
  const headers = ['filing_date','companies','firm','attorney_names','address','phone','filing_url'];
  const csv = [
    headers.join(','),
    ...allData.map(r => headers.map(h => '"' + (r[h] || '').replace(/"/g, '""') + '"').join(','))
  ].join('\\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'sc_tot_attorneys.csv';
  a.click();
}
</script>

</body>
</html>
"""

if __name__ == '__main__':
    print("SC TO-T Attorney Lookup")
    print("Open http://localhost:8050")
    uvicorn.run(app, host='0.0.0.0', port=8050)
