"""The frontend's smoke test — a real render in headless Chrome, not in the test suite.

There is no browser extension in this environment, so the check drives Chrome over the
DevTools protocol directly. It has to be a real render: `node --check` proves only syntax,
and every frontend bug found so far was invisible to it — the citation map that verified
perfectly at its offsets while showing the wrong bucket's excerpt (invariant 5), and the
`path.country` rule whose specificity reverted every shaded country to the empty-state
grey. Both drew a page that looked finished and encoded something false.

So each captured example is loaded, a datum is clicked, and the drawer's excerpt is
checked against the datum that was clicked. It is not collected by pytest (the file is not
named `test_*`) because it needs Chrome and a running server.

Run:  .venv/bin/python -m uvicorn cheiron.api.app:app --port 8123     # in another shell
      .venv/bin/python tests/ui_smoke.py [base-url]
"""

import asyncio
import json
import subprocess
import sys
import time
import urllib.request

import websockets

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9333
BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8123").rstrip("/")
UI = f"{BASE}/ui"


class Page:
    def __init__(self, ws):
        self.ws = ws
        self.n = 0
        self.console = []

    async def send(self, method, **params):
        self.n += 1
        mid = self.n
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("method") == "Runtime.consoleAPICalled":
                args = " ".join(str(a.get("value", a.get("description", "")))
                                for a in msg["params"]["args"])
                self.console.append(f'{msg["params"]["type"]}: {args}')
            elif msg.get("method") == "Runtime.exceptionThrown":
                d = msg["params"]["exceptionDetails"]
                self.console.append("exception: " + (d.get("exception", {}).get("description")
                                                     or d.get("text", "")))
            elif msg.get("id") == mid:
                return msg

    async def js(self, expr):
        r = await self.send("Runtime.evaluate", expression=expr,
                            awaitPromise=True, returnByValue=True)
        result = r.get("result", {})
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"])
        return result.get("result", {}).get("value")


