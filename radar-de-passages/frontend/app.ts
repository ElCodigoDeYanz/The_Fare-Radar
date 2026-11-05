export {};
type Kind = "flight" | "bus" | "product";
type AlertMode = "target" | "smart" | "both";
type Criteria = { kind: Kind; currency: string } & Record<string, unknown>;
type Price = {
  id: string;
  total_cents: number;
  currency: string;
  observed_at: number;
  expires_at: number;
  expired: boolean;
  source: string;
  synthetic: number;
  conditions: Record<string, unknown>;
  analytics?: { change_percent: number | null; note: string };
};
type QueryRow = {
  started_at: number;
  status: string;
  error: string | null;
  error_kind: string | null;
};
type Watch = {
  id: string;
  kind: Kind;
  provider: string;
  provider_available: boolean;
  category: string | null;
  subcategory: string | null;
  label: string | null;
  criteria: Criteria;
  target_cents: number;
  alert_mode: AlertMode;
  enabled: number;
  channel: string;
  next_run_at: number | null;
  schedule_status: string | null;
  last_query: QueryRow | null;
  last_success_at: number | null;
  latest_offers: Price[];
};
type Alert = {
  created_at: number;
  channel: string;
  status: string;
  message: string;
  error: string | null;
  priority: string | null;
  priority_rank: number;
};
type CatalogRow = {
  id: string;
  source_id: string;
  domain: string;
  mode: "listing" | "sitemap";
  url: string;
  category: string | null;
  max_items: number;
  enabled: number;
  next_run_at: number;
  fail_count: number;
  last_status: string | null;
  last_error: string | null;
  found_count: number;
  watches: number;
};
type Incident = {
  at: number;
  domain: string;
  kind: string;
  severity: "low" | "medium" | "high";
  action: string;
  http_status: number | null;
  detail: string;
};
type StoreRow = {
  domain: string | null;
  availability: string | null;
  price_cents: number;
  expired: boolean;
};
type Group = {
  title: string | null;
  match_key: string;
  currency: string;
  condition: string | null;
  stores: StoreRow[];
  cheapest: string | null;
  spread_percent: number | null;
  saving_cents: number | null;
  ranked_count: number;
};
type SourceRow = {
  id: string;
  name: string;
  kind: string;
  country: string;
  domain: string;
  status: string;
  enabled: boolean;
};
type Egress = { name: string; kind: string; enabled: boolean; endpoint: string | null };
type Resources = {
  cpu_seconds: number;
  cpu_percent_of_one_core: number;
  rss_mb: number | null;
  database_mb: number;
  disk_free_gb: number | null;
  requests_total: number;
  downloaded_mb: number;
  downloaded_mb_per_day: number;
  incidents_24h: number;
  rows: Record<string, number>;
  note: string;
};
type History = {
  observations: Price[];
  queries: QueryRow[];
  observation_count: number;
  query_count: number;
};
type ProviderInfo = {
  name: string;
  kinds: Kind[];
  live: boolean;
  monthly_cap: number | null;
};
type Catalog = {
  kinds: Record<string, { label: string; group: string }>;
  categories: Record<string, { label: string; subcategories: string[] }>;
  providers: ProviderInfo[];
};
type Status = {
  scheduler: {
    running: boolean;
    holds_lease: boolean;
    interval_s: [number, number];
    domain_gap_s: [number, number];
    enabled_watches: number;
    due_now: number;
    last_tick_at: number | null;
    last_error: string | null;
    capacity: { provider: string; watches: number; effective_interval_s: number; note: string }[];
  };
  domains: {
    domain: string;
    status: string;
    block_count: number;
    blocked_until: number;
    last_error: string | null;
  }[];
  usage_this_month: Record<string, number>;
  providers: ProviderInfo[];
  egress: Egress[];
  resources: Resources;
};
const get = <T extends HTMLElement>(id: string): T => {
  const node = document.getElementById(id);
  if (!node) throw new Error(id);
  return node as T;
};
const esc = (v: unknown): string =>
  String(v).replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ] ?? c,
  );
const plural = (n: number, one: string, many: string): string =>
  `${n} ${n === 1 ? one : many}`;
const text = (v: unknown): string => (typeof v === "string" ? v : "");
const num = (v: unknown): number => (typeof v === "number" ? v : 0);
const notice = (m: string, error = false): void => {
  get("notice").textContent = m;
  get("notice").classList.toggle("error", error);
};
const fail = (e: unknown): void =>
  notice(e instanceof Error ? e.message : "Operación fallida.", true);
async function api<T>(url: string, options?: RequestInit): Promise<T> {
  const r = await fetch(url, options);
  if (!r.ok) {
    let data: unknown = null;
    try {
      data = await r.json();
    } catch {
      data = null;
    }
    throw new Error(
      typeof data === "object" && data !== null && "detail" in data
        ? typeof data.detail === "string"
          ? data.detail
          : JSON.stringify(data.detail)
        : `HTTP ${r.status}`,
    );
  }
  return r.json() as Promise<T>;
}
const money = (cents: number, currency: string): string =>
  new Intl.NumberFormat("es-PE", { style: "currency", currency }).format(
    cents / 100,
  );
