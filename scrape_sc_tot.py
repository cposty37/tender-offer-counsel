#!/usr/bin/env python3
"""
SEC SC TO-T Attorney Scraper
==============================
Scrapes SC TO-T (tender offer) filings from EDGAR full-text search,
extracts attorney/counsel info from the "Copies to:" section of each filing,
and outputs a CSV table.

The "Copies to:" block in SC TO-T filings consistently contains:
  - Attorney name(s)
  - Law firm
  - Address
  - Phone number

Usage:
    python3 scrape_sc_tot.py [--start 2026-01-01] [--end 2026-03-18] [--limit 50]
    python3 scrape_sc_tot.py --output table.csv
"""
import os
import re
import csv
import sys
import json
import time
import logging
import argparse
from datetime import date
from html.parser import HTMLParser

import requests

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('sc_tot')

EDGAR_SEARCH = 'https://efts.sec.gov/LATEST/search-index'
EDGAR_BASE = 'https://www.sec.gov/Archives/edgar/data'
USER_AGENT = 'LienHunter/1.0 (Research; charlie@camacpartners.com)'
REQUEST_DELAY = 0.2  # SEC rate limit: 10 requests/sec


class HTMLTextExtractor(HTMLParser):
    """Strip HTML tags, keep text."""
    def __init__(self):
        super().__init__()
        self.text_parts = []

    def handle_data(self, data):
        self.text_parts.append(data)

    def get_text(self):
        return ' '.join(self.text_parts)


def html_to_text(html):
    extractor = HTMLTextExtractor()
    extractor.feed(html)
    return extractor.get_text()