async def main():
    proc = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
         "--no-first-run", "--no-default-browser-check", "--window-size=1280,1600",
         "--user-data-dir=/tmp/cheiron-cdp", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
                page = next(t for t in tabs if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.4)
        else:
            raise SystemExit("Chrome never came up")

        async with websockets.connect(page["webSocketDebuggerUrl"],
                                      max_size=40_000_000) as ws:
            p = Page(ws)
            await p.send("Runtime.enable")
            await p.send("Page.enable")
            await p.send("Page.navigate", url=UI)
            await asyncio.sleep(2.5)

            failures = []

            def check(name, ok, detail=""):
                print(("  ok   " if ok else "  FAIL ") + name + (f"  {detail}" if detail else ""))
                if not ok:
                    failures.append(name)

            n_examples = await p.js("document.querySelectorAll('.examples .chip-btn').length")
            check("example chips rendered", n_examples == 8, f"got {n_examples}")

            index = json.load(urllib.request.urlopen(f"{BASE}/examples"))
            for i, entry in enumerate(index):
                print(f"\n{entry['file']}  ({entry['visualization']})")
                await p.js(f"document.querySelectorAll('.examples .chip-btn')[{i}].click()")
                await asyncio.sleep(1.6)

                title = await p.js("document.querySelector('.titles h2')?.textContent || ''")
                check("title rendered", bool(title.strip()), title[:60])

                answer = await p.js("document.querySelector('.answer')?.textContent || ''")
                check("answer rendered", bool(answer.strip()))

                kind = entry["visualization"]
                if kind == "network":
                    nodes = await p.js("document.querySelectorAll('.network circle').length")
                    edges = await p.js("document.querySelectorAll('.network line').length")
                    check("network drawn", nodes == 10 and edges == 41,
                          f"{nodes} nodes / {edges} edges")
                    target = ".network line"
                elif kind == "choropleth":
                    # top_n bounds the plotted set; "Other" is a residue, not a place, and
                    # must be reported under the map rather than shaded.
                    viz = json.load(urllib.request.urlopen(
                        f"{BASE}/examples/{entry['file'][:-5]}"))["visualization"]
                    dim = viz["encoding"]["x"]["field"]
                    places = [r for r in viz["data"] if r[dim] != "Other"]
                    shaded = await p.js("document.querySelectorAll('.map path.datum').length")
                    check("every mapped country shaded", shaded == len(places),
                          f"{shaded} of {len(places)}")
                    listed = await p.js(
                        "document.querySelector('.callout.warn h4')?.textContent || ''")
                    check("the Other residue is reported, not dropped",
                          "Not drawn on the map" in listed, listed)
                    # The specificity trap: a shaded country must not be the empty grey.
                    # Read the grey from the stylesheet rather than pinning a literal, so
                    # retuning the palette cannot quietly make this assertion vacuous.
                    grey = await p.js(
                        "getComputedStyle(document.querySelector("
                        "'.map path.country:not(.datum)')).fill")
                    fill = await p.js(
                        "getComputedStyle(document.querySelector('.map path.datum')).fill")
                    check("shaded fill is not the empty-state grey", fill != grey,
                          f"{fill} vs {grey}")
                    target = ".map path.datum"
                elif kind == "kpi":
                    target = None
                else:
                    pts = await p.js(
                        "(()=>{const c=Chart.getChart("
                        "document.querySelector('.chart-frame canvas'));"
                        "return c ? c.data.datasets.reduce((a,d)=>a+d.data.length,0) : 0})()")
                    check("chart.js has data", pts > 0, f"{pts} points")
                    ctype = await p.js(
                        "Chart.getChart(document.querySelector("
                        "'.chart-frame canvas'))?.config.type")
                    expected = {"line": "line", "bar": "bar", "grouped_bar": "bar",
                                "stacked_bar": "bar", "pie": "pie", "scatter": "scatter",
                                "stacked_area": "line"}[kind]
                    check("chart type matches the envelope", ctype == expected,
                          f"{kind} -> {ctype}")
                    target = None  # clicked via the Chart.js hit path below

                # --- the drawer, and invariant 5: the excerpt must be for THIS datum ---
                if kind in ("bar", "grouped_bar", "line", "pie", "stacked_bar", "scatter"):
                    # Pick the first point that actually carries evidence. On a scatter each
                    # datum is one trial and only 522 of 3,625 have a verified excerpt, so
                    # index 0 is a legitimate empty state rather than a bug.
                    opened = await p.js("""(()=>{
                      const cv=document.querySelector('.chart-frame canvas');
                      const c=Chart.getChart(cv);
                      const d=c.data.datasets[0].data;
                      let i=0;
                      for (let k=0;k<d.length;k++) {
                        const row=d[k] && d[k].row;
                        if (!row || (row.citations||[]).length) { i=k; break }
                      }
                      const el=c.getDatasetMeta(0).data[i];
                      const r=cv.getBoundingClientRect();
                      for (const t of ['mousedown','mouseup','click']) {
                        cv.dispatchEvent(new MouseEvent(t,
                          {clientX:r.left+el.x, clientY:r.top+el.y, bubbles:true}));
                      }
                      return true})()""")
                elif target:
                    await p.js(f"document.querySelector('{target}').dispatchEvent("
                               "new MouseEvent('click',{{bubbles:true}}))"
                               .replace("{{", "{").replace("}}", "}"))
                    opened = True
                else:
                    opened = False

                if opened:
                    await asyncio.sleep(0.6)
                    is_open = await p.js(
                        "document.querySelector('.drawer-root')?.classList.contains('open')")
                    check("sources drawer opened", bool(is_open))
                    label = await p.js(
                        "document.querySelector('.drawer-head h3')?.textContent || ''")
                    cites = await p.js("document.querySelectorAll('.cite').length")
                    check("drawer has citations", cites > 0, f'"{label}" — {cites} excerpts')

                    # Invariant 5: the excerpt must state the value it is cited for.
                    if kind in ("bar", "choropleth", "grouped_bar"):
                        ok = await p.js("""(()=>{
                          const label=document.querySelector('.drawer-head h3')
                            .textContent.split(' · ')[0].trim();
                          const vals=[...document.querySelectorAll('.cite dd')]
                            .filter((_,i)=>i%2===1).map(d=>d.textContent.trim());
                          const ex=[...document.querySelectorAll('.excerpt')].map(e=>e.textContent);
                          return JSON.stringify({label, vals:[...new Set(vals)].slice(0,4),
                                                 sample: ex[0]})})()""")
                        info = json.loads(ok)
                        stated = any(info["label"].lower() in v.lower()
                                     or v.lower() in info["label"].lower()
                                     for v in info["vals"])
                        check("cited value matches the clicked datum", stated,
                              json.dumps(info)[:220])

                    await p.js("document.querySelector('.drawer .icon').click()")
                    await asyncio.sleep(0.4)
                    closed = await p.js(
                        "!document.querySelector('.drawer-root').classList.contains('open')")
                    check("drawer closes", bool(closed))

                # A datum with no verified excerpt must say so, not open a blank panel.
                if kind == "scatter":
                    await p.js("""(()=>{
                      const cv=document.querySelector('.chart-frame canvas');
                      const c=Chart.getChart(cv);
                      const d=c.data.datasets[0].data;
                      const k=d.findIndex(p=>!(p.row.citations||[]).length);
                      const el=c.getDatasetMeta(0).data[k];
                      const r=cv.getBoundingClientRect();
                      for (const t of ['mousedown','mouseup','click']) {
                        cv.dispatchEvent(new MouseEvent(t,
                          {clientX:r.left+el.x, clientY:r.top+el.y, bubbles:true}));
                      }
                      return k})()""")
                    await asyncio.sleep(0.5)
                    empty = await p.js("document.querySelectorAll('.drawer .empty').length")
                    check("uncited datum explains itself", empty == 1, f"{empty} empty-state")
                    await p.js("document.querySelector('.drawer .icon').click()")
                    await asyncio.sleep(0.3)

                # counts strip and warnings survive every response type
                stats = await p.js("document.querySelectorAll('.stat').length")
                check("counts strip", stats in (0, 4), f"{stats} stats")

            # --- the loading pipeline, without spending a model call ---
            print("\nloading pipeline")
            await p.js("""(()=>{
              const f=window.fetch;
              window.fetch=(u,o)=> String(u).includes('/analyze')
                ? new Promise(()=>{})   // never resolves: hold the loading state open
                : f(u,o);
              const i=document.querySelector('.ask input');
              const set=Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype,'value').set;
              set.call(i,'how many trials mention pembrolizumab');
              i.dispatchEvent(new Event('input',{bubbles:true}));
              document.querySelector('.ask form').dispatchEvent(
                new Event('submit',{bubbles:true,cancelable:true}));
              return true})()""")
            await asyncio.sleep(1.2)
            stages = await p.js("document.querySelectorAll('.stage').length")
            check("pipeline stages rendered", stages == 6, f"{stages} stages")
            active = await p.js("document.querySelectorAll('.stage.active').length")
            check("exactly one active stage", active == 1, f"{active} active")
            t1 = await p.js("document.querySelector('.clock').textContent")
            await asyncio.sleep(1.4)
            t2 = await p.js("document.querySelector('.clock').textContent")
            check("clock advances", t1 != t2, f"{t1} -> {t2}")
            adv = await p.js("document.querySelectorAll('.stage.done').length")
            check("stages advance", adv >= 1, f"{adv} done")

            print("\nconsole:")
            noise = [c for c in p.console if "favicon" not in c]
            for line in noise:
                print("  " + line)
            check("no console errors or React warnings",
                  not any(w in c.lower() for c in noise
                          for w in ("error", "warning", "exception", "invalid")))

            print()
            if failures:
                print(f"FAILED: {len(failures)} — " + "; ".join(failures))
                sys.exit(1)
            print("all checks passed")
    finally:
        proc.terminate()


asyncio.run(main())