const utc = (t: number): string =>
  new Date(t * 1000).toISOString().slice(0, 19).replace("T", " ");
const inMinutes = (t: number): string => {
  const m = Math.round((t - Date.now() / 1000) / 60);
  return m <= 0 ? "ahora" : m < 90 ? `en ${m} min` : `en ${(m / 60).toFixed(1)} h`;
};
const integer = (id: string): number => Number(get<HTMLInputElement>(id).value);
const value = (id: string): string => get<HTMLInputElement>(id).value.trim();
const list = (id: string, upper = false): string[] =>
  value(id)
    .split(",")
    .map((s) => (upper ? s.trim().toUpperCase() : s.trim()))
    .filter((s) => s.length > 0);
function cents(id: string): number {
  const t = value(id);
  if (!/^\d{1,8}(\.\d{1,2})?$/.test(t))
    throw new Error("Usa un importe positivo con punto y hasta dos decimales.");
  const [whole, fraction = ""] = t.split(".");
  const v = Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
  if (v < 1) throw new Error("El objetivo debe ser mayor que cero.");
  return v;
}
const states: Record<string, string> = {
  success: "Correcta",
  error: "Fallida",
  running: "En curso",
  simulated: "Simulada",
  pending: "En cola; sin enviar",
  sent: "Enviada",
  uncertain: "Sin confirmar",
  failed: "Fallida",
  sending: "Enviando",
  past: "Fecha pasada",
  blocked: "Bloqueo del sitio",
  rate_limited: "Límite del sitio",
  budget: "Tope mensual",
  transient: "Fallo temporal",
  invalid: "Respuesta inválida",
  cooling: "En pausa",
  disabled: "Desactivado",
  ok: "Activo",
  captcha: "Verificación humana (CAPTCHA)",
  denied: "Acceso denegado",
  session: "Pide iniciar sesión",
  redirect: "Redirección sospechosa",
  structure: "Cambió la estructura",
  gone: "Página inexistente",
  empty: "Respuesta vacía",
  timeout: "Tiempo agotado",
  network: "Error de red",
  egress: "Salida de red apagada",
  circuit_open: "Circuito abierto",
  domain_paused: "Sitio en pausa",
  domain_cooling: "Sitio descansando",
  watch_backoff: "Seguimiento en espera",
  retry_backoff: "Reintento espaciado",
  objetivo: "Objetivo",
  baja: "Baja",
  media: "Media",
  alta: "Alta",
  maxima: "Máxima",
  smart: "Inteligente",
  target: "Precio objetivo",
  both: "Ambos",
};
const kindLabel: Record<Kind, string> = {
  flight: "Vuelo",
  bus: "Bus",
  product: "Producto",
};
let watches: Watch[] = [];
let catalogRows: CatalogRow[] = [];
let sourcesLoaded = false;
let catalog: Catalog = { kinds: {}, categories: {}, providers: [] };
let selected: string | null = null;
let configKey: string | null = null;
let offset = 0;
let alertOffset = 0;
const currentKind = (): Kind => {
  const k = get<HTMLSelectElement>("kind").value;
  return k === "bus" || k === "product" ? k : "flight";
};
const providerFor = (name: string): ProviderInfo | undefined =>
  catalog.providers.find((p) => p.name === name);