def search_filings(start_date, end_date, limit=200):
    """Search EDGAR for SC TO-T filings in date range."""
    params = {
        'forms': 'SC TO-T',
        'dateRange': 'custom',
        'startdt': start_date,
        'enddt': end_date,
        '_source': 'adsh,form,file_date,display_names,ciks,biz_locations',
        'size': limit,
    }
    headers = {'User-Agent': USER_AGENT}

    resp = requests.get(EDGAR_SEARCH, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    hits = data.get('hits', {}).get('hits', [])
    log.info(f'Found {len(hits)} SC TO-T filings')

    filings = []
    for h in hits:
        s = h['_source']
        doc_id = h['_id']  # e.g. "0001193125-26-113936:d70141dsctota.htm"
        adsh = s['adsh']
        filename = doc_id.split(':')[1] if ':' in doc_id else ''

        filings.append({
            'adsh': adsh,
            'filename': filename,
            'form': s.get('form', ''),
            'file_date': s.get('file_date', ''),
            'companies': s.get('display_names', []),
            'ciks': s.get('ciks', []),
            'locations': s.get('biz_locations', []),
        })

    return filings


def build_filing_url(adsh, cik, filename):
    """Build the URL to access a specific filing document."""
    # ADSH format: 0001193125-26-113936 → path: 000119312526113936
    adsh_path = adsh.replace('-', '')
    return f'{EDGAR_BASE}/{cik}/{adsh_path}/{filename}'


def fetch_filing_html(url):
    """Download a filing's HTML content."""
    headers = {'User-Agent': USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        log.debug(f'  Failed to fetch {url}: {e}')
        return None


def extract_copies_to(html):
    """Extract the 'Copies to:' section from SC TO-T filing HTML.

    Returns list of dicts with attorney_name, firm, address, phone.
    """
    if not html:
        return []

    # Find "Copies to:" section — it appears in bold/italic tags
    # Pattern: everything after "Copies to:" until a divider or next section
    copies_pattern = re.compile(
        r'Copies\s+to:.*?(?=<(?:center|TABLE|DIV\s+STYLE="line-height)|$)',
        re.IGNORECASE | re.DOTALL
    )
    match = copies_pattern.search(html)
    if not match:
        return []

    block_html = match.group(0)

    # Extract text from HTML tags in the block
    # Each <P> tag typically contains one line of info
    p_pattern = re.compile(r'<P[^>]*>(.*?)</P>', re.IGNORECASE | re.DOTALL)
    lines = []
    for p in p_pattern.finditer(block_html):
        text = html_to_text(p.group(1)).strip()
        # Clean up whitespace
        text = re.sub(r'\s+', ' ', text)
        if text and text != '\xa0' and text != '&nbsp;':
            lines.append(text)

    if not lines:
        return []

    # Parse the lines into attorney records
    # Typical pattern:
    #   Line 1: Attorney Name(s)
    #   Line 2: (possibly more names)
    #   Line N: Law Firm Name (contains LLP, LLC, P.C., etc.)
    #   Line N+1: Street Address
    #   Line N+2: City, State ZIP
    #   Line N+3: Phone (contains parenthesized area code)
    attorneys = []
    current = {'names': [], 'firm': '', 'address': '', 'phone': ''}

    firm_patterns = re.compile(
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

    phone_pattern = re.compile(r'\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}')
    address_pattern = re.compile(r'\d+\s+\w+.*(?:Street|St\.|Avenue|Ave\.|Boulevard|Blvd|Road|Rd|Plaza|Drive|Dr|Way|Place|Pl|Suite|Floor)', re.IGNORECASE)
    city_state_pattern = re.compile(r'[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*,?\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*,?\s+\d{5}', re.IGNORECASE)

    for line in lines:
        # Skip the "Copies to:" header itself
        if re.match(r'^\s*Copies\s+to:\s*$', line, re.IGNORECASE):
            continue

        if phone_pattern.search(line):
            current['phone'] = phone_pattern.search(line).group(0)
            # Phone usually ends a block — save and start new
            if current['names'] or current['firm']:
                attorneys.append(dict(current))
                current = {'names': [], 'firm': '', 'address': '', 'phone': ''}
        elif firm_patterns.search(line):
            current['firm'] = line.strip()
        elif address_pattern.search(line):
            current['address'] = line.strip() if not current['address'] else current['address'] + ', ' + line.strip()
        elif city_state_pattern.search(line):
            current['address'] = current['address'] + ', ' + line.strip() if current['address'] else line.strip()
        else:
            # Likely a name
            current['names'].append(line.strip())

    # Don't forget the last block if it wasn't terminated by a phone
    if current['names'] or current['firm']:
        attorneys.append(current)

    # Flatten into output format
    results = []
    for a in attorneys:
        names = [n for n in a['names'] if n and len(n) > 2]
        results.append({
            'attorney_names': '; '.join(names),
            'firm': a['firm'],
            'address': a['address'],
            'phone': a['phone'],
        })

    return results


def run(start_date, end_date, limit, output_file):
    log.info(f'Searching SC TO-T filings from {start_date} to {end_date}')

    filings = search_filings(start_date, end_date, limit)

    all_rows = []
    for i, f in enumerate(filings):
        companies = ' / '.join(f['companies'])
        log.info(f'[{i+1}/{len(filings)}] {f["file_date"]} | {f["form"]} | {companies[:60]}')

        # Build URL — try first CIK
        if not f['ciks'] or not f['filename']:
            log.debug('  No CIK or filename — skipping')
            continue

        url = build_filing_url(f['adsh'], f['ciks'][0], f['filename'])
        html = fetch_filing_html(url)

        if not html:
            # Try second CIK
            if len(f['ciks']) > 1:
                url = build_filing_url(f['adsh'], f['ciks'][1], f['filename'])
                html = fetch_filing_html(url)

        attorneys = extract_copies_to(html) if html else []

        if attorneys:
            for a in attorneys:
                all_rows.append({
                    'filing_date': f['file_date'],
                    'form': f['form'],
                    'companies': companies,
                    'adsh': f['adsh'],
                    'attorney_names': a['attorney_names'],
                    'firm': a['firm'],
                    'address': a['address'],
                    'phone': a['phone'],
                    'filing_url': url,
                })
            names = [a['firm'] or a['attorney_names'] for a in attorneys]
            log.info(f'  Found: {" | ".join(names)}')
        else:
            log.info(f'  No attorney info found')

        time.sleep(REQUEST_DELAY)

    # Output
    if not all_rows:
        log.warning('No attorney data extracted')
        return

    # Write CSV
    fieldnames = ['filing_date', 'form', 'companies', 'attorney_names', 'firm', 'address', 'phone', 'adsh', 'filing_url']
    with open(output_file, 'w', newline='') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)

    log.info(f'\nDONE: {len(all_rows)} attorney records from {len(filings)} filings')
    log.info(f'Output: {output_file}')

    # Also print a nice table to console
    print(f'\n{"Date":<12} {"Companies":<45} {"Firm":<40} {"Attorney":<30} {"Phone":<16}')
    print('-' * 145)
    for r in all_rows:
        print(f'{r["filing_date"]:<12} {r["companies"][:44]:<45} {r["firm"][:39]:<40} {r["attorney_names"][:29]:<30} {r["phone"]:<16}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='SEC SC TO-T Attorney Scraper')
    parser.add_argument('--start', default='2026-01-01', help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end', default=date.today().isoformat(), help='End date (YYYY-MM-DD)')
    parser.add_argument('--limit', type=int, default=200, help='Max filings to process')
    parser.add_argument('--output', default='sc_tot_attorneys.csv', help='Output CSV file')
    args = parser.parse_args()
    run(args.start, args.end, args.limit, args.output)
