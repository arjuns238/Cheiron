/* Trace — the demo frontend. React (UMD) + htm, no build step.
 *
 * This file is a consumer of the documented response envelope and deliberately nothing
 * more. It learns which key holds the dimension from `visualization.encoding.x.field`
 * rather than hardcoding "phases" or "countries", picks a renderer from
 * `visualization.type`, and shows provenance from the citations hanging off each datum.
 * If it can render a chart without special-casing a query, so can a real frontend — which
 * is the claim `/schema` makes on the backend's behalf.
 *
 * htm stands in for JSX so the page stays buildless: `uv sync` and uvicorn remain the
 * whole setup, with no Node toolchain in a Python repository.
 *
 * Three renderers, each an effect that draws into a ref: Chart.js for the cartesian and
 * pie families, d3-geo for the choropleth, and a small hand-written force layout for the
 * network (the graphs are top-N bounded — ten nodes in the captured example — so a full
 * layout library would be more dependency than the job needs).
 */

const html = htm.bind(React.createElement);
const { useState, useEffect, useRef, useCallback, useMemo } = React;

const PALETTE = [
  '#2f6fb0', '#c96a2b', '#4e9a68', '#a3486e', '#7a6bb5',
  '#b5893f', '#5aa0a8', '#8f5f4a', '#8a8f96', '#3d6f8e',
];

const num = (n) => (typeof n === 'number' ? n.toLocaleString() : '—');

/** The dimension key is whatever the encoding says it is — never assumed. */
const dimensionKey = (viz) => viz.encoding?.x?.field || 'label';
const seriesKey = (viz) => viz.encoding?.series?.field || null;

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

/** Fetched once, on the first request that actually needs a map. */
let worldTopology = null;

// ------------------------------------------------------------------------------------
// The pipeline stages, for the loading state
//
// The service does not stream progress, so these advance on measured-typical durations
// rather than on real events, and the caption says so. They are worth showing anyway:
// the sequence *is* the architecture, and a 10–60 second wait with a spinner tells the
// viewer nothing about what is being spent on their behalf.
// ------------------------------------------------------------------------------------

const STAGES = [
  ['Router', 'Deciding whether this is a question about trials at all', 2200],
  ['Planner', 'Choosing fields and filters, probing the registry for counts', 9000],
  ['Reviewer', 'Judging the plan against the question that was asked', 5000],
  ['Retrieval', 'Paginating ClinicalTrials.gov for the matching records', 12000],
  ['Aggregation', 'Folding records into buckets; citations are minted here', 4000],
  ['Chart', 'Rules narrow the legal charts, then the selector picks one', 3000],
];

// ------------------------------------------------------------------------------------
// Renderers
// ------------------------------------------------------------------------------------

function useChartJs(viz, onPick) {
  const canvas = useRef(null);

  useEffect(() => {
    if (!canvas.current) return undefined;
    const dim = dimensionKey(viz);
    const series = seriesKey(viz);
    const rows = viz.data;

    // A scatter is not a bucketed chart: each datum is one trial, and both coordinates
    // are numbers. Chart.js needs {x, y} pairs — feeding it labels plus values would plot
    // the row index against y and silently draw the wrong relationship.
    const instance = viz.type === 'scatter'
      ? scatterChart(canvas.current, viz, dim, onPick)
      : bucketChart(canvas.current, viz, dim, series, rows, onPick);

    return () => instance.destroy();
  }, [viz, onPick]);

  return canvas;
}

