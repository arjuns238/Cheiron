/* Cheiron demo frontend.
 *
 * This file is a consumer of the documented response envelope and deliberately nothing
 * more. It learns which key holds the dimension from `visualization.encoding.x.field`
 * rather than hardcoding "phases" or "countries", picks a renderer from
 * `visualization.type`, and shows provenance from `citations`. If it can render a chart
 * without special-casing a query, so can a real frontend — which is the claim `/schema`
 * makes on the backend's behalf.
 *
 * Three renderers: Chart.js for the cartesian and pie families, d3-geo for the
 * choropleth, and a small hand-written force layout for the network (the graphs are
 * top-N bounded — ten nodes in the captured example — so a full layout library would be
 * more dependency than the job needs).
 */

const $ = (id) => document.getElementById(id);

/* `hidden` is a property of HTMLElement, and SVGElement does not implement it: assigning
   `svg.hidden = false` sets a meaningless expando and leaves the attribute in place. The
   attribute works for both, so toggle that. */
const show = (id) => $(id).removeAttribute('hidden');
const hide = (id) => $(id).setAttribute('hidden', '');

const PALETTE = [
  '#2f6fb0', '#c96a2b', '#4e9a68', '#a3486e', '#7a6bb5',
  '#b5893f', '#5aa0a8', '#8f5f4a', '#8a8f96', '#3d6f8e',
];

let chart = null;          // the live Chart.js instance, destroyed between renders
let current = null;        // the response being displayed
let worldTopology = null;  // lazily fetched, only when a map is first needed

// ------------------------------------------------------------------------------------
// Country names
//
// The registry and the world atlas disagree, and not marginally: a plain name join
// leaves 71 of the registry's 226 country names unmatched, including "United States"
// (the atlas calls it "United States of America"), which alone is 194,226 study-country
// rows. Aliasing takes the unmatched share from 29.8% of rows to 1.25%.
//
// What remains unmatched is city-states, islands and territories that have no polygon at
// this resolution at all — Hong Kong, Singapore, Monaco, Malta, Guam. No alias can fix
// those, so they are listed under the map instead of vanishing from it. A country
// silently missing from a choropleth reads as "no trials here", which is a different and
// false claim.
// ------------------------------------------------------------------------------------

const COUNTRY_ALIASES = {
  'United States': 'United States of America',
  'Turkey (Türkiye)': 'Turkey',
  'Bosnia and Herzegovina': 'Bosnia and Herz.',
  'Dominican Republic': 'Dominican Rep.',
  'North Macedonia': 'Macedonia',
  'Macedonia, The Former Yugoslav Republic of': 'Macedonia',
  'Democratic Republic of the Congo': 'Dem. Rep. Congo',
  'Republic of the Congo': 'Congo',
  'The Gambia': 'Gambia',
  'Palestinian Territories': 'Palestine',
  'Burma': 'Myanmar',
  'The Bahamas': 'Bahamas',
  'Central African Republic': 'Central African Rep.',
  'South Sudan': 'S. Sudan',
  'Solomon Islands': 'Solomon Is.',
  'Equatorial Guinea': 'Eq. Guinea',
  'Serbia and Montenegro': 'Serbia',
  'Former Serbia and Montenegro': 'Serbia',
  'Federal Republic of Yugoslavia': 'Serbia',
  'Former Yugoslavia': 'Serbia',
  'Czech Republic': 'Czechia',
  'Slovak Republic': 'Slovakia',
  'Russian Federation': 'Russia',
  'Republic of Korea': 'South Korea',
  'Viet Nam': 'Vietnam',
  "Lao People's Democratic Republic": 'Laos',
  'Brunei Darussalam': 'Brunei',
  'Syrian Arab Republic': 'Syria',
  'Iran, Islamic Republic of': 'Iran',
  'Republic of Moldova': 'Moldova',
  'Libyan Arab Jamahiriya': 'Libya',
  'Cape Verde': 'Cabo Verde',
};

/** Curly and straight apostrophes differ between the two sources (Côte d'Ivoire). */
const normalise = (name) => (COUNTRY_ALIASES[name] || name).replace(/’/g, "'");

