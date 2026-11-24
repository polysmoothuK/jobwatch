"""Builds data/sources_index.json: the list of everything JobWatch watches (for the dashboard)."""
import collections, glob, json, os
h = json.load(open('data/health.json'))['sources'] if os.path.exists('data/health.json') else {}
API = {'greenhouse','lever','lever_eu','smartrecruiters','recruitee','workable','bamboohr','teamtailor','workday','jobvite','personio','ashby'}
REG = {'uk_ie': 'Royaume-Uni & Irlande', 'europe': 'Europe', 'americas': 'Amériques', 'apac': 'Asie-Pacifique'}
rows = []
for f in sorted(glob.glob('sources/*.json')):
    key = os.path.splitext(os.path.basename(f))[0]
    if key not in REG:
        continue
    for s in json.load(open(f, encoding='utf-8')):
        rows.append({"name": s['name'], "track": s.get('track', 'vfx'), "region": REG[key], "country": s.get('country', ''),
                     "method": "Offres lues une par une" if s.get('ats') in API else "Page surveillée (changements)",
                     "url": s.get('careers_url', ''), "ok": h.get(s['name'], {}).get('ok', True)})
for a in json.load(open('sources/aggregators.json', encoding='utf-8')):
    if a.get('automatable') or a.get('name') == 'VFXwire':
        rows.append({"name": a['name'], "track": "vfx", "region": "Sites d'offres", "country": "Monde",
                     "method": "Site d'offres", "url": a.get('url', ''), "ok": True})
json.dump(rows, open('data/sources_index.json', 'w', encoding='utf-8'), ensure_ascii=False)
print(len(rows), dict(collections.Counter(r['region'] for r in rows)), dict(collections.Counter(r['method'] for r in rows)))