function bucketChart(canvas, viz, dim, series, rows, onPick) {
  const labels = [...new Set(rows.map((r) => r[dim]))];

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

  return new Chart(canvas, {
    type: kind,
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 550 },
      onClick: (_event, hit) => {
        if (!hit.length) return;
        const { datasetIndex, index } = hit[0];
        const label = labels[index];
        const name = series ? datasets[datasetIndex].label : null;
        const row = rows.find((r) => r[dim] === label && (!series || r[series] === name));
        if (row) onPick({
          label: `${label}${name ? ` · ${name}` : ''}`,
          citations: row.citations || [],
          total: row.nct_id_total,
        });
      },
      plugins: {
        legend: { display: Boolean(series) || viz.type === 'pie', labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true } },
        tooltip: {
          callbacks: {
            // `nct_id_total` is the real contributor count; `nct_ids` is a sample of it.
            // Showing the sample length as though it were the total would understate
            // every bar by a factor of a hundred or more.
            afterLabel: (item) => {
              const name = series ? item.dataset.label : null;
              const row = rows.find((r) => r[dim] === item.label && (!series || r[series] === name));
              return row ? `${num(row.nct_id_total)} trial(s) — click to see sources` : '';
            },
          },
        },
      },
      scales: kind === 'pie' ? {} : {
        x: { stacked, grid: { display: false }, title: { display: true, text: viz.encoding?.x?.label || '' } },
        y: {
          stacked,
          beginAtZero: viz.config?.y_starts_at_zero !== false,
          grid: { color: 'rgba(16,20,24,.07)' },
          title: { display: true, text: viz.encoding?.y?.label || '' },
        },
      },
    },
  });
}

function scatterChart(canvas, viz, dim, onPick) {
  const rows = viz.data;
  return new Chart(canvas, {
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
      animation: { duration: 550 },
      onClick: (_e, hit) => {
        if (!hit.length) return;
        const row = rows[hit[0].index];
        onPick({
          label: row.nct_id || 'this trial',
          citations: row.citations || [],
          total: row.nct_id_total,
        });
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
        // The scale comes from the response, not from inspecting the data here. Both
        // measures are heavy-tailed — 99.6% of points sit below 1% of the maximum — so a
        // linear axis is correct and shows nothing. A log axis cannot plot 0, and 0 is a
        // real value for site_count, so the floor is nudged rather than the point dropped.
        x: {
          type: viz.config?.x_scale === 'log' ? 'logarithmic' : 'linear',
          min: viz.config?.x_scale === 'log' ? 0.5 : undefined,
          grid: { display: false },
          title: { display: true, text: viz.encoding?.x?.label || '' },
        },
        y: {
          type: viz.config?.y_scale === 'log' ? 'logarithmic' : 'linear',
          min: viz.config?.y_scale === 'log' ? 0.5 : undefined,
          beginAtZero: viz.config?.y_scale !== 'log' && viz.config?.y_starts_at_zero !== false,
          grid: { color: 'rgba(16,20,24,.07)' },
          title: { display: true, text: viz.encoding?.y?.label || '' },
        },
      },
    },
  });
}

function ChartSurface({ viz, onPick }) {
  const canvas = useChartJs(viz, onPick);
  return html`<div className="chart-frame"><canvas ref=${canvas}></canvas></div>`;
}

// --- choropleth ---------------------------------------------------------------------

function Choropleth({ viz, onPick }) {
  const svg = useRef(null);
  const [unmapped, setUnmapped] = useState(null);

  useEffect(() => {
    let live = true;
    (async () => {
      if (!worldTopology) {
        worldTopology = await (await fetch('/static/vendor/countries-110m.json')).json();
      }
      if (!live || !svg.current) return;
      setUnmapped(drawMap(svg.current, viz, onPick));
    })();
    return () => { live = false; };
  }, [viz, onPick]);

  const dim = dimensionKey(viz);
  return html`
    <div className="map-frame">
      <svg ref=${svg} className="map" viewBox="0 0 960 500" preserveAspectRatio="xMidYMid meet"></svg>
      ${unmapped && html`<${MapLegend} scale=${unmapped.scale} unit=${viz.encoding?.y?.unit} />`}
      ${unmapped && html`<${UnmappedNote} rows=${unmapped.rows} other=${unmapped.other} dim=${dim} />`}
    </div>`;
}