// ------------------------------------------------------------------------------------
// Wiring
// ------------------------------------------------------------------------------------

async function boot() {
  $('query-form').addEventListener('submit', onAsk);
  try {
    const captured = await (await fetch('/examples')).json();
    renderExampleButtons(captured);
  } catch {
    /* No captured runs is a normal state — the query box still works. */
  }
}

function renderExampleButtons(captured) {
  const host = $('examples');
  if (!captured.length) return;
  host.append(Object.assign(document.createElement('span'), {
    className: 'hint', textContent: 'Example prompts:', style: 'margin:0 .2rem 0 0;align-self:center',
  }));
  for (const entry of captured) {
    const slug = entry.file.replace(/\.json$/, '');
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = entry.title;
    button.title = entry.query;
    button.setAttribute('aria-pressed', 'false');
    button.addEventListener('click', () => loadExample(slug, button));
    host.append(button);
  }
}

function selectExample(button) {
  for (const b of $('examples').querySelectorAll('button')) {
    b.setAttribute('aria-pressed', String(b === button));
  }
}

async function loadExample(slug, button) {
  selectExample(button);
  say('Loading the captured run…');
  try {
    const response = await fetch(`/examples/${slug}`);
    if (!response.ok) throw new Error(await response.text());
    const body = await response.json();
    $('query').value = body.meta?.interpretation || '';
    render(body);
  } catch (error) {
    fail(`Could not load the example: ${error.message}`);
  }
}

async function onAsk(event) {
  event.preventDefault();
  const query = $('query').value.trim();
  if (!query) return;
  selectExample(null);

  $('run').disabled = true;
  say('Routing, planning, retrieving and aggregating. A live question takes 10–60 seconds.');
  try {
    const response = await fetch('/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query }),
    });
    const body = await response.json();
    if (!response.ok) {
      // The backend's failure modes are part of its design, so show what it actually
      // said rather than a generic apology. An invariant failure in particular is a
      // deliberate refusal to return a chart, not a crash.
      fail(body.message || body.detail || `Request failed (${response.status}).`, body);
      return;
    }
    render(body);
  } catch (error) {
    fail(`Could not reach the service: ${error.message}`);
  } finally {
    $('run').disabled = false;
  }
}

function say(message) {
  const status = $('status');
  show('status');
  status.className = 'callout';
  status.textContent = message;
}

function fail(message, detail) {
  const status = $('status');
  show('status');
  status.className = 'callout warn';
  status.innerHTML = '';
  status.append(Object.assign(document.createElement('strong'), { textContent: message }));
  if (detail?.detail) {
    status.append(Object.assign(document.createElement('pre'), {
      textContent: typeof detail.detail === 'string'
        ? detail.detail : JSON.stringify(detail.detail, null, 2),
    }));
  }
  hide('result');
}

// ------------------------------------------------------------------------------------
// Rendering
// ------------------------------------------------------------------------------------