const sourceTag = (w: Watch): string => {
  const p = providerFor(w.provider);
  return `<span class="tag ${p?.live ? "live" : "synthetic"}">${esc(w.provider)}${p?.live ? " · real" : " · sintético"}</span>`;
};
function title(w: Watch): string {
  const c = w.criteria;
  if (w.kind === "product") {
    try {
      const u = new URL(text(c.url));
      return esc(w.label ?? u.hostname + u.pathname);
    } catch {
      return esc(w.label ?? text(c.url));
    }
  }
  const prefix = w.kind === "bus" ? "Bus " : "";
  return `${esc(w.label ? w.label + " · " : "")}${prefix}${esc(text(c.origin))} → ${esc(text(c.destination))}`;
}
function detail(w: Watch): string {
  const c = w.criteria;
  const goal =
    w.alert_mode === "smart"
      ? "Alerta inteligente (por prioridad)"
      : `Objetivo: ${money(w.target_cents, c.currency)}${w.alert_mode === "both" ? " + inteligente" : ""}`;
  if (w.kind === "flight")
    return `${plural(num(c.adults), "adulto", "adultos")} · ${plural(num(c.children), "niño", "niños")} · ${plural(num(c.infants), "infante", "infantes")}<br>${esc(text(c.cabin))}<br>${esc(goal)}`;
  if (w.kind === "bus")
    return `${plural(num(c.passengers), "pasajero", "pasajeros")}<br>${esc(goal)}`;
  const cat = catalog.categories[w.category ?? ""]?.label ?? w.category ?? "";
  return `${esc(cat)}${w.subcategory ? " / " + esc(w.subcategory.replace(/_/g, " ")) : ""}<br>${esc(goal)}`;
}
function dates(w: Watch): string {
  const c = w.criteria;
  if (w.kind === "product")
    return `<span class="route-meta">Precio de página · ${esc(c.currency)}</span>`;
  return `${esc(text(c.departure))}<div class="route-meta">${esc(text(c.return_date) || "Solo ida")} · ${esc(c.currency)}</div>`;
}
function schedule(w: Watch): string {
  if (!w.enabled) return "Pausado (alertas y revisión desactivadas)";
  if (!w.provider_available) return "Fuente no configurada en este servidor";
  if (w.next_run_at === null) return "Primera revisión pendiente";
  const label =
    w.schedule_status && w.schedule_status !== "success"
      ? ` · último intento: ${states[w.schedule_status] ?? w.schedule_status}`
      : "";
  return `Próxima revisión ${inMinutes(w.next_run_at)}${label}`;
}
function fillSelect(
  id: string,
  options: [string, string][],
  keep = true,
): void {
  const select = get<HTMLSelectElement>(id);
  const previous = select.value;
  select.innerHTML = options
    .map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`)
    .join("");
  if (keep && options.some(([v]) => v === previous)) select.value = previous;
}
function syncForm(): void {
  const kind = currentKind();
  document
    .querySelectorAll<HTMLFieldSetElement>("fieldset.kind-group")
    .forEach((f) => {
      const on = f.dataset.kind === kind;
      f.hidden = !on;
      f.disabled = !on; // disabled groups are skipped by form validation
    });
  const options = catalog.providers
    .filter((p) => p.kinds.includes(kind))
    .sort((a, b) => Number(b.live) - Number(a.live))
    .map((p): [string, string] => [
      p.name,
      `${p.name} (${p.live ? "datos reales" : "sintético"})`,
    ]);
  fillSelect("provider", options.length ? options : [["", "Sin fuente disponible"]], false);
  get<HTMLButtonElement>("create-button").disabled = options.length === 0;
  const mode = get<HTMLSelectElement>("alert-mode");
  if (!mode.dataset.touched) mode.value = kind === "product" ? "smart" : "target";
  syncMode();
  get("target-help").textContent =
    kind === "product"
      ? "El objetivo es el precio de la página para una unidad."
      : "El objetivo es el total para todos los pasajeros.";
  get<HTMLSelectElement>("currency").value = kind === "flight" ? "USD" : "PEN";
  syncSubcategories();
  const p = providerFor(get<HTMLSelectElement>("provider").value);
  get("create-hint").textContent = p?.live
    ? "Esta fuente consulta datos reales; el sistema respeta robots.txt, espacia las consultas y pausa el sitio si responde con bloqueos."
    : "Fuente sintética: valores de prueba, no son precios reales.";
}
const modeHelp: Record<AlertMode, string> = {
  smart:
    "Avisa por prioridad (baja ≥8 %, media ≥15 %, alta ≥25 %, máxima ≥40 % bajo el precio de referencia) comparando con el historial propio y con otras tiendas. No necesita objetivo.",
  target: "Avisa cuando el precio vigente llega a tu objetivo.",
  both: "Avisa por objetivo y también por prioridad; si coinciden, manda la más importante.",
};
function currentMode(): AlertMode {
  const v = get<HTMLSelectElement>("alert-mode").value;
  return v === "smart" || v === "both" ? v : "target";
}
function syncMode(): void {
  const mode = currentMode();
  get<HTMLInputElement>("target").required = mode !== "smart";
  get("target-label").hidden = mode === "smart";
  get("mode-help").textContent = modeHelp[mode];
}
function syncSubcategories(): void {
  const cat = get<HTMLSelectElement>("category").value;
  const subs = catalog.categories[cat]?.subcategories ?? [];
  fillSelect("subcategory", [
    ["", "(sin subcategoría)"],
    ...subs.map((s): [string, string] => [s, s.replace(/_/g, " ")]),
  ]);
}
function renderStatus(status: Status): void {
  const s = status.scheduler;
  const anyLive = status.providers.some((p) => p.live);
  get("mode-pill").textContent = anyLive ? "FUENTES REALES" : "DEMO SINTÉTICA";
  get("source-label").textContent = anyLive
    ? "Fuentes reales activas"
    : "Fuente sintética";
  get("mode-title").textContent = anyLive
    ? "Monitoreo con fuentes reales"
    : "Sin fuentes reales configuradas";
  get("mode-text").textContent = anyLive
    ? "Los precios vienen de las fuentes marcadas como «real». Verifica siempre en la tienda o aerolínea antes de decidir."
    : "Solo está activa la fuente sintética: los valores sirven para probar el motor y no son precios de mercado. Configura una fuente real en el servidor (ver README).";
  get("mode-footer").textContent = anyLive
    ? "Los datos reales pueden cambiar o estar desactualizados."
    : "Sin conexión a tiendas ni aerolíneas reales.";
  const [lo, hi] = s.interval_s;
  const usage = Object.entries(status.usage_this_month)
    .map(([p, n]) => {
      const cap = providerFor(p)?.monthly_cap;
      return `${esc(p)}: ${n}${cap ? " / " + cap : ""}`;
    })
    .join(" · ");
  get("status-list").innerHTML = [
    `Revisión automática: ${s.running ? "<strong>activa</strong>" : "<strong>apagada</strong> (RADAR_SCHEDULER=1 para activarla)"}`,
    `Cada seguimiento se revisa a intervalos aleatorios de ${Math.round(lo / 60 * 10) / 10}–${Math.round(hi / 60 * 10) / 10} min`,
    `Espaciado mínimo por sitio: ${s.domain_gap_s[0]}–${s.domain_gap_s[1]} s`,
    `Seguimientos activos: ${s.enabled_watches} · pendientes ahora: ${s.due_now}`,
    s.last_tick_at ? `Último ciclo: ${utc(s.last_tick_at)} UTC` : "Aún sin ciclos",
    usage ? `Consultas este mes: ${usage}` : "",
    s.last_error ? `Último error interno: ${esc(s.last_error)}` : "",
  ]
    .filter((l) => l)
    .map((l) => `<li>${l}</li>`)
    .join("");
  get("domain-list").innerHTML = status.domains
    .filter((d) => d.status !== "ok" || d.block_count > 0)
    .map(
      (d) =>
        `<div class="domain-row"><span><strong>${esc(d.domain)}</strong> <span class="tag ${esc(d.status)}">${esc(states[d.status] ?? d.status)}</span><br><span class="route-meta">${d.status === "cooling" ? "Reanuda " + inMinutes(d.blocked_until) + " · " : ""}${esc(d.last_error ?? "")}</span></span><button class="button secondary" data-resume="${esc(d.domain)}">Reanudar</button></div>`,
    )
    .join("");
  const warn = get("capacity-warning");
  warn.hidden = s.capacity.length === 0;
  warn.textContent = s.capacity
    .map(
      (c) =>
        `«${c.provider}»: ${c.watches} seguimientos en un mismo sitio; cada uno se revisará aprox. cada ${Math.round(c.effective_interval_s / 60)} min. ${c.note}`,
    )
    .join(" ");
  get("domain-list")
    .querySelectorAll<HTMLButtonElement>("[data-resume]")
    .forEach((b) =>
      b.addEventListener("click", () => {
        void act(b, async () => {
          await api(
            `/api/domains/${encodeURIComponent(b.dataset.resume ?? "")}/resume`,
            { method: "POST" },
          );
          notice("Sitio reanudado. Se volverá a consultar con el espaciado normal.");
          await load();
        });
      }),
    );
  get("notice").textContent = anyLive
    ? "Monitor con fuentes reales. Los precios pueden cambiar; confírmalos en la fuente. Sin reservas ni pagos."
    : "Demostración sintética. No consulta tiendas ni aerolíneas reales y no permite reservar ni comprar.";
}
function renderCatalogs(rows: CatalogRow[]): void {
  const hasStore = catalog.providers.some((p) => p.name === "store");
  get("catalog-form").hidden = !hasStore;
  get("catalog-rows").innerHTML =
    rows
      .map((c) => {
        const where = `<strong>${esc(c.domain)}</strong><div class="route-meta">${esc(c.url)}${c.category ? " · " + esc(c.category) : ""}</div>`;
        const state = c.enabled
          ? `<span class="tag ${esc(c.last_status ?? "")}">${esc(c.last_status ? (states[c.last_status] ?? c.last_status) : "Pendiente")}</span>`
          : '<span class="tag disabled">Pausado</span>';
        const why = c.last_error ? `<div class="route-meta">${esc(c.last_error)}</div>` : "";
        return `<tr><td>${where}</td><td>${c.mode === "sitemap" ? "Sitemap" : "Categoría"}<div class="route-meta">máx. ${c.max_items}</div></td><td>${state}${why}</td><td>${c.watches}<div class="route-meta">${c.found_count} encontrados</div></td><td>${c.enabled ? esc(inMinutes(c.next_run_at)) : "—"}</td><td><button class="button secondary" data-catalog="${esc(c.id)}" data-on="${c.enabled ? "0" : "1"}">${c.enabled ? "Pausar" : "Reanudar"}</button></td></tr>`;
      })
      .join("") ||
    '<tr><td colspan="6" class="table-empty">Sin catálogos. Añade uno abajo o importa tiendas con <code>python -m radar.main --import-sources</code>.</td></tr>';
  get("catalog-rows")
    .querySelectorAll<HTMLButtonElement>("[data-catalog]")
    .forEach((b) =>
      b.addEventListener("click", () => {
        void act(b, async () => {
          await api(
            `/api/catalogs/${encodeURIComponent(b.dataset.catalog ?? "")}`,
            json({ enabled: b.dataset.on === "1" }, "PATCH"),
          );
          await load();
        });
      }),
    );
}
function renderIncidents(rows: Incident[]): void {
  get("incident-rows").innerHTML =
    rows
      .map(
        (i) =>
          `<tr><td>${esc(utc(i.at))}</td><td><span class="tag ${esc(i.severity)}">${esc(i.severity === "high" ? "Alta" : i.severity === "medium" ? "Media" : "Baja")}</span></td><td>${esc(i.domain)}</td><td>${esc(states[i.kind] ?? i.kind)}${i.http_status ? `<div class="route-meta">HTTP ${i.http_status}</div>` : ""}</td><td>${esc(states[i.action] ?? i.action)}</td><td class="alert-message">${esc(i.detail)}</td></tr>`,
      )
      .join("") || '<tr><td colspan="6" class="table-empty">Sin incidentes.</td></tr>';
}
function renderCompare(groups: Group[]): void {
  get("compare-rows").innerHTML =
    groups
      .map((g) => {
        const stores = g.stores
          .map((s) => {
            const note = s.availability === "in_stock" ? "" : ` · ${esc(s.availability ?? "stock sin indicar")}`;
            const cheapest = s.domain === g.cheapest ? " <strong>★</strong>" : "";
            return `<div class="store-row"><span>${esc(s.domain ?? "?")}${cheapest}</span><span>${esc(money(s.price_cents, g.currency))}${note}${s.expired ? " · caducada" : ""}</span></div>`;
          })
          .join("");
        const spread =
          g.spread_percent === null
            ? "Sin dos tiendas con stock"
            : `${g.spread_percent} % · ${esc(money(g.saving_cents ?? 0, g.currency))}`;
        return `<tr><td>${esc(g.title ?? g.match_key)}<div class="route-meta">${esc(g.match_key)} · ${esc(g.currency)}${g.condition ? " · " + esc(g.condition) : ""}</div></td><td>${stores}</td><td>${spread}</td></tr>`;
      })
      .join("") ||
    '<tr><td colspan="3" class="table-empty">Aún no hay productos presentes en dos o más tiendas.</td></tr>';
}
function renderResources(status: Status): void {
  const r = status.resources;
  const mb = (v: number | null): string => (v === null ? "n/d" : `${v} MB`);
  get("resource-list").innerHTML = [
    `Procesador del monitor: ${r.cpu_seconds} s acumulados (${r.cpu_percent_of_one_core} % de un núcleo)`,
    `Memoria: ${mb(r.rss_mb)} · base de datos: ${mb(r.database_mb)} · disco libre: ${r.disk_free_gb === null ? "n/d" : r.disk_free_gb + " GB"}`,
    `Consultas: ${r.requests_total} · descargado: ${r.downloaded_mb} MB (≈ ${r.downloaded_mb_per_day} MB/día)`,
    `Incidentes de acceso en 24 h: ${r.incidents_24h}`,
    `Filas: ${Object.entries(r.rows).map(([k, v]) => `${esc(k)} ${v}`).join(" · ")}`,
    esc(r.note),
  ]
    .map((l) => `<li>${l}</li>`)
    .join("");
  get("egress-list").innerHTML =
    status.egress
      .map(
        (e) =>
          `<div class="egress-row"><span><strong>${esc(e.name)}</strong> <span class="tag ${e.enabled ? "live" : "disabled"}">${e.enabled ? "Activa" : "Apagada"}</span><br><span class="route-meta">${e.kind === "direct" ? "Salida directa desde este servidor" : "Proxy autorizado: " + esc(e.endpoint ?? "")}</span></span><button class="button secondary" data-egress="${esc(e.name)}" data-on="${e.enabled ? "0" : "1"}">${e.enabled ? "Apagar" : "Activar"}</button></div>`,
      )
      .join("") || '<p class="route-meta">Sin lector de tiendas configurado.</p>';
  get("egress-list")
    .querySelectorAll<HTMLButtonElement>("[data-egress]")
    .forEach((b) =>
      b.addEventListener("click", () => {
        void act(b, async () => {
          await api(
            `/api/egress/${encodeURIComponent(b.dataset.egress ?? "")}`,
            json({ enabled: b.dataset.on === "1" }),
          );
          notice(
            b.dataset.on === "1"
              ? "Salida activada."
              : "Salida apagada: los sitios asignados a ella no recibirán consultas (no se cambia a otra salida).",
          );
          await load();
        });
      }),
    );
}
async function loadSources(): Promise<void> {
  if (sourcesLoaded) return;
  const rows = await api<SourceRow[]>("/api/sources");
  sourcesLoaded = true;
  get("sources-count").textContent = String(rows.length);
  get("source-rows").innerHTML = rows
    .map(
      (r) =>
        `<tr><td>${esc(r.name)}${r.enabled ? ' <span class="tag live">Activa</span>' : ""}</td><td>${esc(r.kind)}</td><td>${esc(r.country)}</td><td>${esc(r.domain)}</td><td>${esc(r.status === "sin_conector" ? "Sin conector" : r.status === "unverified" ? "Sin verificar" : r.status)}</td></tr>`,
    )
    .join("");
}
async function history(): Promise<void> {
  if (!selected) return;
  const data = await api<History>(
    `/api/watches/${selected}/history?limit=20&offset=${offset}`,
  );
  const watch = watches.find((w) => w.id === selected);
  get("history-caption").textContent = watch
    ? `${kindLabel[watch.kind]} · ${watch.label ?? ""} ${watch.kind === "product" ? text(watch.criteria.url) : text(watch.criteria.origin) + " → " + text(watch.criteria.destination)} · ${watch.criteria.currency} · ${providerFor(watch.provider)?.live ? "datos reales" : "datos sintéticos"}`
    : "Historial";
  get("observation-rows").innerHTML =
    data.observations
      .map(
        (o) =>
          `<tr><td>${esc(utc(o.observed_at))}</td><td class="file-name">${esc(money(o.total_cents, o.currency))}</td><td><span class="tag ${o.expired ? "error" : ""}">${o.expired ? "Caducada" : o.synthetic ? "Vigente (demo)" : "Vigente"}</span></td><td>${o.analytics?.change_percent == null ? "Sin comparación" : `${o.analytics.change_percent}%`}<div class="route-meta">${esc(o.analytics?.note ?? "")}</div></td><td><details class="conditions"><summary>Ver condiciones</summary><pre>${esc(JSON.stringify(o.conditions, null, 2))}</pre><p>${esc(o.source)}</p></details></td></tr>`,
      )
      .join("") ||
    '<tr><td colspan="5" class="table-empty">Aún no hay observaciones.</td></tr>';
  get("query-rows").innerHTML =
    data.queries
      .map(
        (q) =>
          `<tr><td>${esc(utc(q.started_at))}</td><td><span class="tag ${esc(q.error_kind ?? q.status)}">${esc(states[q.error_kind ?? ""] ?? states[q.status] ?? q.status)}</span></td><td>${esc(q.error ?? "Consulta correcta; la observación conserva su procedencia.")}</td></tr>`,
      )
      .join("") ||
    '<tr><td colspan="3" class="table-empty">Aún no hay consultas.</td></tr>';
  get<HTMLButtonElement>("prev").disabled = offset === 0;
  get<HTMLButtonElement>("next").disabled =
    offset + 20 >= Math.max(data.observation_count, data.query_count);
  get("page-label").textContent =
    `${data.observation_count} observaciones · ${data.query_count} consultas · página ${offset / 20 + 1}`;
}
async function load(): Promise<void> {
  const kind = get<HTMLSelectElement>("filter-kind").value;
  const category = get<HTMLSelectElement>("filter-category").value;
  const query = new URLSearchParams();
  if (kind) query.set("kind", kind);
  if (category) query.set("category", category);
  const [rows, alerts, status, catalogs, incidents, groups] = await Promise.all([
    api<Watch[]>(`/api/watches?${query.toString()}`),
    api<Alert[]>(`/api/alerts?limit=100&offset=${alertOffset}`),
    api<Status>("/api/status"),
    api<CatalogRow[]>("/api/catalogs"),
    api<Incident[]>("/api/incidents?limit=50"),
    api<Group[]>("/api/compare?limit=50"),
  ]);
  watches = rows;
  catalogRows = catalogs;
  renderStatus(status);
  renderCatalogs(catalogs);
  renderIncidents(incidents);
  renderCompare(groups);
  renderResources(status);
  get("watch-count").textContent = String(rows.length);
  get("fresh-count").textContent = String(
    rows.filter(
      (w) =>
        w.latest_offers.some((o) => !o.expired) &&
        w.last_query?.status === "success",
    ).length,
  );
  get("error-count").textContent = String(
    rows.filter((w) => w.last_query?.status === "error").length,
  );
  get("alert-count").textContent =
    alerts.length === 100 ? "100+" : String(alerts.length);
  get("alert-caption").textContent = "Últimas 100 · consulta la API para más";
  get("watch-rows").innerHTML =
    rows
      .map((w) => {
        const price = w.latest_offers[0];
        const live = providerFor(w.provider)?.live ?? false;
        const scenarios = live
          ? ""
          : `<label>Escenario<select data-scenario="${esc(w.id)}"><option value="auto">Automático</option><option value="baseline">Base</option><option value="drop">Bajada de precio</option><option value="failure">Fallo del proveedor</option><option value="blocked">Bloqueo del sitio</option><option value="expired">Oferta caducada</option><option value="different_conditions">Otras condiciones</option><option value="missing_conditions">Condiciones incompletas</option>${w.kind === "product" ? '<option value="out_of_stock">Sin stock</option>' : ""}</select></label>`;
        return `<tr><td><span class="kind-label">${esc(kindLabel[w.kind])}</span> ${sourceTag(w)}<div class="route">${title(w)}</div><div class="route-meta">${detail(w)}</div></td><td>${dates(w)}</td><td>${price ? `<strong>${esc(money(price.total_cents, price.currency))}</strong><div class="route-meta">${esc(utc(price.observed_at))} UTC<br>${price.expired ? "Caducada" : w.last_query?.status === "error" ? "Dato anterior al fallo" : price.synthetic ? "Observación sintética" : "Observación real"}<br>Vence: ${esc(utc(price.expires_at))} UTC</div>` : "Sin observación"}</td><td><span class="tag ${esc(w.last_query?.error_kind ?? w.last_query?.status ?? "")}">${esc(w.last_query ? (states[w.last_query.error_kind ?? ""] ?? states[w.last_query.status] ?? w.last_query.status) : "Sin consultar")}</span><div class="route-meta">${esc(w.last_query?.error ?? "")}</div><div class="route-meta">${esc(schedule(w))}</div></td><td class="watch-actions">${scenarios}<button class="button secondary" data-refresh="${esc(w.id)}">Consultar ↻</button> <button class="button secondary" data-history="${esc(w.id)}">Historial</button><br><button class="button secondary" data-config="${esc(w.id)}">Configurar</button></td></tr>`;
      })
      .join("") ||
    '<tr><td colspan="5" class="table-empty">Crea un seguimiento o carga un ejemplo.</td></tr>';
  get("watch-rows")
    .querySelectorAll<HTMLButtonElement>("[data-refresh]")
    .forEach((b) =>
      b.addEventListener("click", () => {
        void act(b, async () => {
          const id = b.dataset.refresh ?? "";
          const select = get("watch-rows").querySelector<HTMLSelectElement>(
            `select[data-scenario="${id}"]`,
          );
          const result = await api<{ status: string; error?: string }>(
            `/api/watches/${id}/refresh`,
            json({ scenario: select?.value ?? "baseline" }),
          );
          selected = id;
          offset = 0;
          await load();
          notice(
            result.status === "error"
              ? (result.error ?? "Consulta fallida.")
              : "Consulta guardada. Las condiciones y la procedencia se conservan.",
            result.status === "error",
          );
        });
      }),
    );
  get("watch-rows")
    .querySelectorAll<HTMLButtonElement>("[data-history]")
    .forEach((b) =>
      b.addEventListener("click", () => {
        selected = b.dataset.history ?? null;
        offset = 0;
        void history()
          .then(() => get("history").scrollIntoView({ block: "start" }))
          .catch(fail);
      }),
    );
  get("watch-rows")
    .querySelectorAll<HTMLButtonElement>("[data-config]")
    .forEach((b) =>
      b.addEventListener("click", () => {
        configKey = b.dataset.config ?? null;
        const w = watches.find((w) => w.id === configKey);
        if (!w) return;
        get("configuration").hidden = false;
        get("config-caption").textContent =
          `${kindLabel[w.kind]} · ${w.label ?? (w.kind === "product" ? text(w.criteria.url) : text(w.criteria.origin) + " → " + text(w.criteria.destination))} · ${w.criteria.currency}`;
        get<HTMLSelectElement>("config-mode").value = w.alert_mode;
        get<HTMLInputElement>("config-target").value =
          w.alert_mode === "smart" ? "" : (w.target_cents / 100).toFixed(2);
        get<HTMLInputElement>("config-enabled").checked = Boolean(w.enabled);
        get<HTMLSelectElement>("config-channel").value = w.channel;
        get("configuration").scrollIntoView({ block: "start" });
        get("config-target").focus();
      }),
    );
  get("alert-rows").innerHTML =
    alerts
      .map(
        (a) =>
          `<tr><td>${esc(utc(a.created_at))}</td><td>${a.priority ? `<span class="tag ${esc(a.priority)}">${esc(states[a.priority] ?? a.priority)}</span>` : ""}</td><td>${a.channel === "simulation" ? "Simulación" : "Telegram"}</td><td><span class="tag ${esc(a.status)}">${esc(states[a.status] ?? a.status)}</span></td><td class="alert-message">${esc(a.message)}${a.error ? `<div>${esc(a.error)}</div>` : ""}</td></tr>`,
      )
      .join("") ||
    '<tr><td colspan="5" class="table-empty">No hay alertas registradas. Se crean cuando el precio cumple el objetivo o cae bajo su referencia.</td></tr>';
  await history();
}
function json(body: unknown, method = "POST"): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}
async function act(
  button: HTMLButtonElement,
  fn: () => Promise<void>,
): Promise<void> {
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  try {
    await fn();
  } catch (e) {
    fail(e);
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
}
function buildCriteria(kind: Kind): Record<string, unknown> {
  const currency = get<HTMLSelectElement>("currency").value;
  if (kind === "flight")
    return {
      kind,
      origin: value("origin").toUpperCase(),
      destination: value("destination").toUpperCase(),
      departure: value("departure"),
      return_date: value("return-date") || null,
      adults: integer("adults"),
      children: integer("children"),
      infants: integer("infants"),
      currency,
      cabin: get<HTMLSelectElement>("cabin").value,
      max_connections: integer("max-connections"),
      airlines: list("airlines", true),
    };
  if (kind === "bus")
    return {
      kind,
      origin: value("bus-origin"),
      destination: value("bus-destination"),
      departure: value("bus-departure"),
      return_date: value("bus-return") || null,
      passengers: integer("bus-passengers"),
      currency,
      operators: list("bus-operators"),
    };
  return {
    kind,
    url: value("product-url"),
    sku: value("product-sku") || null,
    currency,
  };
}
get("demo").addEventListener("click", () => {
  void act(get("demo"), async () => {
    const kind = currentKind();
    const data = await api<{ watch_id: string }>(`/api/demo?kind=${kind}`, {
      method: "POST",
    });
    selected = data.watch_id;
    offset = 0;
    await load();
    notice(
      "Ejemplo sintético creado. Con «Consultar» y el escenario «Bajada de precio» verás una alerta simulada.",
    );
  });
});
get<HTMLFormElement>("create-form").addEventListener("submit", (e) => {
  e.preventDefault();
  void act(get("create-button"), async () => {
    const kind = currentKind();
    const row = await api<Watch>(
      "/api/watches",
      json({
        criteria: buildCriteria(kind),
        ...(currentMode() === "smart" ? {} : { target_cents: cents("target") }),
        alert_mode: currentMode(),
        enabled: true,
        channel: get<HTMLSelectElement>("channel").value,
        provider: get<HTMLSelectElement>("provider").value,
        label: value("label") || null,
        category:
          kind === "product" ? get<HTMLSelectElement>("category").value : null,
        subcategory:
          kind === "product" ? value("subcategory") || null : null,
      }),
    );
    selected = row.id;
    offset = 0;
    // A new watch must not inherit the previous one's name or link.
    get<HTMLInputElement>("label").value = "";
    get<HTMLInputElement>("product-url").value = "";
    get<HTMLInputElement>("product-sku").value = "";
    await load();
    notice(
      "Seguimiento creado. Se revisará automáticamente en unos instantes; «Consultar» fuerza una revisión ahora.",
    );
  });
});
get<HTMLFormElement>("config-form").addEventListener("submit", (e) => {
  e.preventDefault();
  void act(get("config-save"), async () => {
    if (!configKey) throw new Error("Selecciona un seguimiento.");
    await api(
      `/api/watches/${configKey}`,
      json(
        {
          ...(value("config-target") ? { target_cents: cents("config-target") } : {}),
          alert_mode: get<HTMLSelectElement>("config-mode").value,
          enabled: get<HTMLInputElement>("config-enabled").checked,
          channel: get<HTMLSelectElement>("config-channel").value,
        },
        "PATCH",
      ),
    );
    await load();
    notice(
      "Configuración guardada. Una misma observación no se vuelve a alertar.",
    );
  });
});
get("prev").addEventListener("click", () => {
  offset = Math.max(0, offset - 20);
  void history().catch(fail);
});
get("next").addEventListener("click", () => {
  offset += 20;
  void history().catch(fail);
});
get<HTMLFormElement>("catalog-form").addEventListener("submit", (e) => {
  e.preventDefault();
  void act(get("catalog-save"), async () => {
    await api(
      "/api/catalogs",
      json({
        url: value("catalog-url"),
        mode: get<HTMLSelectElement>("catalog-mode").value,
        category: get<HTMLSelectElement>("catalog-category").value,
        subcategory: value("catalog-subcategory") || null,
        currency: get<HTMLSelectElement>("catalog-currency").value,
        include_regex: value("catalog-regex") || null,
        max_items: integer("catalog-max"),
        alert_mode: "smart",
        channel: "simulation",
      }),
    );
    get<HTMLInputElement>("catalog-url").value = "";
    await load();
    notice("Catálogo añadido. Se barrerá en unos segundos y luego cada pocas horas.");
  });
});
get("catalog-category").addEventListener("change", () => {
  const subs = catalog.categories[get<HTMLSelectElement>("catalog-category").value]?.subcategories ?? [];
  fillSelect("catalog-subcategory", [["", "(sin subcategoría)"], ...subs.map((x): [string, string] => [x, x.replace(/_/g, " ")])]);
});
get("alert-mode").addEventListener("change", () => {
  get("alert-mode").dataset.touched = "1";
  syncMode();
});
get("sources-details").addEventListener("toggle", () => void loadSources().catch(fail));
get("kind").addEventListener("change", syncForm);
get("provider").addEventListener("change", syncForm);
get("category").addEventListener("change", syncSubcategories);
get("filter-kind").addEventListener("change", () => void load().catch(fail));
get("filter-category").addEventListener("change", () => void load().catch(fail));
const isoIn = (days: number): string => {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
};
get<HTMLInputElement>("departure").value = isoIn(45);
get<HTMLInputElement>("return-date").value = isoIn(59);
get<HTMLInputElement>("bus-departure").value = isoIn(30);
async function init(): Promise<void> {
  catalog = await api<Catalog>("/api/catalog");
  fillSelect("category", Object.entries(catalog.categories).map(([k, v]): [string, string] => [k, v.label]), false);
  fillSelect("filter-kind", [
    ["", "Todos"],
    ...Object.entries(catalog.kinds).map(([k, v]): [string, string] => [k, v.label]),
  ]);
  fillSelect("filter-category", [
    ["", "Todas"],
    ["viajes", "Viajes (vuelos y buses)"],
    ...Object.entries(catalog.categories).map(([k, v]): [string, string] => [k, v.label]),
  ]);
  fillSelect(
    "catalog-category",
    [["", "Elige una categoría"], ...Object.entries(catalog.categories).map(([k, v]): [string, string] => [k, v.label])],
    false,
  );
  get("catalog-category").dispatchEvent(new Event("change"));
  get("demo").hidden = !catalog.providers.some((p) => p.name === "demo");
  syncForm();
  await load();
  void loadSources().catch(() => undefined); // only a local list: gives the real count before the panel is opened
}
void init().catch(fail);
// Refresh the table while the page is open, without clobbering a scenario being chosen.
setInterval(() => {
  const active = document.activeElement;
  if (document.visibilityState !== "visible" || active instanceof HTMLSelectElement)
    return;
  void load().catch(() => undefined);
}, 30000);