function drawMap(svg, viz, onPick) {
  svg.innerHTML = '';
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
  const min = Math.min(...values);
  // The ramp's low end has to be unmistakably blue. The previous one started at
  // rgb(232,238,244), which is within three points of the no-data grey — a country with
  // one trial and a country with none were the same colour, and "no trials here" is a
  // different and false claim. The floor is now a clear tint, and the grey moved off-hue.
  const LOW = [219, 231, 245], HIGH = [23, 68, 116];
  const shade = (v) => {
    const t = max === min ? 1 : v / max;
    const c = LOW.map((lo, i) => Math.round(lo + (HIGH[i] - lo) * t));
    return `rgb(${c[0]},${c[1]},${c[2]})`;
  };

  const drawn = new Set();
  for (const feature of countries.features) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    node.setAttribute('d', path(feature) || '');
    const row = byName.get(feature.properties.name);
    if (row) {
      drawn.add(feature.properties.name);
      node.setAttribute('class', 'country datum');
      node.setAttribute('fill', shade(row.value));
      node.addEventListener('click', () => onPick({
        label: row[dim], citations: row.citations || [], total: row.nct_id_total,
      }));
      const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
      title.textContent = `${row[dim]}: ${num(row.value)}`;
      node.append(title);
    } else {
      node.setAttribute('class', 'country');
    }
    svg.append(node);
  }

  return {
    rows: viz.data.filter((r) => r[dim] !== 'Other' && !drawn.has(normalise(r[dim]))),
    other: viz.data.find((r) => r[dim] === 'Other'),
    scale: { min, max, low: shade(min), high: shade(max) },
  };
}

/** A ramp is unreadable without its endpoints, and "no data" needs its own swatch. */
function MapLegend({ scale, unit }) {
  return html`
    <div className="legend">
      <span className="legend-label">${num(scale.min)}</span>
      <span className="ramp" style=${{ background: `linear-gradient(90deg, ${scale.low}, ${scale.high})` }}></span>
      <span className="legend-label">${num(scale.max)} ${unit || ''}</span>
      <span className="legend-gap"></span>
      <span className="swatch nodata"></span>
      <span className="legend-label">no trials in this dataset</span>
    </div>`;
}

function UnmappedNote({ rows, other, dim }) {
  if (!rows.length && !other) return null;
  return html`
    <div className="callout warn">
      <h4>Not drawn on the map</h4>
      <ul>
        ${rows.map((row) => html`
          <li key=${row[dim]}>
            <b>${row[dim]}</b> — ${num(row.value)}. No polygon exists at this map resolution
            (city-states, islands and territories); the value is in the data, it simply
            cannot be shaded.
          </li>`)}
        ${other && html`
          <li key="other">
            <b>Other</b> — ${num(other.value)} distinct trials across the countries beyond the
            top N. A residue is not a place, so it has no polygon.
          </li>`}
      </ul>
    </div>`;
}

// --- network --------------------------------------------------------------------------
//
// A small spring/repulsion simulation. Node area is proportional to weight; edge width to
// co-occurrence weight. `strength` is on the edge too, but it is a derived, normalised
// figure and is shown in the tooltip rather than drawn — see the backend's warning about
// reading it alone.

function Network({ viz, onPick }) {
  const svg = useRef(null);

  useEffect(() => {
    if (!svg.current) return;
    drawNetwork(svg.current, viz, onPick);
  }, [viz, onPick]);

  return html`
    <div className="network-frame">
      <svg ref=${svg} className="network" viewBox="0 0 960 560" preserveAspectRatio="xMidYMid meet"></svg>
    </div>`;
}

