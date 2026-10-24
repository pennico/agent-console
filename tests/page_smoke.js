// node tests/page_smoke.js console/index.html status.json
// Runs the page's script without a browser against a status.json, renders every view and
// prints a JSON summary; any exception fails the run. Used by tests/test_collector.py.
const fs = require('fs');
const [,, PAGE, DATA] = process.argv;
const html = fs.readFileSync(PAGE, 'utf8');
const script = html.slice(html.indexOf('<script>') + 8, html.lastIndexOf('</script>'));
const els = {};
const mk = () => ({innerHTML: '', textContent: '', hidden: false, value: '', dataset: {}, clientWidth: 1100, offsetWidth: 260, offsetHeight: 40,
  classList: {toggle() {}, add() {}, remove() {}, contains: () => false}, style: {}, scrollTop: 0, contains: () => false,
  setAttribute(k, v) { this[k] = v; }, removeAttribute() {}, focus() {}, blur() {}, replaceChildren() {}, append() {},
  getBoundingClientRect: () => ({left: 0, top: 0, width: 10, height: 10, bottom: 10}), querySelector: () => mk(), querySelectorAll: () => []});
Object.assign(global, {window: global, innerWidth: 1200, addEventListener() {}, setInterval: () => 0,
  location: {hash: '', hostname: 'localhost'}, history: {replaceState() {}},
  localStorage: {getItem: () => null, setItem() {}, removeItem() {}}});
global.document = {getElementById: id => els[id] || (els[id] = mk()), addEventListener() {}, createElement: () => mk(),
  querySelectorAll: () => [], querySelector: () => null, activeElement: null};
const D = fs.readFileSync(DATA, 'utf8');
global.fetch = async url => ({ok: url.startsWith('status.json'), status: url.startsWith('status.json') ? 200 : 404,
  json: async () => JSON.parse(D)});
process.on('unhandledRejection', e => { console.error(e); process.exit(1); });
eval(script + `
setTimeout(() => {
  if (!data) { console.error("status.json did not load"); process.exit(1); }
  const out = {};
  for (const [v, m] of [["agents", "location"], ["agents", "roles"], ["schedules", "location"], ["connections", "location"]]) {
    view = v; agentsMode = m; render(true);
    out[v + ":" + m] = items.map(i => ({id: i.id, name: i.name, section: i.section, status: i.status, head: i.head}));
    for (const it of items) { sel[SK()] = it.id; renderDetail(); }
  }
  out.search = buildSearchIndex().length;
  setHdrPop("account");
  out.account = document.getElementById("acct-pop").innerHTML.replace(/<[^>]+>/g, " ").replace(/\\s+/g, " ");   // \\s: this code sits in a template string
  view = "agents"; agentsMode = "location"; render(true); sel[SK()] = "bridge";
  askDraft = "Why is it down?"; out.ask = askPrompt();
  console.log(JSON.stringify(out));
}, 100);`);