function render(body) {
  current = body;
  hide('status');
  show('result');

  const viz = body.visualization;
  $('viz-title').textContent = viz?.title || '';
  $('viz-subtitle').textContent = viz?.subtitle || '';
  $('answer').textContent = body.answer || '';

  for (const id of ['chart', 'map', 'network', 'kpi']) hide(id);
  if (chart) { chart.destroy(); chart = null; }
  hide('unmapped');
  hide('pick-hint');

  // `unsupported` and `conversational` are answers, not failures. The backend states an
  // obstruction and suggests what it can do instead; that prose is the whole response,
  // so show it and draw nothing.
  const hasData = viz && body.response_type !== 'unsupported' && body.response_type !== 'conversational';
  if (hasData) drawChart(viz);

  renderWarnings(body.meta?.warnings || []);
  renderCitations(null);
  renderMeta(body);
  $('result').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function drawChart(viz) {
  switch (viz.type) {
    case 'network': return drawNetwork(viz);
    case 'choropleth': return drawMap(viz);
    case 'kpi': return drawKpi(viz);
    default: return drawChartJs(viz);
  }
}

/** The dimension key is whatever the encoding says it is — never assumed. */
const dimensionKey = (viz) => viz.encoding?.x?.field || 'label';
const seriesKey = (viz) => viz.encoding?.series?.field || null;

function drawChartJs(viz) {
  const canvas = $('chart');
  show('chart');
  show('pick-hint');

  const dim = dimensionKey(viz);
  const series = seriesKey(viz);
  const rows = viz.data;
  const labels = [...new Set(rows.map((r) => r[dim]))];

  // A scatter is not a bucketed chart: each datum is one trial, and both coordinates are
  // numbers. Chart.js needs {x, y} pairs — feeding it labels plus values would plot the
  // row index against y and silently draw the wrong relationship.
  if (viz.type === 'scatter') return drawScatter(viz, canvas, dim);

  let datasets;
  if (series) {
    const names = [...new Set(rows.map((r) => r[series]))];
    datasets = names.map((name, i) => ({
      label: name,
      data: labels.map((l) => {
        const hit = rows.find((r) => r[dim] === l && r[series] === name);
        return hit ? hit.value : 0;
      }),
      backgroundColor: PALETTE[i % PALETTE.length],
      borderColor: PALETTE[i % PALETTE.length],
      fill: viz.type === 'stacked_area',
    }));
  } else {
    const pieLike = viz.type === 'pie';
    datasets = [{
      label: viz.encoding?.y?.label || 'Value',
      data: rows.map((r) => r.value),
      backgroundColor: pieLike ? labels.map((_, i) => PALETTE[i % PALETTE.length]) : PALETTE[0],
      borderColor: PALETTE[0],
      fill: viz.type === 'stacked_area',
    }];
  }

  const stacked = viz.type === 'stacked_bar' || viz.type === 'stacked_area';
  const kind = { line: 'line', stacked_area: 'line', pie: 'pie', scatter: 'scatter' }[viz.type] || 'bar';

  chart = new Chart(canvas, {
    type: kind,
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      onClick: (_event, hit) => {
        if (!hit.length) return;
        const { datasetIndex, index } = hit[0];
        const label = labels[index];
        const name = series ? datasets[datasetIndex].label : null;
        const row = rows.find((r) => r[dim] === label && (!series || r[series] === name));
        if (row) showSourcesFor(row.citations, row.nct_id_total, `${label}${name ? ` · ${name}` : ''}`);
      },
      plugins: {
        legend: { display: Boolean(series) || viz.type === 'pie' },
        tooltip: {
          callbacks: {
            // `nct_id_total` is the real contributor count; `nct_ids` is a sample of it.
            // Showing the sample length as though it were the total would understate
            // every bar by a factor of a hundred or more.
            afterLabel: (item) => {
              const name = series ? item.dataset.label : null;
              const row = rows.find((r) => r[dim] === item.label && (!series || r[series] === name));
              return row ? `${row.nct_id_total.toLocaleString()} trial(s)` : '';
            },
          },
        },
      },
      scales: kind === 'pie' ? {} : {
        x: { stacked, title: { display: true, text: viz.encoding?.x?.label || '' } },
        y: {
          stacked,
          beginAtZero: viz.config?.y_starts_at_zero !== false,
          title: { display: true, text: viz.encoding?.y?.label || '' },
        },
      },
    },
  });
}

function drawScatter(viz, canvas, dim) {
  const rows = viz.data;
  chart = new Chart(canvas, {
    type: 'scatter',
    data: {
      datasets: [{
        label: viz.encoding?.y?.label || 'Value',
        data: rows.map((r) => ({ x: r[dim], y: r.value, row: r })),
        backgroundColor: 'rgba(47,111,176,.45)',
        pointRadius: 3,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      onClick: (_e, hit) => {
        if (!hit.length) return;
        const row = rows[hit[0].index];
        showSourcesFor(row.citations, row.nct_id_total, row.nct_id || 'this trial');
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (item) => {
              const row = item.raw.row;
              return `${row.nct_id || ''}  ${viz.encoding?.x?.label}=${item.parsed.x}, `
                + `${viz.encoding?.y?.label}=${item.parsed.y}`;
            },
          },
        },
      },
      scales: {
        x: { type: 'linear', title: { display: true, text: viz.encoding?.x?.label || '' } },
        y: {
          beginAtZero: viz.config?.y_starts_at_zero !== false,
          title: { display: true, text: viz.encoding?.y?.label || '' },
        },
      },
    },
  });
}