function drawNetwork(svg, viz, onPick) {
  svg.innerHTML = '';
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
  const radius = (n) => 6 + 22 * Math.sqrt(n.weight / maxNode);

  // The simulation settles wherever it likes, which left roughly a third of the frame
  // empty. Spread the settled positions to fill it — positions only. Fitting the *viewBox*
  // to the content instead would scale the whole drawing up, and 11px labels would render
  // at 20px; the cap keeps a small graph from being flung into the corners.
  {
    const pad = 46;
    const xs = nodes.map((n) => n.x), ys = nodes.map((n) => n.y);
    const spanX = Math.max(...xs) - Math.min(...xs);
    const spanY = Math.max(...ys) - Math.min(...ys);
    const scale = Math.min(
      spanX > 1 ? (W - 2 * pad) / spanX : 1,
      spanY > 1 ? (H - 2 * pad) / spanY : 1,
      1.45,
    );
    if (scale > 1.02) {
      const cx = (Math.max(...xs) + Math.min(...xs)) / 2;
      const cy = (Math.max(...ys) + Math.min(...ys)) / 2;
      for (const n of nodes) {
        n.x = W / 2 + (n.x - cx) * scale;
        n.y = H / 2 + (n.y - cy) * scale;
      }
    }
    // Keep every node — and the label above or below it — inside the frame.
    for (const n of nodes) {
      const r = radius(n);
      n.x = Math.min(W - r - 8, Math.max(r + 8, n.x));
      n.y = Math.min(H - r - 22, Math.max(r + 22, n.y));
    }
  }

  edges.forEach((e, i) => {
    const line = document.createElementNS(svgNS, 'line');
    line.setAttribute('x1', e.a.x); line.setAttribute('y1', e.a.y);
    line.setAttribute('x2', e.b.x); line.setAttribute('y2', e.b.y);
    line.setAttribute('stroke-width', 1 + 7 * (e.weight / maxEdge));
    line.style.setProperty('--i', String(i));
    line.addEventListener('click', () => onPick({
      label: `${e.a.label} + ${e.b.label}`,
      citations: e.citations || [],
      total: e.nct_id_total,
    }));
    const title = document.createElementNS(svgNS, 'title');
    title.textContent = `${e.a.label} + ${e.b.label}\n${num(e.weight)} shared arm groups`
      + `\nassociation strength ${e.strength} (derived)`;
    line.append(title);
    svg.append(line);
  });

  // Labels sat above every node, so two nodes side by side in the lower half of the
  // cluster overprinted each other ("Dexamethasone" over "Fludarabine phosphate"). Pushing
  // each label away from the centre puts it in the empty margin instead of over a
  // neighbour, and a greedy nudge separates whatever still lands on top.
  const placed = [];
  for (const n of nodes) {
    const r = radius(n);
    const above = n.y <= H / 2;
    let ly = above ? n.y - 9 - r : n.y + 18 + r;
    while (placed.some((p) => Math.abs(p.x - n.x) < 8 * Math.max(p.len, n.label.length)
                              && Math.abs(p.y - ly) < 13)) {
      ly += above ? -14 : 14;
    }
    placed.push({ x: n.x, y: ly, len: n.label.length });

    const circle = document.createElementNS(svgNS, 'circle');
    circle.setAttribute('cx', n.x); circle.setAttribute('cy', n.y);
    circle.setAttribute('r', r);
    const title = document.createElementNS(svgNS, 'title');
    title.textContent = `${n.label}: ${num(n.weight)} trials`;
    circle.append(title);
    svg.append(circle);

    const text = document.createElementNS(svgNS, 'text');
    text.setAttribute('x', n.x);
    text.setAttribute('y', ly);
    text.setAttribute('text-anchor', 'middle');
    text.textContent = n.label;
    svg.append(text);
  }
}

// --- kpi ------------------------------------------------------------------------------

function Kpi({ viz }) {
  const row = viz.data?.[0];
  return html`
    <div className="kpi">
      <div className="kpi-value">${row ? num(Number(row.value)) : '—'}</div>
      <div className="kpi-label">${viz.encoding?.y?.label || ''}</div>
    </div>`;
}

// ------------------------------------------------------------------------------------
// Loading
// ------------------------------------------------------------------------------------

function Pipeline({ startedAt }) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 120);
    return () => clearInterval(id);
  }, []);

  const elapsed = now - startedAt;
  let acc = 0;
  const marks = STAGES.map(([, , ms]) => (acc += ms));
  // The final stage never self-completes: the response arriving is what ends it.
  const active = Math.min(marks.findIndex((m) => elapsed < m), STAGES.length - 1);
  const current = active === -1 ? STAGES.length - 1 : active;

  return html`
    <section className="card pipeline" aria-live="polite">
      <div className="pipeline-head">
        <span className="spark" aria-hidden="true"><i></i><i></i><i></i></span>
        <div>
          <h3>Working through the pipeline</h3>
          <p className="hint">A live question calls the language models and the registry — typically 10–60 seconds.</p>
        </div>
        <span className="clock">${(elapsed / 1000).toFixed(1)}s</span>
      </div>

      <ol className="stages">
        ${STAGES.map(([name, detail], i) => {
          const state = i < current ? 'done' : i === current ? 'active' : 'todo';
          return html`
            <li key=${name} className=${`stage ${state}`}>
              <span className="dot" aria-hidden="true"></span>
              <div className="stage-text">
                <b>${name}</b>
                <span>${detail}</span>
              </div>
            </li>`;
        })}
      </ol>

      <p className="hint footnote">
        Stages advance on typical durations, not on streamed events — the service returns
        one response at the end. The sequence is real; the timing is indicative.
      </p>
    </section>`;
}

// ------------------------------------------------------------------------------------
// Provenance drawer
//
// Citations hang off the datum, not off a response-level map keyed by NCT ID. That map
// could only hold one excerpt per trial, so on a multi-valued dimension every datum but
// the first read a citation stating a different datum's value — clicking Canada showed
// `"country":"United States"` as its evidence. See invariant 5.
// ------------------------------------------------------------------------------------