function drawKpi(viz) {
  const host = $('kpi');
  show('kpi');
  const row = viz.data?.[0];
  host.textContent = row ? Number(row.value).toLocaleString() : '—';
}

// ------------------------------------------------------------------------------------
// Choropleth
// ------------------------------------------------------------------------------------

async function drawMap(viz) {
  const svg = $('map');
  show('map');
  svg.innerHTML = '';

  if (!worldTopology) {
    worldTopology = await (await fetch('/static/vendor/countries-110m.json')).json();
  }
  const all = topojson.feature(worldTopology, worldTopology.objects.countries);
  // Antarctica has no trials and occupies a fifth of the frame. Dropping the polygon is
  // safe rather than a silent loss: anything with data and no polygon is caught below and
  // listed, so if it ever did appear it would be reported instead of disappearing.
  const countries = {
    ...all,
    features: all.features.filter((f) => f.properties.name !== 'Antarctica'),
  };
  const projection = d3.geoNaturalEarth1().fitSize([960, 500], countries);
  const path = d3.geoPath(projection);

  const dim = dimensionKey(viz);
  const byName = new Map();
  for (const row of viz.data) {
    // "Other" is a residue of collapsed countries, not a place. It has no polygon and is
    // reported separately rather than being dropped without comment.
    if (row[dim] !== 'Other') byName.set(normalise(row[dim]), row);
  }

  const values = viz.data.filter((r) => r[dim] !== 'Other').map((r) => r.value);
  const max = Math.max(...values, 1);
  const shade = (v) => `rgb(${Math.round(232 - 180 * (v / max))},${Math.round(238 - 130 * (v / max))},${Math.round(244 - 60 * (v / max))})`;

  const drawn = new Set();
  for (const feature of countries.features) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    node.setAttribute('d', path(feature) || '');
    const row = byName.get(feature.properties.name);
    if (row) {
      drawn.add(feature.properties.name);
      node.setAttribute('class', 'country datum');
      node.setAttribute('fill', shade(row.value));
      node.addEventListener('click', () =>
        showSourcesFor(row.citations, row.nct_id_total, row[dim]));
      const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
      title.textContent = `${row[dim]}: ${row.value.toLocaleString()}`;
      node.append(title);
    } else {
      node.setAttribute('class', 'country');
    }
    svg.append(node);
  }

  const unmapped = viz.data.filter((r) => r[dim] !== 'Other' && !drawn.has(normalise(r[dim])));
  const other = viz.data.find((r) => r[dim] === 'Other');
  reportUnmapped(unmapped, other, dim);
  show('pick-hint');
}

function reportUnmapped(unmapped, other, dim) {
  const host = $('unmapped');
  if (!unmapped.length && !other) return;
  show('unmapped');
  host.innerHTML = '<h4>Not drawn on the map</h4>';
  const list = document.createElement('ul');
  for (const row of unmapped) {
    list.append(Object.assign(document.createElement('li'), {
      textContent: `${row[dim]} — ${row.value.toLocaleString()}. No polygon exists at this `
        + `map resolution (city-states, islands and territories); the value is in the data, `
        + `it simply cannot be shaded.`,
    }));
  }
  if (other) {
    list.append(Object.assign(document.createElement('li'), {
      textContent: `Other — ${other.value.toLocaleString()} distinct trials across the `
        + `countries beyond the top N. A residue is not a place, so it has no polygon.`,
    }));
  }
  host.append(list);
}

// ------------------------------------------------------------------------------------
// Network
//
// A small spring/repulsion simulation. Node area is proportional to weight; edge width to
// co-occurrence weight. `strength` is on the edge too, but it is a derived, normalised
// figure and is shown in the tooltip rather than drawn — see the backend's warning about
// reading it alone.
// ------------------------------------------------------------------------------------