function Sources({ selection, onClose }) {
  const closer = useRef(null);
  const open = Boolean(selection);

  useEffect(() => {
    if (!open) return undefined;
    closer.current?.focus();
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  const citations = selection?.citations || [];

  return html`
    <div className=${`drawer-root ${open ? 'open' : ''}`}>
      <div className="scrim" onClick=${onClose}></div>
      <aside className="drawer" role="dialog" aria-modal="true" aria-label="Sources"
             aria-hidden=${open ? undefined : 'true'}>
        <header className="drawer-head">
          <div>
            <p className="eyebrow">Traced to the record</p>
            <h3>${selection?.label || ''}</h3>
          </div>
          <button ref=${closer} className="icon" onClick=${onClose} aria-label="Close sources">✕</button>
        </header>

        <p className="drawer-note">
          ${typeof selection?.total === 'number'
            ? html`<b>${num(selection.total)}</b>${' trial(s) contributed this value; '}
                   <b>${citations.length}</b>${' excerpt(s) are cited below. '}`
            : null}
          Each excerpt is a literal span of the record as fetched, re-verified at the byte
          offsets shown before it was emitted. Any citation that failed that check was
          dropped rather than shown.
        </p>

        <div className="drawer-body">
          ${citations.length === 0 && html`
            <p className="empty">
              No excerpt is attached to this datum. Citations are a per-datum sample of up
              to five contributing trials, and one is emitted only where the value could be
              located and verified in the record as fetched — an excerpt that failed that
              check is dropped rather than shown. The complete, reproducible record is the
              request URLs under <code>meta.api_requests</code>.
            </p>`}
          ${citations.map((c, i) => html`
            <article className="cite" key=${`${c.nct_id}-${i}`}>
              <header>
                <a href=${c.url} target="_blank" rel="noopener" title=${c.brief_title || ''}>${c.nct_id}</a>
                <span className=${`badge ${c.supports === 'series' ? 'series' : ''}`}>
                  ${c.supports === 'series' ? 'series' : 'value'}
                </span>
              </header>
              ${c.brief_title && html`<p className="cite-title">${c.brief_title}</p>`}
              <dl>
                <dt>Field</dt><dd><code>${c.field_path}</code></dd>
                <dt>Value</dt><dd>${String(c.field_value)}</dd>
              </dl>
              <pre className="excerpt" title=${`bytes ${c.offset?.[0]}–${c.offset?.[1]} of the serialized record`}>${c.excerpt}</pre>
              <p className="offsets">verified at bytes ${c.offset?.[0]}–${c.offset?.[1]}</p>
            </article>`)}
        </div>
      </aside>
    </div>`;
}

// ------------------------------------------------------------------------------------
// Result
// ------------------------------------------------------------------------------------

function Result({ body, onPick, onOpenAll }) {
  const viz = body.visualization;
  const meta = body.meta || {};
  const drawable = viz && body.response_type !== 'unsupported' && body.response_type !== 'conversational';
  const counts = meta.record_counts;
  const filters = Object.entries(meta.filters_applied || {});
  const pickable = drawable && viz.type !== 'kpi';

  return html`
    <div className="result">
      <section className="card">
        <div className="titles">
          <span className=${`pill ${body.response_type}`}>${(body.response_type || '').replace('_', ' ')}</span>
          <h2>${viz?.title || 'Answer'}</h2>
          ${viz?.subtitle && html`<p className="subtitle">${viz.subtitle}</p>`}
        </div>

        ${body.answer && html`<p className="answer">${body.answer}</p>`}

        ${drawable && html`
          <div className="chart-head">
            <span className="chart-kind">${viz.type.replace('_', ' ')}</span>
            ${pickable && html`<span className="hint">Select a bar, point, country or edge to see the trials behind it.</span>`}
            <button className="ghost" onClick=${onOpenAll}>All sources</button>
          </div>`}

        ${drawable && html`<${Surface} viz=${viz} onPick=${onPick} />`}
      </section>

      ${counts && html`
        <section className="card counts">
          <${Stat} label="Matched" value=${num(counts.matched)} note="in the registry" />
          <${Stat} label="Retrieved" value=${num(counts.retrieved)} note="fetched" />
          <${Stat} label="Used" value=${num(counts.used)} note="folded into the chart" />
          <${Stat}
            label="Excluded"
            value=${num(Object.values(counts.excluded_by_reason || {}).reduce((a, b) => a + b, 0))}
            note=${Object.entries(counts.excluded_by_reason || {}).map(([r, n]) => `${num(n)} ${r}`).join(' · ') || 'none'} />
        </section>`}

      ${(meta.interpretation || filters.length || meta.counting_semantics || meta.assumptions?.length) && html`
        <section className="card">
          <h3 className="section-title">How the question was read</h3>
          ${meta.interpretation && html`<p className="read">${meta.interpretation}</p>`}
          ${filters.length > 0 && html`
            <div className="chips">
              ${filters.map(([leg, spec]) => html`
                <span className="chip" key=${leg}>
                  <b>${leg}</b>${' '}
                  ${typeof spec === 'object' && spec !== null
                    ? Object.entries(spec).map(([k, v]) => `${k}=${v}`).join(', ')
                    : String(spec)}
                </span>`)}
            </div>`}
          ${meta.counting_semantics && html`<p className="hint">${meta.counting_semantics}</p>`}
          ${meta.assumptions?.length > 0 && html`
            <ul className="assumptions">
              ${meta.assumptions.map((a, i) => html`<li key=${i}>${a}</li>`)}
            </ul>`}
        </section>`}

      ${meta.warnings?.length > 0 && html`
        <section className="callout warn">
          <h4>What this chart does not say</h4>
          <ul>${meta.warnings.map((w, i) => html`<li key=${i}>${w}</li>`)}</ul>
        </section>`}

      ${meta.suggested_requests?.length > 0 && html`
        <section className="callout">
          <h4>What can be answered instead</h4>
          <ul>${meta.suggested_requests.map((s, i) => html`
            <li key=${i}>${typeof s === 'string' ? s : JSON.stringify(s)}</li>`)}</ul>
        </section>`}

      <${MetaPanel} meta=${meta} />
    </div>`;
}

function Surface({ viz, onPick }) {
  switch (viz.type) {
    case 'network': return html`<${Network} viz=${viz} onPick=${onPick} />`;
    case 'choropleth': return html`<${Choropleth} viz=${viz} onPick=${onPick} />`;
    case 'kpi': return html`<${Kpi} viz=${viz} />`;
    default: return html`<${ChartSurface} viz=${viz} onPick=${onPick} />`;
  }
}

function Stat({ label, value, note }) {
  return html`
    <div className="stat">
      <span className="stat-label">${label}</span>
      <span className="stat-value">${value}</span>
      <span className="stat-note">${note}</span>
    </div>`;
}

function MetaPanel({ meta }) {
  const counts = meta.record_counts;
  const review = meta.review;

  return html`
    <details className="card meta">
      <summary>Plan, review, counts and the exact API requests</summary>
      <div className="meta-body">
        ${review && html`
          <p className=${`verdict ${review.verdict}`}>
            Reviewer verdict: <b>${review.verdict}</b>${review.revised ? ' (plan was revised)' : ''}
            ${review.concerns?.length > 0 && html`<span> — ${review.concerns.join('; ')}</span>`}
          </p>`}
        ${counts && html`
          <${Block} heading="Records" body=${[
            `matched    ${num(counts.matched)}`,
            `retrieved  ${num(counts.retrieved)}`,
            `used       ${num(counts.used)}`,
            Object.entries(counts.excluded_by_reason || {}).length
              ? `excluded:\n${Object.entries(counts.excluded_by_reason).map(([r, n]) => `    ${num(n)} — ${r}`).join('\n')}`
              : 'excluded:  none',
            '',
            'used + excluded == retrieved is asserted in production; a mismatch returns no chart.',
          ].join('\n')} />`}
        ${meta.plan && html`<${Block} heading="Plan" body=${JSON.stringify(meta.plan, null, 2)} />`}
        ${meta.api_requests?.length > 0 && html`
          <${Block} heading="API requests issued" body=${meta.api_requests.join('\n\n')} />`}
        ${meta.planning_trace && html`
          <${Block} heading="Planning trace" body=${JSON.stringify(meta.planning_trace, null, 2)} />`}
        <p className="hint">
          ${meta.llm_provider ? `provider ${meta.llm_provider}` : ''}
          ${meta.cache_hit ? ' · registry responses served from cache' : ''}
          ${typeof meta.elapsed_ms === 'number' ? ` · ${num(meta.elapsed_ms)} ms` : ''}
        </p>
      </div>
    </details>`;
}

function Block({ heading, body }) {
  return html`<h4>${heading}</h4><pre>${body}</pre>`;
}

// ------------------------------------------------------------------------------------
// App
// ------------------------------------------------------------------------------------

/* A trail gaining weight toward the value it lands on, arriving one step at a time — the
   same motion as following a datum back to its record. Drawn rather than vendored: sharp
   at any density, themable from the accent variable, and one less asset to ship. */
function Mark() {
  return html`
    <svg className="mark" viewBox="0 0 28 28" aria-hidden="true">
      <circle cx="4" cy="21" r="1.7" opacity=".3" />
      <circle cx="10" cy="18" r="2" opacity=".5" />
      <circle cx="16" cy="13" r="2.4" opacity=".72" />
      <circle cx="23" cy="7" r="3.4" />
    </svg>`;
}

function App() {
  const [query, setQuery] = useState('');
  const [examples, setExamples] = useState([]);
  const [chosen, setChosen] = useState(null);
  const [body, setBody] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(null);      // null | {live: bool, startedAt: number}
  const [selection, setSelection] = useState(null);

  useEffect(() => {
    (async () => {
      try {
        setExamples(await (await fetch('/examples')).json());
      } catch {
        /* No captured runs is a normal state — the query box still works. */
      }
    })();
  }, []);

  const onPick = useCallback((sel) => setSelection(sel), []);
  const closeSources = useCallback(() => setSelection(null), []);

  /** Every citation on the page, for the "All sources" view. */
  const openAll = useCallback(() => {
    const data = body?.visualization?.data;
    const units = data ? (data.edges || data) : [];
    setSelection({
      label: body?.visualization?.title || 'Every cited excerpt',
      citations: units.flatMap((u) => u.citations || []),
      total: null,
    });
  }, [body]);

  async function loadExample(entry) {
    const slug = entry.file.replace(/\.json$/, '');
    setChosen(slug);
    setError(null);
    setBusy({ live: false, startedAt: Date.now() });
    try {
      const response = await fetch(`/examples/${slug}`);
      if (!response.ok) throw new Error(await response.text());
      const captured = await response.json();
      setQuery(captured.meta?.interpretation || entry.query || '');
      setBody(captured);
    } catch (e) {
      setBody(null);
      setError({ message: `Could not load the example: ${e.message}` });
    } finally {
      setBusy(null);
    }
  }

  async function onAsk(event) {
    event.preventDefault();
    const asked = query.trim();
    if (!asked || busy) return;
    setChosen(null);
    setError(null);
    setSelection(null);
    setBusy({ live: true, startedAt: Date.now() });
    try {
      const response = await fetch('/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: asked }),
      });
      const payload = await response.json();
      if (!response.ok) {
        // The backend's failure modes are part of its design, so show what it actually
        // said rather than a generic apology. An invariant failure in particular is a
        // deliberate refusal to return a chart, not a crash.
        setBody(null);
        setError({
          message: payload.message || payload.detail || `Request failed (${response.status}).`,
          detail: payload.detail,
        });
        return;
      }
      setBody(payload);
    } catch (e) {
      setBody(null);
      setError({ message: `Could not reach the service: ${e.message}` });
    } finally {
      setBusy(null);
    }
  }

  return html`
    <${React.Fragment}>
      <header className="masthead">
        <div className="wrap">
          <div className="brand">
            <${Mark} />
            <h1>Trace</h1>
          </div>
          <p className="lede">
            The name is the claim, twice. <b>Trace</b> follows a question about clinical
            trials through to a chart — and traces every value in that chart back to the
            trial record it came from, quotable and verified at its byte offsets. Select
            anything drawn to see the records behind it.
          </p>
          <p className="colophon">
            This page is a plain client of the documented response envelope: it reads
            <code>encoding</code>, <code>type</code> and each datum's <code>citations</code>,
            and nothing else.
          </p>
        </div>
      </header>

      <main className="wrap">
        <section className="card ask">
          <form onSubmit=${onAsk}>
            <input
              value=${query}
              onChange=${(e) => setQuery(e.target.value)}
              autoComplete="off"
              aria-label="Your question"
              placeholder="How are melanoma trials distributed across phases?" />
            <button type="submit" disabled=${Boolean(busy)}>${busy?.live ? 'Working…' : 'Ask'}</button>
          </form>
          <p className="hint">
          </p>
          ${examples.length > 0 && html`
            <div className="examples">
              ${examples.map((entry) => {
                const slug = entry.file.replace(/\.json$/, '');
                return html`
                  <button key=${slug} type="button" title=${entry.query}
                          className=${chosen === slug ? 'chip-btn on' : 'chip-btn'}
                          aria-pressed=${chosen === slug}
                          onClick=${() => loadExample(entry)}>
                    ${entry.title}
                  </button>`;
              })}
            </div>`}
        </section>

        ${busy?.live && html`<${Pipeline} startedAt=${busy.startedAt} />`}
        ${busy && !busy.live && html`<section className="card skeleton"><span></span><span></span><span></span></section>`}

        ${error && html`
          <section className="callout warn">
            <h4>The service declined to answer</h4>
            <p><b>${error.message}</b></p>
            ${error.detail && html`<pre>${typeof error.detail === 'string' ? error.detail : JSON.stringify(error.detail, null, 2)}</pre>`}
          </section>`}

        ${body && !busy && html`<${Result} body=${body} onPick=${onPick} onOpenAll=${openAll} />`}
      </main>

      <${Sources} selection=${selection} onClose=${closeSources} />
    <//>`;
}

ReactDOM.createRoot(document.getElementById('root')).render(html`<${App} />`);