function drawNetwork(viz) {
  const svg = $('network');
  show('network');
  svg.innerHTML = '';
  show('pick-hint');

  const W = 960, H = 560;
  const nodes = viz.data.nodes.map((n, i) => ({
    ...n,
    x: W / 2 + 200 * Math.cos((2 * Math.PI * i) / viz.data.nodes.length),
    y: H / 2 + 200 * Math.sin((2 * Math.PI * i) / viz.data.nodes.length),
    vx: 0, vy: 0,
  }));
  const index = new Map(nodes.map((n) => [n.id, n]));
  const edges = viz.data.edges
    .map((e) => ({ ...e, a: index.get(e.source), b: index.get(e.target) }))
    .filter((e) => e.a && e.b);

  for (let step = 0; step < 400; step += 1) {
    for (const a of nodes) {
      for (const b of nodes) {
        if (a === b) continue;
        const dx = a.x - b.x, dy = a.y - b.y;
        const d2 = Math.max(dx * dx + dy * dy, 100);
        const force = 90000 / d2;
        a.vx += (dx / Math.sqrt(d2)) * force * 0.01;
        a.vy += (dy / Math.sqrt(d2)) * force * 0.01;
      }
    }
    for (const e of edges) {
      const dx = e.b.x - e.a.x, dy = e.b.y - e.a.y;
      const d = Math.max(Math.hypot(dx, dy), 1);
      const pull = (d - 190) * 0.005;
      e.a.vx += (dx / d) * pull; e.a.vy += (dy / d) * pull;
      e.b.vx -= (dx / d) * pull; e.b.vy -= (dy / d) * pull;
    }
    for (const n of nodes) {
      n.vx += (W / 2 - n.x) * 0.002;
      n.vy += (H / 2 - n.y) * 0.002;
      n.x += (n.vx *= 0.82);
      n.y += (n.vy *= 0.82);
      n.x = Math.min(W - 70, Math.max(70, n.x));
      n.y = Math.min(H - 40, Math.max(40, n.y));
    }
  }

  const svgNS = 'http://www.w3.org/2000/svg';
  const maxEdge = Math.max(...edges.map((e) => e.weight), 1);
  const maxNode = Math.max(...nodes.map((n) => n.weight), 1);

  for (const e of edges) {
    const line = document.createElementNS(svgNS, 'line');
    line.setAttribute('x1', e.a.x); line.setAttribute('y1', e.a.y);
    line.setAttribute('x2', e.b.x); line.setAttribute('y2', e.b.y);
    line.setAttribute('stroke-width', 1 + 7 * (e.weight / maxEdge));
    line.addEventListener('click', () => showSourcesFor(
      e.citations, e.nct_id_total, `${e.a.label} + ${e.b.label}`));
    const title = document.createElementNS(svgNS, 'title');
    title.textContent = `${e.a.label} + ${e.b.label}\n${e.weight.toLocaleString()} shared arm groups`
      + `\nassociation strength ${e.strength} (derived)`;
    line.append(title);
    svg.append(line);
  }

  for (const n of nodes) {
    const circle = document.createElementNS(svgNS, 'circle');
    circle.setAttribute('cx', n.x); circle.setAttribute('cy', n.y);
    circle.setAttribute('r', 6 + 22 * Math.sqrt(n.weight / maxNode));
    const title = document.createElementNS(svgNS, 'title');
    title.textContent = `${n.label}: ${n.weight.toLocaleString()} trials`;
    circle.append(title);
    svg.append(circle);

    const text = document.createElementNS(svgNS, 'text');
    text.setAttribute('x', n.x);
    text.setAttribute('y', n.y - 10 - 22 * Math.sqrt(n.weight / maxNode));
    text.setAttribute('text-anchor', 'middle');
    text.textContent = n.label;
    svg.append(text);
  }
}

// ------------------------------------------------------------------------------------
// Provenance
// ------------------------------------------------------------------------------------

function renderWarnings(warnings) {
  const host = $('warnings');
  if (!warnings.length) { hide('warnings'); return; }
  show('warnings');
  host.innerHTML = '<h4>What this chart does not say</h4>';
  const list = document.createElement('ul');
  for (const w of warnings) {
    list.append(Object.assign(document.createElement('li'), { textContent: w }));
  }
  host.append(list);
}

function showSourcesFor(citations, total, label) {
  renderCitations({ citations, total, label });
  $('provenance').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

/** Every citation on the page, for the default view before anything is selected. */
function allCitations() {
  const data = current?.visualization?.data;
  if (!data) return [];
  const units = data.edges || data;
  return units.flatMap((u) => u.citations || []);
}

function renderCitations(selection) {
  const host = $('provenance');
  // Citations hang off the datum, not off a response-level map keyed by NCT ID. That map
  // could only hold one excerpt per trial, so on a multi-valued dimension every datum but
  // the first read a citation stating a different datum's value — clicking Canada showed
  // `"country":"United States"` as its evidence.
  const citations = selection ? selection.citations : allCitations();
  if (!citations.length) { hide('provenance'); return; }
  show('provenance');

  $('provenance-title').textContent = selection ? `Sources for ${selection.label}` : 'Sources';
  $('provenance-note').textContent = selection
    ? `${selection.total.toLocaleString()} trial(s) contributed this value; `
      + `${citations.length} excerpt(s) are cited. Each is a literal span of the fetched `
      + `record, verified at the byte offsets shown before it was emitted.`
    : `Every excerpt below is a literal substring of the record as fetched, re-verified at `
      + `its offsets. Any citation that failed that check was dropped rather than shown.`;

  const body = $('citations').querySelector('tbody');
  body.innerHTML = '';
  const header = document.createElement('tr');
  for (const label of ['Trial', 'Supports', 'Field', 'Value', 'Excerpt (verified)']) {
    header.append(Object.assign(document.createElement('th'), { textContent: label }));
  }
  body.append(header);

  for (const citation of citations) {
    const row = document.createElement('tr');

    const nct = document.createElement('td');
    nct.className = 'nct';
    const link = document.createElement('a');
    link.href = citation.url;
    link.target = '_blank';
    link.rel = 'noopener';
    link.textContent = citation.nct_id;
    link.title = citation.brief_title || '';
    nct.append(link);
    row.append(nct);

    // A grouped datum has two coordinates and one excerpt rarely states both, so the
    // bucket and the series are evidenced separately and labelled as such.
    row.append(Object.assign(document.createElement('td'), {
      textContent: citation.supports === 'series' ? 'series' : 'value',
    }));
    row.append(Object.assign(document.createElement('td'), { textContent: citation.field_path }));
    row.append(Object.assign(document.createElement('td'), { textContent: String(citation.field_value) }));
    const excerpt = document.createElement('td');
    excerpt.className = 'excerpt';
    excerpt.textContent = citation.excerpt;
    excerpt.title = `bytes ${citation.offset?.[0]}\u2013${citation.offset?.[1]} of the serialized record`;
    row.append(excerpt);
    body.append(row);
  }
}

function renderMeta(body) {
  const host = $('meta-body');
  host.innerHTML = '';
  const counts = body.meta?.record_counts;

  const block = (heading, content) => {
    host.append(Object.assign(document.createElement('h4'), { textContent: heading }));
    host.append(Object.assign(document.createElement('pre'), { textContent: content }));
  };

  if (counts) {
    const excluded = Object.entries(counts.excluded_by_reason || {})
      .map(([reason, n]) => `    ${n.toLocaleString()} — ${reason}`).join('\n');
    block('Records', [
      `matched    ${counts.matched?.toLocaleString() ?? '—'}`,
      `retrieved  ${counts.retrieved?.toLocaleString() ?? '—'}`,
      `used       ${counts.used?.toLocaleString() ?? '—'}`,
      excluded ? `excluded:\n${excluded}` : 'excluded:  none',
      '',
      'used + excluded == retrieved is asserted in production; a mismatch returns no chart.',
    ].join('\n'));
  }
  if (body.meta?.plan) block('Plan', JSON.stringify(body.meta.plan, null, 2));
  if (body.meta?.api_requests?.length) {
    block('API requests issued', body.meta.api_requests.join('\n\n'));
  }
  if (body.meta?.planning_trace) {
    block('Planning trace', JSON.stringify(body.meta.planning_trace, null, 2));
  }
}

boot();
